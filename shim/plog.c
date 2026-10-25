/*
 * Persistent-RAM console ("ramoops console") writer — a MediaTek bring-up
 * debug channel that survives a warm reset and is recovered from Linux with
 *   adb shell su -c 'cat /sys/fs/pstore/console-ramoops-0'
 * instead of depending on the (sometimes invisible) framebuffer.
 *
 * On-disk layout matches the Linux kernel persistent_ram console zone:
 *   bytes  0..11 : u32 sig = 0x43474244 ("DBGC" == PERSISTENT_RAM_SIG)
 *                    u32 start, u32 size
 *   bytes 12..   : ring buffer of console bytes (cap = PSTORE_SIZE - 12)
 * Linux's ramoops recognises the sig and preserves the existing start/size,
 * so logs written by the shim (and, via a companion XNU patch, by XNU) before
 * a reset survive into the next Linux boot.
 *
 * ENABLED ONLY when HAS_PSTORE is set. PSTORE_BASE/SIZE must point at free
 * DRAM that survives reset and is reserved in the Linux device tree — see
 * EVERGO.md ("Persistent-RAM (ramoops) debug channel"). The shim and XNU must
 * agree on the address; a region that collides with LK/XNU/Linux memory will
 * corrupt the boot, so this stays OFF by default.
 */
#include "plog.h"
#include "console.h"
#include "font.h"

#if HAS_PSTORE

#ifndef PSTORE_BASE
#error "HAS_PSTORE is set but PSTORE_BASE is undefined"
#endif
#ifndef PSTORE_SIZE
#error "HAS_PSTORE is set but PSTORE_SIZE is undefined"
#endif

#define PRZ_SIG  0x43474244U   /* Linux PERSISTENT_RAM_SIG == "DBGC" */
#define PRZ_HDR  12U           /* sig(4) + start(4) + size(4) */

static volatile uint32_t *prz_hdr = (volatile uint32_t *)(uintptr_t)PSTORE_BASE;
static volatile uint8_t  *prz_buf = (volatile uint8_t  *)(uintptr_t)(PSTORE_BASE + PRZ_HDR);
static const uint32_t prz_cap = (uint32_t)(PSTORE_SIZE - PRZ_HDR);

void plog_init(void)
{
	/* A zone already stamped with the kernel sig holds logs from a prior boot
	 * (shim -> XNU -> reset). Keep them. Otherwise mark it fresh so Linux's
	 * ramoops identifies and recovers it. */
	if (prz_hdr[0] != PRZ_SIG) {
		prz_hdr[0] = PRZ_SIG;
		prz_hdr[1] = 0;   /* start */
		prz_hdr[2] = 0;   /* size  */
	}
}

static void prz_append(uint8_t b)
{
	/* Lazily stamp the kernel sig so Linux's ramoops recognises (and
	 * preserves) the zone on the next boot; once stamped it keeps the
	 * existing start/size across a warm reset. */
	if (prz_hdr[0] != PRZ_SIG) {
		prz_hdr[0] = PRZ_SIG;
		prz_hdr[1] = 0;   /* start */
		prz_hdr[2] = 0;   /* size  */
	}
	uint32_t start = prz_hdr[1];
	uint32_t size  = prz_hdr[2];
	uint32_t off   = (start + size) % prz_cap;

	prz_buf[off] = b;
	if (size < prz_cap)
		prz_hdr[2] = size + 1;        /* not yet full */
	else
		prz_hdr[1] = (start + 1) % prz_cap;  /* full: drop oldest */
}

void plog_putc(char c)
{
	prz_append((uint8_t)c);
}

void plog_puts(const char *s)
{
	while (*s)
		prz_append((uint8_t)*s++);
}

#endif /* HAS_PSTORE */

/*
 * Print the persistent-RAM zone: the kernel's fbcon mirrored its console there
 * (patch 0083e), so this is the previous boot's full log - which the fbcon's
 * own 75-line ring buffer would have scrolled away. Drawn 1:1 with the 8x16
 * font (the shim's own console uses 3x, which only fits 45 columns).
 */
int plog_dump(uint64_t base, uint64_t size, int y_px)
{
	const uint32_t sig = 0x43474244U;
	uint32_t cap, start, len, i;
	int lines = 0;

	if (!base || size < 4096)
		return 0;
	const volatile uint32_t *hdr = (const volatile uint32_t *)(uintptr_t)base;
	if (hdr[0] != sig)
		return 0;			/* no valid log from the previous boot */
	cap = (uint32_t)(size - 12);
	start = hdr[1];
	len = hdr[2];
	if (len > cap)
		len = cap;
	const volatile uint8_t *buf = (const volatile uint8_t *)(uintptr_t)(base + 12);

	uint64_t fb = con_fb_addr();
	uint32_t stride = con_rowbytes();
	if (!fb || !stride)
		return 0;
	volatile uint32_t *fbm = (volatile uint32_t *)(uintptr_t)fb;

	int cx = 0, cy = y_px;
	for (i = 0; i < len; i++) {
		char c = (char)buf[(start + i) % cap];
		if (c == '\r')
			continue;
		if (c == '\n' || cx + 8 > (int)FB_W) {
			cx = 0;
			cy += 16;
			if (cy + 16 > (int)FB_H)
				break;
			lines++;
			if (c == '\n')
				continue;
		}
		if (c < 0x20 || c > 0x7e)
			c = '?';
		const unsigned char *g = font8x16[c - 0x20];
		for (int gy = 0; gy < 16; gy++) {
			volatile uint32_t *line = fbm + (cy + gy) * (stride / 4) + cx;
			for (int gx = 0; gx < 8; gx++)
				line[gx] = (g[gy] & (0x80 >> gx)) ? 0xffffffffU : 0xff000000U;
		}
		cx += 8;
	}
	return lines;
}
