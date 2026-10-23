#pragma once
/*
 * Device configuration for the boot shim. Exactly one of DEVICE_TOPAZ /
 * DEVICE_EVERGO must be defined at build time (shim/build.sh -D...); the
 * per-device headers hold every address that is not generic arm64.
 */
#if defined(DEVICE_TOPAZ)
#include "device_topaz.h"
#elif defined(DEVICE_EVERGO)
#include "device_evergo.h"
#else
#error "define DEVICE_TOPAZ or DEVICE_EVERGO (shim/build.sh DEVICE=...)"
#endif

/* Generic arm64 defaults every device shares. */
#ifndef NR_CPUS
#define NR_CPUS			8
#endif
#define SGI_INTID		1	/* SWEET_IPI_SGI (patch 0004) */
#define GICR_PAGE		0x10000	/* 64K GICR page walk granularity */

#ifndef RESTART_BOOTLOADER
#define RESTART_BOOTLOADER	0x77665500U
#endif
