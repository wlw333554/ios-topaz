#pragma once
/*
 * evergo: Xiaomi Redmi Note 11 5G (21091116AC), MediaTek MT6833 (Dimensity 810).
 * 6x Cortex-A55 @ MPIDR 0x000-0x005 + 2x Cortex-A76 @ MPIDR 0x100-0x101,
 * all ARMv8.2 with the 16K granule, so the shim runs everything on the boot
 * core (MTK LK starts CPU0, an A55) and XNU uses all eight cores.
 *
 * Facts from the evergo live device tree (mt6833.dts / evergo.dts,
 * Xiaomi-MT6833/kernel_xiaomi_evergo branch R):
 *   gic:    GICD 0x0c000000 (0x40000), GICR 0x0c040000 (0x200000)
 *   timer:  arm,armv8-timer clock-frequency 13 MHz; vtimer PPI 13 (INTID 29),
 *           ptimer PPI 14 (INTID 30)
 *   memory: dtsi bank0 = 0x40000000 + 0x3e605000 (LK patches the real banks
 *           into /memory; the shim reads them at runtime and clamps)
 *   splash: atag,videolfb-fb_base_l = 0x7e605000, vram 0x1be0000, 60 fps
 *   ufs:    mediatek,ufshci @ 0x11270000 (0x2300), MPHY @ 0x11fa0000
 *   touch:  Novatek NT36672C on spi1 @ 0x11010000 (RST gpio13, IRQ gpio14)
 *   wdt:    TOP_RGU_BASE 0x10007000
 */
#define DEVICE_NAME		"evergo"

/* MTK resets go through PSCI only (ATF); no QC IMEM/PSHOLD registers. */
#define IMEM_RESTART_REASON	0
#define PSHOLD			0

#define GICD_BASE		0x0c000000UL
#define GICR_BASE		0x0c040000UL
#define GICR_SIZE		0x200000UL
#define VTIMER_INTID		29	/* PPI 13 */
#define PTIMER_INTID		30	/* PPI 14 */
#define TIMEBASE_HZ		13000000

/* MTK LK leaves the splash framebuffer in /chosen (atag,videolfb-*); the
 * default matches the evergo dts. */
#define FB_BASE_DEFAULT		0x7e605000UL
#define FB_W			1080
#define FB_H			2400
#define FB_STRIDE		(FB_W * 4)
#define FB_FROM_DTB		1	/* read atag,videolfb-fb_base_l at boot */

/* MTK LK boots CPU0 (A55): ARMv8.2 with the 16K granule, no hand-off. */
#define NEEDS_GOLD_HANDOFF	0
#define BOOT_CPU_MPIDR		0x0UL
#define N_CORES			8
#define CPU_MPIDR(i)		((i) < 6 ? (uint64_t)(i) : (0x100UL | ((i) - 6)))

/* No QC cpufreq-hw block: clocks stay wherever MTK LK left them. */
#define HAS_CPUFREQ_HW		0

/* MTK ufshci@11270000. Linux drives it through the standard ufshcd core
 * (ufs-mediatek.c only adds vendor PHY/clock ops), so the register offsets
 * are probably standard UFSHCI - but that is unverified on evergo.
 *
 * HAS_UFS_PROBE is OPT-IN and off by default: with it on, shim.c runs the
 * probe after loading the payload, keeps the result on screen for 3 minutes,
 * then resets - it does NOT jump to XNU. So it produces a UFS-diagnostic
 * image, not a bootable one. Flip to 1 only to capture the dump, then back.
 *
 * The probe is read-only (UFS_PROBE_TAKEOVER 0): it dumps CAP/VER/HCS/HCE and
 * never writes UTRLBA/UTRLRSR or issues SCSI, because writing a wrong MTK
 * register map could permanently brick the UFS device. Set TAKEOVER=1 only
 * after a sane dump (cap != 0/0xffffffff, HCS link-up). */
#define HAS_UFS_PROBE		0
#define UFS_PROBE_BASE		0x11270000UL
#define UFS_PROBE_TAKEOVER	0

/* MTK watchdog (TOP_RGU). LK arms it and XNU has no driver to kick it, so a
 * hang or panic resets the phone ~15s in, wiping the panic screen before it
 * can be read. Disable it (MTK_WDT_MODE key, ENABLE cleared) just before the
 * XNU jump so panics stay on screen. */
#define HAS_WDT_DISABLE		1
#define WDT_BASE		0x10007000UL

/* DISP OVL0 layer-1 source address + pitch. The color-hunt diagnostic (v7)
 * proved the panel actually scans OVL0 **layer 1** (filling it turned the whole
 * screen green), NOT layer 0. mtk_disp_ovl.h (mt6833): L1_ADDR = 0x0f60,
 * L1_PITCH = 0x064, per-layer stride 0x20, OVL0 base 0x14005000. The shim reads
 * L1's address (the real visible buffer) and its byte pitch, and points the
 * console + XNU boot_args.Video there. */
#define HAS_DISP_FB_PROBE	1
#define DISP_OVL0_L1_ADDR	0x14005f60UL
#define DISP_OVL0_L1_PITCH	0x14005064UL

/* MTK LK may not put linux,initrd-* in /chosen, but it still decompresses the
 * boot ramdisk to the header ramdisk_addr. If the /chosen search finds no
 * SWEETXNU payload, loader.c scans this known address as a fallback. */
#define HAS_RAMDISK_FALLBACK	1
#define RAMDISK_LOAD_ADDR	0x51100000UL
#define RAMDISK_SCAN_LEN	0x2000000UL	/* 32 MiB is plenty for our cpio */

/* DISP takeover: LK blanks the panel (stops OVL scanout + turns the backlight
 * off) at kernel hand-off, so the shim console and XNU early console are
 * invisible. disp.c re-enables OVL0 (layer 0 points at our console buffer)
 * and re-pulses the backlight GPIO. The DISP MUTEX / DSI / panel are left
 * configured by LK and are NOT re-initialised here (next lever if still black).
 * Backlight enable = lcm-bl-enable-gpios = <&pio 87 0>. */
#define HAS_DISP_TAKEOVER	1
#define BL_GPIO_PIN		87
