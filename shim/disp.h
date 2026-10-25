#pragma once
#include <stdint.h>

/*
 * Re-light the evergo panel after MTK LK blanks it at kernel hand-off, so the
 * shim console and the XNU early console become visible. See disp.c.
 */
void disp_takeover(void);
