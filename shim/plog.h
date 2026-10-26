#pragma once
#include <stdint.h>

/* Persistent-RAM console ("ramoops console") — an alternate XNU/SHIM boot-log
 * channel that survives a warm reset and is read back from Linux via
 *   adb shell su -c 'cat /sys/fs/pstore/console-ramoops-0'
 * even when the framebuffer is blank or there is no serial adapter.
 *
 * The zone lives in ordinary DRAM at PSTORE_BASE (a region the device header
 * must reserve in the Linux DTB). It is OFF unless HAS_PSTORE is set.
 */
#ifndef HAS_PSTORE
#define HAS_PSTORE 0
#endif

/* Print the zone's content (the PREVIOUS boot's kernel log) with the 8x16
 * font at 2x, below y_px, showing only the TAIL of the log (the panic and
 * its cause are at the end). Returns the number of rows printed, or -1 for
 * a valid but empty zone. This is a READER: the shim never writes to the
 * zone, so the kernel's log (written by patch 0083e) survives untouched. */
int plog_dump(uint64_t base, uint64_t size, int y_px);

/* 1 if the dump just taken mentions a panic (loader.c holds the screen for
 * transcription in that case). Only meaningful right after plog_dump(). */
int plog_had_panic(void);

#if HAS_PSTORE
void plog_init(void);
void plog_putc(char c);
void plog_puts(const char *s);
#else
static inline void plog_init(void) {}
static inline void plog_putc(char c) { (void)c; }
static inline void plog_puts(const char *s) { (void)s; }
#endif
