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
 * READER: print the persistent-RAM zone — the previous boot's full kernel
 * log, mirrored there by the kernel's fbcon (patch 0083e) and therefore
 * immune to the fbcon's own ring buffer scrolling it away.
 *
 * evergo bring-up: drawn at 2x (16x32 cells) so it can be read off the panel
 * without a camera, and only the TAIL is shown. The panic line and the error
 * that provoked it sit at the END of the log, and at 1:1 more than 100 lines
 * fit on the panel, so the interesting part used to fall off the bottom of
 * the screen. At 2x the panel holds ~49 rows of 67 columns below the shim's
 * summary, which covers the panic, its backtrace and the failing command's
 * own error above it.
 *
 * plog_had_panic() lets the caller (loader.c) hold this on screen for
 * transcription before jumping to XNU.
 */
#define PLOG_SCALE		2
#define PLOG_CELL_W		(8 * PLOG_SCALE)
#define PLOG_CELL_H		(16 * PLOG_SCALE)

static int plog_panic_seen;

int plog_had_panic(void)
{
	return plog_panic_seen;
}

/* Logical byte i of the ring: the len live bytes sit at start..start+len-1. */
static char ring_ch(const volatile uint8_t *buf, uint32_t cap, uint32_t start, uint32_t i)
{
	return (char)buf[(start + i) % cap];
}

/* Case-insensitive substring scan over the logical ring bytes [0, len). */
static int ring_contains_ci(const volatile uint8_t *buf, uint32_t cap, uint32_t start,
			    uint32_t len, const char *needle)
{
	uint32_t nl = 0, i, j;

	while (needle[nl])
		nl++;
	if (!nl || len < nl)
		return 0;
	for (i = 0; i + nl <= len; i++) {
		for (j = 0; j < nl; j++) {
			char a = ring_ch(buf, cap, start, i + j);
			char b = needle[j];
			if (a >= 'A' && a <= 'Z')
				a = (char)(a + 32);
			if (b >= 'A' && b <= 'Z')
				b = (char)(b + 32);
			if (a != b)
				break;
		}
		if (j == nl)
			return 1;
	}
	return 0;
}

/* Next wrapped row of the logical log: [rs, re), the terminating '\n'
 * included in re. Hard-wraps at cols columns. 0 at the end of the log. */
static int next_row(const volatile uint8_t *buf, uint32_t cap, uint32_t start,
		    uint32_t len, uint32_t *pos, uint32_t *rs, uint32_t *re, int cols)
{
	uint32_t i = *pos, s;
	int c = 0;

	if (i >= len)
		return 0;
	s = i;
	while (i < len) {
		char ch = ring_ch(buf, cap, start, i);
		i++;
		if (ch == '\n')
			break;
		if (++c >= cols) {
			/* hard wrap: swallow a newline sitting exactly on the
			 * boundary so the wrap leaves no phantom empty row */
			if (i < len && ring_ch(buf, cap, start, i) == '\n')
				i++;
			break;
		}
	}
	*pos = i;
	*rs = s;
	*re = i;
	return 1;
}

/* One glyph at PLOG_SCALE, white on black (the fbcon's colours). The caller
 * guarantees the cell fits on the panel. */
static void put2x(volatile uint32_t *fbm, uint32_t stride, int px, int py, char ch)
{
	const unsigned char *g;
	int gy, gx, sy, sx;

	if (ch < 0x20 || ch > 0x7e)
		ch = '?';
	g = font8x16[ch - 0x20];
	for (gy = 0; gy < 16; gy++) {
		volatile uint32_t *line = fbm + (py + gy * PLOG_SCALE) * (stride / 4) + px;
		for (gx = 0; gx < 8; gx++) {
			uint32_t c = (g[gy] & (0x80 >> gx)) ? 0xffffffffU : 0xff000000U;
			for (sy = 0; sy < PLOG_SCALE; sy++) {
				volatile uint32_t *p = line + sy * (stride / 4) + gx * PLOG_SCALE;
				for (sx = 0; sx < PLOG_SCALE; sx++)
					p[sx] = c;
			}
		}
	}
}

int plog_dump(uint64_t base, uint64_t size, int y_px)
{
	const uint32_t sig = 0x43474244U;
	const volatile uint32_t *hdr;
	const volatile uint8_t *buf;
	volatile uint32_t *fbm;
	uint32_t cap, start, len, stride, pos, rs, re, total, skip, idx, drawn;
	int cols, max_rows;

	if (!base || size < 4096)
		return 0;
	hdr = (const volatile uint32_t *)(uintptr_t)base;
	if (hdr[0] != sig)
		return 0;			/* no valid log from the previous boot */
	cap = (uint32_t)(size - 12);
	start = hdr[1];
	len = hdr[2];
	if (len > cap)
		len = cap;
	buf = (const volatile uint8_t *)(uintptr_t)(base + 12);

	uint64_t fb = con_fb_addr();
	stride = con_rowbytes();
	if (!fb || !stride)
		return 0;
	fbm = (volatile uint32_t *)(uintptr_t)fb;

	cols = (int)FB_W / PLOG_CELL_W;
	max_rows = ((int)FB_H - y_px) / PLOG_CELL_H - 1;	/* one row for the header */
	if (cols < 8 || max_rows < 1)
		return 0;

	plog_panic_seen = ring_contains_ci(buf, cap, start, len, "panic");

	/* Black backdrop: the screen was cleared to red and the glyph blit only
	 * paints the cells it writes. */
	con_block(0, y_px, FB_W, FB_H - y_px, 0xff000000U);

	/* Header: say what this is, and — when the previous boot panicked —
	 * that the screen is being held for transcription. */
	{
		const char *htxt = plog_panic_seen
			? "== prev boot PANICKED - held for reading =="
			: "== previous boot log (tail, 2x) ==";
		int px = 0;
		while (*htxt) {
			put2x(fbm, stride, px, y_px, *htxt++);
			px += PLOG_CELL_W;
		}
	}

	/* Count the wrapped rows, then render only the last max_rows of them:
	 * the tail is where the panic and its cause are. */
	total = 0;
	pos = 0;
	while (next_row(buf, cap, start, len, &pos, &rs, &re, cols))
		total++;
	skip = total > (uint32_t)max_rows ? total - (uint32_t)max_rows : 0;

	drawn = 0;
	idx = 0;
	pos = 0;
	while (next_row(buf, cap, start, len, &pos, &rs, &re, cols)) {
		if (idx >= skip) {
			int px = 0;
			uint32_t i;
			for (i = rs; i < re; i++) {
				char ch = ring_ch(buf, cap, start, i);
				if (ch == '\r' || ch == '\n')
					continue;
				put2x(fbm, stride, px,
				      y_px + PLOG_CELL_H * (int)(drawn + 1), ch);
				px += PLOG_CELL_W;
			}
			drawn++;
		}
		idx++;
	}
	return total ? (int)drawn : -1;	/* -1: valid zone, empty log */
}
