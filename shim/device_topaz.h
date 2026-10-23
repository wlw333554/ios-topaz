#pragma once
/*
 * topaz: Xiaomi Redmi Note 12 4G (tapas), Qualcomm SM6225 "khaje".
 * ABL starts on a Kryo 260 Silver (Cortex-A53 class) core that has no 16K
 * translation granule, so the shim hands off to the Gold (A73) cluster and
 * XNU only ever runs there.
 */
#define DEVICE_NAME		"topaz"

/* qcom,msm-imem@c125000 restart_reason@65c + PSHOLD for the reset path */
#define IMEM_RESTART_REASON	0x0c12565cUL
#define PSHOLD			0x0440b000UL

#define GICD_BASE		0x0f200000UL
#define GICR_BASE		0x0f300000UL
#define GICR_SIZE		0x100000UL
#define VTIMER_INTID		19	/* DT: <GIC_PPI 3 ...> */
#define PTIMER_INTID		18	/* DT: <GIC_PPI 2 ...> */
#define TIMEBASE_HZ		19200000

/* reserved-memory cont_splash_region left lit by ABL */
#define FB_BASE_DEFAULT		0x5c000000UL
#define FB_W			1080
#define FB_H			2400
#define FB_STRIDE		(FB_W * 4)

/* ABL boots on Silver CPU0; XNU needs 16K pages -> Gold CPU 0x100. */
#define NEEDS_GOLD_HANDOFF	1
#define BOOT_CPU_MPIDR		0x100UL
#define N_CORES			4
#define CPU_MPIDR(i)		(((uint64_t)(i) >> 2) << 8 | ((i) & 3))

/* cpufreq-hw: freq-domain1 0xf523000, LUT +0x110, PERF_STATE +0x920 */
#define HAS_CPUFREQ_HW		1
#define CPUFREQ_BASE		0x0f523000UL

/* Qualcomm UFSHCI at ufs_mem 0x4804000 (shim/ufs.c probe, no payload only) */
#define HAS_UFS_PROBE		1
#define UFS_PROBE_BASE		0x04804000UL
