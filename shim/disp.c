/*
 * evergo DISP  (v6) — do NOT fight the panel (mirror the ruby bring-up).
 *
 * LK leaves the whole display chain RUNNING. The shim does NO display-hardware
 * work: no MUTEX re-arm, no OVL repoint, no panel GPIO, no backlight/PWM. The
 * screen shows whatever is in the LK "visible" scan layer.
 *
 * The v7 color hunt proved that layer is OVL0 **layer 1** (filling it turned the
 * whole screen green), NOT layer 0 and NOT the /chosen videolfb base. So
 * shim_main's fb probe now reads OVL0 L1's address + byte pitch and points the
 * console (and XNU boot_args.Video) there; this file only prints read-back
 * diagnostics and returns, letting the console text and XNU's IOMFB show.
 *
 * The old hardware sequence is kept below behind DISP_HW_TAKEOVER (default 0)
 * so it can be restored in one edit if evergo's LK ever turns out to blank the
 * panel (it does not — the boot logo keeps showing).
 *
 * Runs MMU-off (physical addresses). Device memory -> stay 4-byte aligned.
 */
#include <stdint.h>
#include "device.h"
#include "console.h"
#include "disp.h"

#ifndef HAS_DISP_TAKEOVER
#define HAS_DISP_TAKEOVER 0
#endif

#ifndef DISP_HW_TAKEOVER
#define DISP_HW_TAKEOVER 0     /* 0 = do NOT touch display hardware (ruby way) */
#endif

#if HAS_DISP_TAKEOVER

#define R32(a) (*(volatile uint32_t *)(uintptr_t)(a))
#define W32(a, v) (*(volatile uint32_t *)(uintptr_t)(a) = (uint32_t)(v))

#define OVL_BASE   0x14005000UL
#define OVL_ADDR(n) (0x0f40 + 0x20 * (n))
#define OVL_PITCH(n)(0x044 + 0x20 * (n))

#if DISP_HW_TAKEOVER
/* ===== Hardware takeover (v4/v5) — OFF by default. ===== */
#define OVL_EN 0x000C
#define OVL_RST 0x0014
#define OVL_ROI_SIZE 0x0020
#define OVL_SRC_CON 0x002c
#define OVL_CON(n) (0x0030 + 0x20 * (n))
#define OVL_SRC_SIZE(n) (0x038 + 0x20 * (n))
#define OVL_RDMA_CTRL(n) (0x0c0 + 0x20 * (n))
#define OVL_FMT_ARGB8888 0x00003000UL
#define OVL_CON_AEN 0x00000100UL
#define OVL_CON_ALPHA 0x000000ffUL
#define OVL_BYPASS_SHADOW (1UL << 22)
#define MUTEX_BASE 0x14001000UL
#define MUTEX_EN(n) (0x020 + 0x20 * (n))
#define MUTEX_MOD0(n) (0x030 + 0x20 * (n))
#define MUTEX_SOF(n) (0x02C + 0x20 * (n))
#define MUTEX_MOD0_PRIMARY 0x45B5UL
#define MUTEX_SOF_DSI0 0x41UL
#define PIO_BASE 0x10005000UL
#define PWM0_BASE 0x1100e000UL
#define PWM_EN 0x00
#define PANEL_PM_ENABLE 136
#define PANEL_BIAS_ENP 137
#define PANEL_BIAS_ENN 138

static void spin_delay(volatile unsigned long loops)
{ volatile unsigned long i = 0; while (i++ < loops) { } }
static void pio_set_out_high(unsigned pin)
{
	unsigned doff = (pin % 8) * 4, b = pin % 32;
	uint32_t m = R32(PIO_BASE + 0x300UL + (pin/8)*0x10UL); m &= ~(0xfUL<<doff);
	W32(PIO_BASE + 0x300UL + (pin/8)*0x10UL, m);
	uint32_t d = R32(PIO_BASE + (pin/32)*0x10UL); d |= (1UL<<b);
	W32(PIO_BASE + (pin/32)*0x10UL, d);
	uint32_t o = R32(PIO_BASE + 0x100UL + (pin/32)*0x10UL); o |= (1UL<<b);
	W32(PIO_BASE + 0x100UL + (pin/32)*0x10UL, o);
}
static void disp_mutex_resume(void)
{
	W32(MUTEX_BASE + MUTEX_MOD0(0), 0);
	W32(MUTEX_BASE + MUTEX_MOD0(0), (uint32_t)MUTEX_MOD0_PRIMARY);
	W32(MUTEX_BASE + MUTEX_SOF(0), (uint32_t)MUTEX_SOF_DSI0);
	W32(MUTEX_BASE + MUTEX_EN(0), 1);
}
static void disp_pwm0_max(void)
{
	W32(PWM0_BASE + 0x18, 0); W32(PWM0_BASE + 0x1C, 0x03FF03FF);
	W32(PWM0_BASE + PWM_EN, 1); W32(PWM0_BASE + 0x0C, 1); W32(PWM0_BASE + 0x0C, 0);
}
#endif

/* v6: read-only. Report the visible-layer buffer the console is drawing into
 * (shim_main set it from OVL0 L1) plus read-backs; then return so XNU boots. */
void disp_takeover(void)
{
	uint64_t fb = con_fb_addr();
	uint32_t l1    = R32(OVL_BASE + OVL_ADDR(1));   /* what OVL0 L1 scans */
	uint32_t pitch = R32(OVL_BASE + OVL_PITCH(1));  /* LK's real L1 pitch */

#if DISP_HW_TAKEOVER
	disp_mutex_resume();
	pio_set_out_high(PANEL_PM_ENABLE); spin_delay(100000UL);
	pio_set_out_high(PANEL_BIAS_ENP); pio_set_out_high(PANEL_BIAS_ENN);
	spin_delay(2000000UL); disp_pwm0_max(); pio_set_out_high(BL_GPIO_PIN);
	spin_delay(500000000UL);
	W32(OVL_BASE + OVL_ROI_SIZE, ((uint32_t)FB_H << 16) | (uint32_t)FB_W);
	W32(OVL_BASE + OVL_ADDR(1), (uint32_t)fb);
	W32(OVL_BASE + OVL_PITCH(1), (uint32_t)con_rowbytes());
	W32(OVL_BASE + OVL_SRC_SIZE(1), ((uint32_t)FB_H << 16) | (uint32_t)FB_W);
	W32(OVL_BASE + OVL_CON(1), OVL_FMT_ARGB8888 | OVL_CON_AEN | OVL_CON_ALPHA);
	W32(OVL_BASE + OVL_RDMA_CTRL(1), 1);
	W32(OVL_BASE + OVL_SRC_CON, R32(OVL_BASE + OVL_SRC_CON) | 2);
	spin_delay(2000000UL);
#endif

	con_hex2("DISP", "fb=", (uint32_t)fb, "l1=", l1);
	con_hex2("DISP", "pitch=", pitch, "rowb=", con_rowbytes());
}

#endif /* HAS_DISP_TAKEOVER */
