/*
 * Just enough flattened-device-tree parsing to find what the bootloader put
 * in /chosen (initrd location, MTK splash framebuffer) and the /memory banks.
 * Big-endian, 4-byte aligned tokens.
 */
#include "fdt.h"

#define FDT_MAGIC	0xd00dfeed
#define FDT_BEGIN_NODE	1
#define FDT_END_NODE	2
#define FDT_PROP		3
#define FDT_NOP		4
#define FDT_END		9

static uint32_t be32(uintptr_t a)
{
	return __builtin_bswap32(*(volatile uint32_t *)a);
}

static uint64_t be64(uintptr_t a)
{
	return ((uint64_t)be32(a) << 32) | be32(a + 4);
}

static int streq(const char *a, const char *b)
{
	while (*a && *a == *b)
		a++, b++;
	return *a == *b;
}

static int name_is(const char *name, const char *want)
{
	/* exact match or "want@..." unit-address form */
	while (*want && *want == *name)
		want++, name++;
	return *want == '\0' && (*name == '\0' || *name == '@');
}

static uintptr_t align4(uintptr_t a)
{
	return (a + 3) & ~3UL;
}

static uintptr_t skip_str(uintptr_t p)
{
	/* byte loads are fine on Device memory as long as they are aligned to 1 */
	while (*(volatile char *)p)
		p++;
	return p + 1;
}

int fdt_chosen_u64(uintptr_t fdt, const char *prop, uint64_t *out)
{
	if (!fdt || be32(fdt) != FDT_MAGIC)
		return 0;
	uintptr_t p = fdt + be32(fdt + 8);		/* off_dt_struct */
	uintptr_t strings = fdt + be32(fdt + 12);	/* off_dt_strings */
	int depth = 0, in_chosen = 0;

	for (;;) {
		uint32_t tok = be32(p);
		p += 4;
		switch (tok) {
		case FDT_BEGIN_NODE: {
			const char *name = (const char *)p;
			depth++;
			in_chosen = depth == 2 && name_is(name, "chosen");
			p = align4(skip_str(p));
			break;
		}
		case FDT_END_NODE:
			if (in_chosen)
				return 0;
			depth--;
			break;
		case FDT_PROP: {
			uint32_t len = be32(p), nameoff = be32(p + 4);
			uintptr_t val = p + 8;
			if (in_chosen && streq((const char *)(strings + nameoff), prop)) {
				if (len == 4)
					*out = be32(val);
				else if (len == 8)
					*out = be64(val);
				else
					return 0;
				return 1;
			}
			p = align4(val + len);
			break;
		}
		case FDT_NOP:
			break;
		default:	/* FDT_END or garbage */
			return 0;
		}
	}
}

int fdt_memory_banks(uintptr_t fdt, uint64_t *base, uint64_t *size, int max)
{
	if (!fdt || be32(fdt) != FDT_MAGIC)
		return 0;
	uintptr_t p = fdt + be32(fdt + 8);		/* off_dt_struct */
	int depth = 0, in_memory = 0, nbanks = 0;
	uint32_t ac = 2, sc = 1;			/* root defaults; MTK dts uses 2/2 */

	for (;;) {
		uint32_t tok = be32(p);
		p += 4;
		switch (tok) {
		case FDT_BEGIN_NODE: {
			const char *name = (const char *)p;
			depth++;
			/* /memory is a top-level node (root = depth 1) */
			in_memory = depth == 2 && name_is(name, "memory");
			p = align4(skip_str(p));
			break;
		}
		case FDT_END_NODE:
			if (in_memory)
				return nbanks;
			depth--;
			break;
		case FDT_PROP: {
			uint32_t len = be32(p), nameoff = be32(p + 4);
			uintptr_t val = p + 8;
			if (depth == 1) {
				/* root #address-cells / #size-cells set the reg encoding */
				const char *s = (const char *)(fdt + be32(fdt + 12) + nameoff);
				if (streq(s, "#address-cells") && len == 4)
					ac = be32(val);
				else if (streq(s, "#size-cells") && len == 4)
					sc = be32(val);
			}
			if (in_memory) {
				uint32_t cells = (ac + sc) * 4;
				for (uint32_t off = 0; off + cells <= len && nbanks < max;
				     off += cells) {
					uint64_t b = 0, sz = 0;
					for (uint32_t c = 0; c < ac; c++)
						b = (b << 32) | be32(val + off + c * 4);
					for (uint32_t c = 0; c < sc; c++)
						sz = (sz << 32) | be32(val + off + ac * 4 + c * 4);
					base[nbanks] = b;
					size[nbanks] = sz;
					nbanks++;
				}
			}
			p = align4(val + len);
			break;
		}
		case FDT_NOP:
			break;
		default:	/* FDT_END or garbage */
			return nbanks;
		}
	}
}
