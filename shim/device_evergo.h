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
 *   timer:  arm,armv8-timer clock-frequency 13 MHz; vtimer PPI 11 (INTID 27),
 *           ptimer PPI 14 (INTID 30)
 *   memory: dtsi bank0 = 0x40000000 + 0x3e605000 (LK patches the real banks
 *           into /memory; the shim reads them at runtime and clamps)
 *   splash: the panel scans DISP OVL0 **layer 1**; its buffer lives at
 *           0x7d006000 (measured on the live device 2026-10-25, read from
 *           OVL0_L1_ADDR). The DTB's atag,videolfb-fb_base_l is NOT that layer.
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
#define VTIMER_INTID		27	/* PPI 11 (virtual timer) */
#define PTIMER_INTID		30	/* PPI 14 */
#define TIMEBASE_HZ		13000000

/* The panel's visible layer: DISP OVL0 layer 1. The shim reads its address and
 * byte pitch from the OVL0 L1 registers at boot (disp.c) and points the
 * console and XNU boot_args.Video there; this is only the pre-probe default.
 * Measured on the live device 2026-10-25: 0x7d006000 (NOT the DTB's
 * atag,videolfb-fb_base_l, and NOT 0x7e605000 = the old dts guess). */
#define FB_BASE_DEFAULT		0x7d006000UL

/* The XNU early console (patch 0081e) writes to this address with the MMU off
 * and then to a reserved kernel VA alias of the same physical block. It is a
 * compile-time constant inside the kernel, so if LK ever moves the visible
 * layer the kernel's address goes stale - loader.c prints FBCHK when the
 * runtime address differs from this one. Keep in sync with
 * xnu/patches/0081e-evergo-early-console-mtk-fb.patch. */
#define XNU_EARLY_FB		0x7d006000UL
/* 2 MiB-aligned block base the visible buffer lives in (0x7d006000 - 0x6000).
 * A block descriptor must be 2 MiB aligned, so the mapping uses this and the
 * console adds the 0x6000 offset. */
#define XNU_EARLY_FB_BLOCK	0x7d000000UL
#define XNU_EARLY_FB_OFFSET	0x6000UL
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


/* Persistent-RAM console (MediaTek ramoops) - OPT-IN debug channel.
 * When HAS_PSTORE is 1, the shim (and, via a companion XNU patch, XNU) append
 * boot logs to a DRAM zone that survives a warm reset and is recovered from
 * Linux after a normal boot:
 *   adb shell su -c 'cat /sys/fs/pstore/console-ramoops-0'
 * PSTORE_BASE/SIZE MUST be free DRAM, reserved in the Linux DTB as a ramoops
 * console-size region (see EVERGO.md). An address that collides with LK/XNU/
 * Linux memory corrupts the boot, so this stays OFF (0) by default. */
#ifndef HAS_PSTORE
#define HAS_PSTORE		0
#endif
#define PSTORE_BASE		0x00000000UL	/* set to a verified free DRAM address */
#define PSTORE_SIZE		0x00040000UL	/* 256 KiB console region */
