#!/bin/bash
# Build the shim and wrap it in a boot.img.
#
#   DEVICE=topaz|evergo   device constants from shim/device_<name>.h (default topaz)
#   HEADER_VERSION=2|3|4  Android boot image header version (default 4; topaz ABL
#                         ignores v1/v2; MTK LK accepts 2/3/4 - use 3 if 4 fails)
#   MKBOOTIMG              path to AOSP mkbootimg.py
#
# topaz: ABL ignores v1/v2 images and drops the v4 boot ramdisk, so the XNU
# payload is appended to the Image after _end and the ramdisk stays empty.
# evergo: MTK LK passes the boot ramdisk in /chosen, so the payload rides in
# the (gzip) ramdisk; loader.c finds it there first, Image-embedded second.
set -euo pipefail
cd "$(dirname "$0")"

DEVICE="${DEVICE:-topaz}"
HEADER_VERSION="${HEADER_VERSION:-4}"
case "$DEVICE" in
topaz)  DEFS="-DDEVICE_TOPAZ" ;;
evergo) DEFS="-DDEVICE_EVERGO" ;;
*) echo "error: unknown DEVICE=$DEVICE (topaz|evergo)" >&2; exit 1 ;;
esac

CC=aarch64-linux-gnu-gcc
OBJCOPY=aarch64-linux-gnu-objcopy
CFLAGS="-O2 -ffreestanding -nostdlib -fno-pic -mgeneral-regs-only -mstrict-align -fno-stack-protector -Wall -Wextra $DEFS"
mkdir -p out
for f in start.S shim.c console.c mmu.c fdt.c loader.c ufs.c; do
	$CC $CFLAGS -c $f -o out/${f%.*}.o
done
# The image runs wherever the bootloader puts it with no relocation pass: only the
# header's image_size (start.o, linked at 0) may be an absolute address.
abs=$(aarch64-linux-gnu-readelf -r out/shim.o out/console.o out/mmu.o out/fdt.o out/loader.o out/ufs.o | grep -c ABS64 || true)
[ "$abs" = 0 ] || { echo "error: $abs absolute relocations in C objects" >&2; exit 1; }
$CC $CFLAGS -static -T shim.ld -Wl,--build-id=none -Wl,--no-warn-rwx-segments -Wl,-N \
	out/start.o out/shim.o out/console.o out/mmu.o out/fdt.o out/loader.o out/ufs.o -o out/shim.elf
$OBJCOPY -O binary out/shim.elf out/Image
# The bootloader copies the file; pad to image_size so .bss/stacks exist as zeroed bytes.
size=$((16#$(aarch64-linux-gnu-nm out/shim.elf | awk '$3=="_end"{print $1}')))
truncate -s "$size" out/Image
# Ramdisk: an xnu payload (tools/mkpayload.py) if given as $1, else an empty
# newc cpio (the bootloader dislikes a zero-size ramdisk on some builds)
# -> diagnostics.
if [ -n "${1:-}" ]; then
	if [ "$DEVICE" = "topaz" ]; then
		# topaz ABL drops the v4 boot ramdisk: append the payload to the Image at
		# the first 4K boundary after _end (loader.c) and grow image_size.
		truncate -s $(( (size + 0xfff) & ~0xfff )) out/Image
		cat "$1" >> out/Image
		python3 -c 'import os,struct,sys; f=open(sys.argv[1],"r+b"); f.seek(16); f.write(struct.pack("<Q", os.path.getsize(sys.argv[1])))' out/Image
		(cd out && rm -rf rd && mkdir rd && cd rd && find . | cpio -o -H newc --quiet | gzip -9 > ../ramdisk.bin)
	else
		# evergo: MTK LK passes the boot ramdisk, so the payload goes inside it.
		(cd out && rm -rf rd && mkdir rd && cd rd && cat "$1" > payload.bin && find . | cpio -o -H newc --quiet | gzip -9 > ../ramdisk.bin)
	fi
else
	(cd out && rm -rf rd && mkdir rd && cd rd && find . | cpio -o -H newc --quiet | gzip -9 > ../ramdisk.bin)
fi
MKBOOTIMG="${MKBOOTIMG:-python3 mkbootimg.py}"
$MKBOOTIMG --header_version "$HEADER_VERSION" --kernel out/Image --ramdisk out/ramdisk.bin \
	--os_version 12.0.0 --os_patch_level 2025-01 \
	--cmdline "console=null" -o out/shim-boot.img
# Host render of the console layout.
gcc -O1 -Wall -o out/host_test console.c host_test.c && ./out/host_test
ls -l out/Image out/shim-boot.img
