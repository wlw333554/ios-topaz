#!/bin/bash
# One-shot evergo build: ADT -> payload -> shim boot image.
#
#   tools/build-evergo.sh KERNEL [RAMDISK_IMAGE]
#
# KERNEL      xnu Mach-O from the CI run (kernel.development.sweet)
# RAMDISK     optional root image (mkpayload --ramdisk, boot with rd=md0)
#
# Environment:
#   EXTRA_ADT     extra mkadt_evergo.py flags, e.g. "--highmem" (top DDR bank)
#   CMDLINE       boot-args (default: debug console, no root)
#   HEADER_VERSION Android boot image header version (default 3; MTK LK also
#                 accepts 4 - try 4 if 3 misbehaves)
set -euo pipefail
cd "$(dirname "$0")/.."

KERNEL="${1:?usage: tools/build-evergo.sh KERNEL [RAMDISK_IMAGE]}"
RAMDISK="${2:-}"

mkdir -p shim/out
python3 tools/mkadt_evergo.py shim/out/evergo.adt ${EXTRA_ADT:-}

if [ -n "$RAMDISK" ]; then
	python3 tools/mkpayload.py "$KERNEL" shim/out/evergo.adt shim/out/payload.bin \
		--ramdisk "$RAMDISK" \
		--cmdline "${CMDLINE:-debug=0xc keepsyms=1 -noprogress rd=md0}"
else
	python3 tools/mkpayload.py "$KERNEL" shim/out/evergo.adt shim/out/payload.bin \
		--cmdline "${CMDLINE:-debug=0xc keepsyms=1 -noprogress}"
fi

DEVICE=evergo HEADER_VERSION="${HEADER_VERSION:-3}" shim/build.sh shim/out/payload.bin
