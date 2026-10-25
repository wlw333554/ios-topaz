/*
 * Milestone 5 probe: can we talk to the internal UFS that ABL booted from?
 *
 * Runs with the MMU off (all memory Device, so DMA is coherent with no cache
 * maintenance). Dumps the UFSHCI registers ABL left behind and, if the host
 * is enabled and the link is up, points the transfer request list at our own
 * buffers and issues READ-ONLY SCSI commands on LUN 0. Nothing is ever
 * written to the device: there is no SCSI WRITE in this file.
 *
 * Register layout and descriptor formats: JEDEC UFSHCI (JESD223).
 */
#include <stdint.h>
#include "console.h"
#include "ufs.h"

/* Base comes from the device header (device.h, via console.h). */
#ifndef UFS_PROBE_BASE
#error "device header must define UFS_PROBE_BASE"
#endif
#define UFS_BASE	UFS_PROBE_BASE

#define REG_CAP		0x00
#define REG_VER		0x08
#define REG_IS		0x20
#define REG_HCS		0x30
#define REG_HCE		0x34
#define REG_UTRLBA	0x50
#define REG_UTRLBAU	0x54
#define REG_UTRLDBR	0x58
#define REG_UTRLCLR	0x5c
#define REG_UTRLRSR	0x60

#define HCS_DP		(1u << 0)
#define HCS_UTRLRDY	(1u << 1)
#define HCS_UCRDY	(1u << 3)

#define BLOCK		4096u

/*
 * gcc expands strcmp(x, "literal") inline; clang emits a libcall and the
 * shim links with -nostdlib, so provide the one this file needs. Non-static:
 * if the compiler does inline the calls, this is simply dead-stripped.
 */
int strcmp(const char *a, const char *b)
{
	while (*a && *a == *b)
		a++, b++;
	return (int)(unsigned char)*a - (int)(unsigned char)*b;
}

static inline uint32_t ufs_rd(uint32_t off) { return *(volatile uint32_t *)(UFS_BASE + off); }
static inline void ufs_wr(uint32_t off, uint32_t v) { *(volatile uint32_t *)(UFS_BASE + off) = v; }

/* UTP transfer request descriptor (32 bytes). */
struct utrd {
	uint32_t dw0;		/* [31:28] CT, [26:25] DD, [24] I */
	uint32_t dw1;
	uint32_t dw2;		/* [7:0] OCS */
	uint32_t dw3;
	uint32_t ucdba;		/* command descriptor base, 128-byte aligned */
	uint32_t ucdbau;
	uint32_t resp;		/* [31:16] response UPIU offset, [15:0] length (DWORDs) */
	uint32_t prdt;		/* [31:16] PRDT offset, [15:0] entries (DWORDs/entries) */
};

/* UTP command descriptor: command UPIU, response UPIU, PRDT. */
struct ucd {
	uint8_t cmd[512];
	uint8_t resp[512];
	struct {
		uint32_t addr_lo, addr_hi, rsvd, dbc;	/* dbc = byte count - 1 */
	} prdt[1];
};

static struct utrd utrl[32] __attribute__((aligned(1024)));
static struct ucd ucd __attribute__((aligned(128)));
static uint8_t data[2 * BLOCK] __attribute__((aligned(4096)));

static uint64_t ticks(void)
{
	uint64_t t;
	__asm__ volatile("isb; mrs %0, cntvct_el0" : "=r"(t));
	return t;
}

static void zero(void *p, unsigned n)
{
	volatile uint8_t *b = p;
	for (unsigned i = 0; i < n; i++)
		b[i] = 0;
}

