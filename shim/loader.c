/*
 * Milestone 4: act as iBoot for xnu.
 *
 * mkpayload.py already resolved the kernel's chained fixups and built the
 * ADT, so this only has to: find the payload in the bootloader ramdisk, copy
 * the segments to phys_base + (vmaddr - virt_base), place the ADT and
 * boot_args behind the kernel, and jump to the entry point with the MMU
 * and caches off, x0 = boot_args (physical).
 *
 * The DDR size is clamped at runtime to the /memory bank of the bootloader
 * DTB that actually holds phys_base, so one image boots every RAM variant
 * of the device (the ADT /chosen dram props are patched to match).
 */
#include <stdint.h>
#include "console.h"
#include "device.h"
#include "fdt.h"
#include "loader.h"

#define BOOT_LINE_LENGTH	608
#define PAGE_16K		0x4000UL

struct payload_seg {
	uint64_t vmaddr, vmsize, file_off, file_size;
};

struct payload {
	char magic[8];
	uint32_t version, nsegs;
	uint64_t virt_base, entry_va, adt_off, adt_size, kernel_end;
	uint64_t phys_base, mem_size;
	char cmdline[BOOT_LINE_LENGTH];
	struct payload_seg segs[];
};

/* pexpert/pexpert/arm64/boot.h, Revision/Version 2 */
struct boot_video {
	uint64_t v_baseAddr, v_display, v_rowBytes, v_width, v_height, v_depth;
};

struct boot_args {
	uint16_t Revision, Version;
	uint64_t virtBase, physBase, memSize, topOfKernelData;
	struct boot_video Video;
	uint32_t machineType;
	uint64_t deviceTreeP;
	uint32_t deviceTreeLength;
	char CommandLine[BOOT_LINE_LENGTH];
	uint64_t bootFlags, memSizeActual;
};

_Static_assert(__builtin_offsetof(struct payload, segs) == 0x2a8, "payload header layout");
_Static_assert(__builtin_offsetof(struct boot_args, Video) == 40, "boot_args layout");
_Static_assert(__builtin_offsetof(struct boot_args, CommandLine) == 108, "boot_args layout");
_Static_assert(sizeof(struct boot_args) == 736, "boot_args layout");

/* MMU off: everything is Device memory, so copy in aligned 8-byte words. */
static void copy64(uintptr_t dst, uintptr_t src, uint64_t n)
{
	volatile uint64_t *d = (volatile uint64_t *)dst;
	volatile const uint64_t *s = (volatile const uint64_t *)src;
	for (uint64_t i = 0; i < (n + 7) / 8; i++)
		d[i] = s[i];
}

static void zero64(uintptr_t dst, uint64_t n)
{
	volatile uint64_t *d = (volatile uint64_t *)dst;
	for (uint64_t i = 0; i < n / 8; i++)
		d[i] = 0;
}

static int magic_ok(const char *m)
{
	static const char want[] = "SWEETXNU";
	for (int i = 0; i < 8; i++)
		if (m[i] != want[i])
			return 0;
	return 1;
}

typedef void (*xnu_entry_t)(uint64_t boot_args_pa);

/* build.sh appends the payload to the Image at the first 4K boundary after _end */
extern char _end[];

/*
 * ADT walk helpers (adt.py layout): node = {u32 nprops, u32 nchildren}, then
 * per prop {char name[32], u32 size, value padded to 4}, then children.
 */
static uint64_t adt_node_size(const uint8_t *n)
{
	uint32_t nprops = *(const uint32_t *)n;
	uint32_t nchildren = *(const uint32_t *)(n + 4);
	const uint8_t *p = n + 8;

	for (uint32_t i = 0; i < nprops; i++) {
		uint32_t sz = *(const uint32_t *)(p + 32);
		p += 36 + ((sz + 3) & ~3U);
	}
	uint64_t total = (uint64_t)(p - n);
	for (uint32_t i = 0; i < nchildren; i++) {
		uint64_t cs = adt_node_size(p);
		total += cs;
		p += cs;
	}
	return total;
}

/* MMU off: the ADT values are only 4-byte aligned, so write two words. */
static void wr64(uint8_t *a, uint64_t v)
{
	*(volatile uint32_t *)a = (uint32_t)v;
	*(volatile uint32_t *)(a + 4) = (uint32_t)(v >> 32);
}

/*
 * Patch /chosen dram-base/dram-size (u64 properties) in the copied tree so
 * they match the runtime-clamped kernel RAM.
 */
