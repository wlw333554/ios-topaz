#pragma once
#include <stdint.h>

/* Bootloader DTB helpers (big-endian FDT, what LK/ABL passes in x0). */
int fdt_chosen_u64(uintptr_t fdt, const char *prop, uint64_t *out);

/*
 * Up to `max` DDR banks from the top-level /memory node (the bootloader
 * patches the real detected sizes in). Returns the bank count; bases/sizes
 * are filled in ascending order. MTK LK lists one bank per contiguous range
 * (e.g. 0x40000000+~1G, 0x80000000+2G, 0x1_40000000+...).
 */
int fdt_memory_banks(uintptr_t fdt, uint64_t *base, uint64_t *size, int max);
