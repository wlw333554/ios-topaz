#pragma once
#include <stdint.h>
#include "device.h"

/* Splash framebuffer: device default, overridable at runtime (evergo reads
 * atag,videolfb-fb_base_l from the DTB /chosen before the first write). */
#ifndef FB_BASE
#define FB_BASE		FB_BASE_DEFAULT
#endif

#define RED	0xffff0000U
#define BLUE	0xff0000a0U
#define WHITE	0xffffffffU
#define YELLOW	0xffffff00U
#define GREEN	0xff00ff00U

extern volatile uint32_t *con_fb;

void con_set_fb(uint64_t addr);	/* before the first con_clear() */
uint64_t con_fb_addr(void);

void con_clear(uint32_t bg);
void con_skip(void);
void con_str(const char *label, const char *s);
void con_hex(const char *label, uint64_t v);
/* label, then "a=xxxxxxxx b=xxxxxxxx" style pairs of 32-bit values */
void con_hex2(const char *label, const char *a, uint32_t va, const char *b, uint32_t vb);