static uint32_t be32(const uint8_t *p)
{
	return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

/*
 * Issue one SCSI command (read direction or no data) in slot 0.
 * Returns 0 on success, otherwise a code describing the failure stage.
 */
static uint32_t scsi(uint8_t lun, const uint8_t *cdb, unsigned cdb_len, void *buf, uint32_t len,
    uint32_t *ocs_out, uint32_t *status_out)
{
	static uint8_t tag;
	zero(&utrl[0], sizeof(utrl[0]));
	zero(&ucd, sizeof(ucd));

	/* Command UPIU: header (12 bytes), expected length, CDB. */
	ucd.cmd[0] = 0x01;			/* transaction type: COMMAND */
	ucd.cmd[1] = len ? 0x40 : 0x00;		/* flags: R (data in) */
	ucd.cmd[2] = lun;
	ucd.cmd[3] = ++tag;
	ucd.cmd[12] = len >> 24;
	ucd.cmd[13] = len >> 16;
	ucd.cmd[14] = len >> 8;
	ucd.cmd[15] = len;
	for (unsigned i = 0; i < cdb_len; i++)
		ucd.cmd[16 + i] = cdb[i];

	uintptr_t ucd_pa = (uintptr_t)&ucd;
	utrl[0].dw0 = (1u << 28) | ((len ? 2u : 0u) << 25) | (1u << 24);	/* UFS storage, dev->host */
	utrl[0].dw2 = 0x0f;			/* OCS: invalid until the host updates it */
	utrl[0].ucdba = (uint32_t)ucd_pa;
	utrl[0].ucdbau = (uint32_t)(ucd_pa >> 32);
	utrl[0].resp = (512u / 4) << 16 | (512u / 4);
	if (len) {
		uintptr_t buf_pa = (uintptr_t)buf;
		ucd.prdt[0].addr_lo = (uint32_t)buf_pa;
		ucd.prdt[0].addr_hi = (uint32_t)(buf_pa >> 32);
		ucd.prdt[0].dbc = len - 1;
		utrl[0].prdt = (1024u / 4) << 16 | 1;
	}
	__asm__ volatile("dsb sy" ::: "memory");

	ufs_wr(REG_UTRLDBR, 1);
	uint64_t start = ticks();
	while (ufs_rd(REG_UTRLDBR) & 1) {
		if (ticks() - start > 19200000) {	/* 1 s */
			ufs_wr(REG_UTRLCLR, ~1u);	/* clear slot 0 (write 0 to the bit) */
			return 1;			/* timeout */
		}
	}
	__asm__ volatile("dsb sy" ::: "memory");
	*ocs_out = utrl[0].dw2 & 0xff;
	*status_out = ucd.resp[7];		/* SCSI status in the response UPIU */
	if (*ocs_out)
		return 2;
	if (ucd.resp[0] != 0x21)		/* RESPONSE UPIU */
		return 3;
	return *status_out ? 4 : 0;
}

static uint64_t le64(const uint8_t *p)
{
	uint64_t v = 0;
	for (int i = 7; i >= 0; i--)
		v = v << 8 | p[i];
	return v;
}

/* Name the filesystem whose superblock starts 1024 bytes into a partition
 * that begins at byte offset part_byte (device blocks of blk bytes). */
static const char *fs_type(uint8_t lun, uint64_t part_byte, uint32_t blk)
{
	if (part_byte % blk)
		return "unaligned";
	uint8_t rd[10] = { 0x28, 0,
	    (uint8_t)(part_byte / blk >> 24), (uint8_t)(part_byte / blk >> 16),
	    (uint8_t)(part_byte / blk >> 8), (uint8_t)(part_byte / blk),
	    0, 0, 1, 0 };
	uint32_t ocs, st;
	if (scsi(lun, rd, sizeof(rd), data, blk, &ocs, &st))
		return "read error";
	const uint8_t *sb = data + 1024;
	if (sb[0] == 0x10 && sb[1] == 0x20 && sb[2] == 0xf5 && sb[3] == 0xf2)
		return "f2fs";
	if (sb[56] == 0x53 && sb[57] == 0xef)
		return "ext4";
	return "other";
}

/* One line per GPT entry of at least 64 MiB: name, first LBA, size in MiB.
 * The entry array starts at byte offset ent_byte on the device (the table's
 * sector size and the device's logical block size may differ). */
static void list_partitions(uint8_t lun, uint64_t ent_byte, uint32_t nent, uint32_t gsec, uint32_t blk)
{
	static uint8_t ents[32 * 1024] __attribute__((aligned(4096)));
	uint64_t first_blk = ent_byte / blk;
	uint32_t skew = (uint32_t)(ent_byte % blk);
	uint32_t blocks = (skew + nent * 128 + blk - 1) / blk;
	if (blocks * blk > sizeof(ents))
		blocks = sizeof(ents) / blk;
	uint8_t rd[10] = { 0x28, 0,
	    (uint8_t)(first_blk >> 24), (uint8_t)(first_blk >> 16),
	    (uint8_t)(first_blk >> 8), (uint8_t)first_blk,
	    0, (uint8_t)(blocks >> 8), (uint8_t)blocks, 0 };
	uint32_t ocs, st;
	uint32_t rc = scsi(lun, rd, sizeof(rd), ents, blocks * blk, &ocs, &st);
	con_hex2("UFS PART", "rc=", rc, "blocks=", blocks);
	if (rc)
		return;
	const uint8_t *base = ents + skew;
	for (uint32_t i = 0; i < nent; i++) {
		const uint8_t *e = base + i * 128;
		uint64_t first = le64(e + 32), last = le64(e + 40);
		if (!first || last < first)
			continue;
		char name[17];
		for (int c = 0; c < 16; c++) {
			uint8_t ch = e[56 + 2 * c] & 0x7f;
			name[c] = (ch >= 32 && ch < 127) ? ch : (ch ? '?' : 0);
		}
		name[16] = 0;
		uint64_t bytes = (last - first + 1) * gsec;
		if (bytes < (64ull << 20))	/* keep the screen to the big ones */
			continue;
		con_hex2(name, "lba=", (uint32_t)first,
		    "MiB=", (uint32_t)(bytes >> 20));
		if (!__builtin_strcmp(name, "userdata") || !__builtin_strcmp(name, "cust") ||
		    !__builtin_strcmp(name, "cache"))
			con_str(" fs", fs_type(lun, first * gsec, blk));
	}
}

/*
 * Is this QEMU's virt machine? The probe reads a hard-coded MMIO base
 * (UFS_PROBE_BASE); nothing answers there under QEMU and the read would abort.
 *
 * Deliberately fail-open: only a positive "linux,dummy-virt" match suppresses
 * the probe. The phone's blob is not even a standard FDT (its magic is MTK's
 * 0xd7b7ab1e, not 0xd00dfeed), so a check that required a well-formed FDT would
 * silently skip the dump on the one machine we care about.
 */
int
ufs_is_qemu(uintptr_t fdt)
{
	static const char want[] = "linux,dummy-virt";

	if (!fdt)
		return 0;
	if (__builtin_bswap32(*(volatile uint32_t *)fdt) != 0xd00dfeedu)
		return 0;			/* not an FDT at all: assume real hw */
	uint32_t total = __builtin_bswap32(*(volatile uint32_t *)(fdt + 4));
	if (total < 64 || total > (16u << 20))
		return 0;
	const char *b = (const char *)fdt;
	unsigned n = 0;
	while (want[n])
		n++;

	for (uint32_t i = 0; i + n <= total; i++) {
		unsigned k = 0;
		while (k < n && b[i + k] == want[k])
			k++;
		if (k == n)
			return 1;
	}
	return 0;
}

void test_ufs(void)
{
	con_str("UFS", "milestone 5 probe (read-only)");
	uint32_t cap = ufs_rd(REG_CAP), hcs = ufs_rd(REG_HCS), hce = ufs_rd(REG_HCE);
	con_hex2("UFS", "ver=", ufs_rd(REG_VER), "cap=", cap);
	con_hex2("UFS", "hce=", hce, "hcs=", hcs);
	con_hex2("UFS", "is=", ufs_rd(REG_IS), "rsr=", ufs_rd(REG_UTRLRSR));
	con_hex2("UFS", "utrlba=", ufs_rd(REG_UTRLBA), "hi=", ufs_rd(REG_UTRLBAU));
	con_hex2("UFS", "dbr=", ufs_rd(REG_UTRLDBR), "nutrs=", (cap & 0x1f) + 1);

	/* A dead / clock-gated block reads back 0 or all-ones: stop before any
	 * write so an unverified MTK register map can never be corrupted. */
	if (cap == 0 || cap == 0xffffffffu) {
		con_str("UFS", "no response at base (clock/power gated?)");
		return;
	}
#if !UFS_PROBE_TAKEOVER
	con_str("UFS", "read-only dump only (takeover disabled on this device)");
	return;
#else
	if (!(hce & 1) || !(hcs & HCS_DP) || !(hcs & HCS_UTRLRDY)) {
		con_str("UFS", "host not enabled/ready: stop");
		return;
	}
	if (ufs_rd(REG_UTRLDBR)) {
		con_str("UFS", "requests outstanding: stop");
		return;
	}

	/* Take over the transfer request list: stop, point at ours, run. */
	ufs_wr(REG_UTRLRSR, 0);
	uintptr_t list = (uintptr_t)utrl;
	ufs_wr(REG_UTRLBA, (uint32_t)list);
	ufs_wr(REG_UTRLBAU, (uint32_t)(list >> 32));
	ufs_wr(REG_UTRLRSR, 1);
	con_hex2("UFS", "ours=", (uint32_t)list, "rsr=", ufs_rd(REG_UTRLRSR));
	/*
	 * Never ring a doorbell unless the host really uses our list: otherwise
	 * it would execute whatever ABL left in its own slot 0 (maybe a write).
	 */
	if (ufs_rd(REG_UTRLBA) != (uint32_t)list || ufs_rd(REG_UTRLBAU) != (uint32_t)(list >> 32) ||
	    !(ufs_rd(REG_UTRLRSR) & 1)) {
		con_str("UFS", "list base not taken: stop");
		return;
	}

	/*
	 * evergo ground truth (measured): LUN 0 is a 4 MiB boot LUN full of
	 * 0xff - no GPT. The partition table with userdata lives on another
	 * LUN, so scan them all (0..7): TUR, capacity, first 8 KiB, GPT.
	 */
	static const uint8_t tur[6] = { 0x00 };
	static const uint8_t cap10[10] = { 0x25 };	/* READ CAPACITY(10) */
	for (uint8_t lun = 0; lun < 8; lun++) {
		uint32_t ocs = 0, st = 0, rc;
		/* A first command on a LUN may report a unit attention; retry. */
		for (int i = 0; i < 3; i++) {
			rc = scsi(lun, tur, sizeof(tur), 0, 0, &ocs, &st);
			if (!rc)
				break;
		}
		con_hex2("UFS LUN", "n=", lun, "tur rc=", rc);
		if (rc)
			continue;		/* no such / not ready LUN */

		rc = scsi(lun, cap10, sizeof(cap10), data, 8, &ocs, &st);
		if (rc) {
			con_hex2("UFS CAP", "n=", lun, "rc=", rc);
			continue;
		}
		uint32_t lastlba = be32(data);
		uint32_t blk = be32(data + 4);
		con_hex2("UFS LUN", "n=", lun, "lastlba=", lastlba);
		con_hex2("UFS LUN", "blk=", blk,
		    "MiB=", (uint32_t)(((uint64_t)lastlba + 1) * blk >> 20));
		if (!blk || blk > 8192u)
			blk = 4096u;

		/* The first 8 KiB in the device's own blocks: the GPT header
		 * sits at LBA 1 of the table's sector size, which may be 512
		 * bytes even when UFS reports 4 KiB blocks (then it is at byte
		 * 512, inside block 0). */
		uint32_t nblk = 8192u / blk;
		uint8_t rd10[10] = { 0x28, 0, 0, 0, 0, 0, 0, 0, 0, 0 };
		rd10[7] = nblk >> 8;
		rd10[8] = nblk & 0xff;
		rc = scsi(lun, rd10, sizeof(rd10), data, nblk * blk, &ocs, &st);
		con_hex2("UFS READ", "n=", lun, "rc=", rc);
		if (rc)
			continue;

		const uint8_t *gpt = 0;
		uint32_t gsec = blk;
		for (uint32_t off = 0; off <= 4096u; off += 512u) {
			if (data[off] == 'E' && data[off + 1] == 'F' &&
			    data[off + 2] == 'I' && data[off + 3] == ' ' &&
			    data[off + 4] == 'P') {
				gpt = data + off;
				gsec = (off == 512u) ? 512u : blk;
				break;
			}
		}
		if (!gpt) {
			con_hex2("UFS", "n=", lun, "b0=", be32(data));
			continue;
		}

		char sig[9];
		for (int i = 0; i < 8; i++)
			sig[i] = (gpt[i] >= 32 && gpt[i] < 127) ? gpt[i] : '.';
		sig[8] = 0;
		con_str("UFS GPT", sig);
		con_hex2("UFS GPT", "lun=", lun, "at=", (uint32_t)(gpt - data));
		con_hex2("UFS GPT", "secsize=", gsec, "blk=", blk);
		uint32_t nent = gpt[80] | gpt[81] << 8 | gpt[82] << 16;
		uint32_t esz = gpt[84] | gpt[85] << 8;
		uint32_t elba = gpt[72] | gpt[73] << 8 | gpt[74] << 16 | gpt[75] << 24;
		con_hex2("UFS GPT", "entries=", nent, "esz=", esz);
		con_hex2("UFS GPT", "entlba=", elba, "sec=", gsec);
		if (esz == 128 && nent && nent <= 128)
			list_partitions(lun, (uint64_t)elba * gsec, nent, gsec, blk);
	}
#endif /* UFS_PROBE_TAKEOVER */
}