static void adt_patch_chosen_dram(uintptr_t adt, uint64_t base, uint64_t size)
{
	const uint8_t *root = (const uint8_t *)adt;
	uint32_t nchildren = *(const uint32_t *)(root + 4);
	const uint8_t *p = root + 8;

	/* skip the root's own props */
	uint32_t nprops = *(const uint32_t *)root;
	for (uint32_t i = 0; i < nprops; i++) {
		uint32_t sz = *(const uint32_t *)(p + 32);
		p += 36 + ((sz + 3) & ~3U);
	}
	for (uint32_t i = 0; i < nchildren; i++) {
		const uint8_t *c = p;
		const char *name = (const char *)(c + 8);	/* prop 0 is the node name */

		if (name[0] == 'c' && name[1] == 'h' && name[2] == 'o' && name[3] == 's' &&
		    name[4] == 'e' && name[5] == 'n' && name[6] == '\0') {
			uint32_t cnprops = *(const uint32_t *)c;
			uint8_t *q = (uint8_t *)c + 8;
			for (uint32_t j = 0; j < cnprops; j++) {
				const char *pn = (const char *)q;
				uint32_t sz = *(const uint32_t *)(q + 32);
				if (sz == 8) {
					if (pn[0] == 'd' && pn[1] == 'r' && pn[2] == 'a' &&
					    pn[3] == 'm' && pn[4] == '-' && pn[5] == 'b')
						wr64(q + 36, base);
					else if (pn[0] == 'd' && pn[1] == 'r' && pn[2] == 'a' &&
						 pn[3] == 'm' && pn[4] == '-' && pn[5] == 's')
						wr64(q + 36, size);
				}
				q += 36 + ((sz + 3) & ~3U);
			}
			return;
		}
		p += adt_node_size(c);
	}
}

/*
 * Clamp the payload's kernel RAM to the bootloader-reported /memory bank that
 * contains phys_base (MTK LK patches the detected sizes into the DTB; the
 * 4/6/8 GB variants otherwise disagree with the build-time ADT). Patches the
 * payload header and the ADT /chosen dram props in place.
 */
static void clamp_memory(uintptr_t dtb, struct payload *p, uintptr_t adt_pa)
{
	uint64_t base[8], size[8];
	int n = fdt_memory_banks(dtb, base, size, 8);

	for (int i = 0; i < n; i++) {
		con_hex("MEM bank", base[i]);
		con_hex("MEM size", size[i]);
	}
	con_hex("MEM built", p->mem_size);
	if (!n)
		return;

	for (int i = 0; i < n; i++) {
		if (p->phys_base >= base[i] && p->phys_base < base[i] + size[i]) {
			uint64_t room = base[i] + size[i] - p->phys_base;
			if (room == p->mem_size)
				return;
			if (room < 0x8000000) {	/* < 128 MB: refuse to shrink that far */
				con_hex("MEM room", room);
				con_str("MEM", "bank too small, keep built-in size");
				return;
			}
			p->mem_size = room;
			adt_patch_chosen_dram(adt_pa, p->phys_base, room);
			con_hex("MEM clamped", room);
			return;
		}
	}
	con_str("MEM", "phys_base in no DTB bank, keep built-in size");
}

