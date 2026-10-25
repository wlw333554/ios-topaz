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

#if HAS_PSTORE
void plog_init(void);
void plog_putc(char c);
void plog_puts(const char *s);
#else
static inline void plog_init(void) {}
static inline void plog_putc(char c) { (void)c; }
static inline void plog_puts(const char *s) { (void)s; }
#endif