const char *loader_boot(uintptr_t dtb)
{
	uint64_t rd_start, rd_end;
	uintptr_t hit = 0;
	uintptr_t emb = ((uintptr_t)_end + 0xfff) & ~0xfffUL;

	/*
	 * topaz: the ABL only passes the vendor_boot ramdisk for a v4 boot
	 * image, so the payload rides behind the shim in the kernel Image.
	 * evergo (MTK LK) passes the boot ramdisk in /chosen like every other
	 * loader, so the initrd path is tried first there. Its exact size is
	 * unknown here; the kernel memory check below only needs an upper bound,
	 * and the Image is far below phys_base.
	 */
#if FB_FROM_DTB
	if (!fdt_chosen_u64(dtb, "linux,initrd-start", &rd_start) ||
	    !fdt_chosen_u64(dtb, "linux,initrd-end", &rd_end)) {
		rd_start = rd_end = 0;
	}
	if (rd_end > rd_start) {
		con_hex2("INITRD", "lo=", rd_start, "hi=", rd_end);
		for (uintptr_t a = rd_start; a + 8 <= rd_end; a++)
			if (magic_ok((const char *)a)) {
				hit = a;
				break;
			}
	}
	if (!hit && magic_ok((const char *)emb)) {
		hit = emb;
		rd_start = emb;
		rd_end = emb + 0x8000000;
		con_hex("EMBED", emb);
	}
#else
	if (magic_ok((const char *)emb)) {
		rd_start = emb;
		rd_end = emb + 0x8000000;
		con_hex("EMBED", emb);
		hit = rd_start;
	} else {
		if (!fdt_chosen_u64(dtb, "linux,initrd-start", &rd_start) ||
		    !fdt_chosen_u64(dtb, "linux,initrd-end", &rd_end))
			return "no initrd in /chosen";
		con_hex2("INITRD", "lo=", rd_start, "hi=", rd_end);
	}
	for (uintptr_t a = rd_start; a + 8 <= rd_end; a++)
		if (magic_ok((const char *)a)) {
			hit = a;
			break;
		}
#endif
	if (!hit)
		return "no SWEETXNU payload";
	if (hit != rd_start) {
		uintptr_t dst = (rd_start + 7) & ~7UL;
		volatile uint8_t *d = (volatile uint8_t *)dst;
		volatile const uint8_t *src = (volatile const uint8_t *)hit;

		con_hex2("PAYLOAD", "at=", hit, "to=", dst);
		for (uint64_t i = 0; i < rd_end - hit; i++)
			d[i] = src[i];
		rd_end = dst + (rd_end - hit);
		rd_start = dst;
	}
	struct payload *p = (struct payload *)rd_start;
	if (p->version != 1 || (p->adt_off & 7) || (rd_start & 7))
		return "bad payload header";

	uint64_t pa_of_va_base = p->phys_base - p->virt_base;	/* pa = va + this */
	con_hex2("PHYS", "base=", p->phys_base, "size=", p->mem_size);
	con_hex("VIRT", p->virt_base);
	con_hex("ENTRY", p->entry_va);

	/* The payload must not overlap where the kernel goes. */
	if (rd_end > p->phys_base && rd_start < p->phys_base + p->mem_size)
		return "ramdisk overlaps kernel memory";

	for (uint32_t i = 0; i < p->nsegs; i++) {
		const struct payload_seg *s = &p->segs[i];
		uintptr_t dst = s->vmaddr + pa_of_va_base;
		copy64(dst, rd_start + s->file_off, s->file_size);
		if (s->vmsize > s->file_size)
			zero64(dst + ((s->file_size + 7) & ~7UL), s->vmsize - ((s->file_size + 7) & ~7UL));
	}
	con_hex2("SEGS", "n=", p->nsegs, "end=", p->kernel_end + pa_of_va_base);

	/* ADT, then boot_args, behind the kernel; everything below topOfKernelData is ours. */
	uintptr_t adt_pa = p->kernel_end + pa_of_va_base;
	copy64(adt_pa, rd_start + p->adt_off, p->adt_size);
	clamp_memory(dtb, p, adt_pa);
	uintptr_t ba_pa = (adt_pa + p->adt_size + PAGE_16K - 1) & ~(PAGE_16K - 1);
	uintptr_t top = ba_pa + PAGE_16K;

	zero64(ba_pa, PAGE_16K);
	volatile struct boot_args *ba = (volatile struct boot_args *)ba_pa;
	ba->Revision = 2;
	ba->Version = 2;
	ba->virtBase = p->virt_base;
	ba->physBase = p->phys_base;
	ba->memSize = p->mem_size;
	ba->topOfKernelData = top;
	ba->Video.v_baseAddr = con_fb_addr();
	ba->Video.v_display = 1;
	ba->Video.v_rowBytes = FB_STRIDE;
	ba->Video.v_width = FB_W;
	ba->Video.v_height = FB_H;
	ba->Video.v_depth = 32;
	ba->deviceTreeP = adt_pa - pa_of_va_base;	/* xnu reads it with the MMU on: a VA */
	ba->deviceTreeLength = (uint32_t)p->adt_size;
	for (int i = 0; i < BOOT_LINE_LENGTH; i++)
		ba->CommandLine[i] = p->cmdline[i];
	ba->memSizeActual = p->mem_size;
	con_hex2("BOOTARG", "pa=", ba_pa, "top=", top);

	uintptr_t entry_pa = p->entry_va + pa_of_va_base;
	con_hex("JUMP", entry_pa);
	con_str("XNU", "jumping (fbcon clears screen)");

	/* Everything was written with the MMU and caches off; drop any stale I-cache lines too. */
	__asm__ volatile("dsb sy; ic iallu; dsb sy; isb" ::: "memory");
	((xnu_entry_t)entry_pa)(ba_pa);
	return "xnu returned";
}
