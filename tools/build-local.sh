#!/bin/bash
# The topaz kernel built on a Mac (faster than CI for day-to-day work).
# Same steps as .github/workflows/xnu.yml: blacktop/darwin-xnu-build at a fixed commit, our
# patches next to its own, Xcode 15.4 (its iig matches the xnu-8796 IORPC ABI), KDK 13.5
# unpacked here instead of installed with sudo, ccache.
#   tools/build-local.sh        build this checkout's xnu/patches
# Work tree (build.sh clone, xnu clone, KDK, ccache): $XNU_BUILD_DIR, default ~/topaz-xnu-build.
# Output: $XNU_BUILD_DIR/out/kernel.development.sweet and out/SHA256SUMS.
set -euo pipefail
TREE=$(cd "$(dirname "$0")/.." && pwd)
ROOT=${XNU_BUILD_DIR:-$HOME/topaz-xnu-build}
mkdir -p "$ROOT"
cd "$ROOT"
DXB_SHA=663906528d1cd5ea8fde7de6f16de73db9511549

export DEVELOPER_DIR=/Applications/Xcode_15.4.app/Contents/Developer
export MACOS_VERSION=13.5 KERNEL_CONFIG=DEVELOPMENT MACHINE_CONFIG=SWEET
export KDKROOT=$ROOT/kdkpkg/KDK.pkg/Payload
export CCACHE_DIR=$ROOT/.ccache CCACHE_BASEDIR=$ROOT CCACHE_COMPILERCHECK=content
export CCACHE_SLOPPINESS=time_macros,include_file_mtime,include_file_ctime,file_macro,pch_defines CCACHE_MAXSIZE=3G
export CCACHE_WRAP=$ROOT/ccache-bin
export MAKEFLAGS=${MAKEFLAGS:--k}
export PATH=$ROOT/bin:/opt/homebrew/bin:$PATH

# The build script and the xnu clone are kept between builds (the clone took most of the
# first one); every build starts from the pristine build.sh and xnu tree with our patches
# copied in again, so they apply as on CI. FRESH=1 starts from nothing.
[ "${FRESH:-0}" = 1 ] && rm -rf dxb
if [ ! -d dxb/.git ]; then
	git init -q dxb
	git -C dxb fetch -q --depth 1 https://github.com/blacktop/darwin-xnu-build "$DXB_SHA"
fi
git -C dxb checkout -q -f FETCH_HEAD 2>/dev/null || git -C dxb checkout -q -f "$DXB_SHA"
git -C dxb clean -fdq patches
if [ -d dxb/xnu/.git ]; then
	git -C dxb/xnu reset -q --hard
	git -C dxb/xnu clean -fdq
fi
rm -rf dxb/fakeroot/System/Library/Kernels/kernel.* dxb/build/xnu.obj/kernel.*   # (build.sh skips a kernel it finds there)
cp "$TREE"/xnu/patches/*.patch dxb/patches/
cp "$TREE"/xnu/patches/*.sh dxb/patches/ 2>/dev/null || true
sed -i '' 's/ARCHS="x86_64 arm64e" VALID_ARCHS="x86_64 arm64e"/ARCHS="arm64" VALID_ARCHS="arm64" OTHER_CFLAGS="-mkernel -nostdinc -Wno-packed -mcpu=cortex-a53"/' dxb/build.sh
grep -q 'OTHER_CFLAGS="-mkernel -nostdinc -Wno-packed -mcpu=cortex-a53"' dxb/build.sh
sed -i '' 's|export CC="$(xcrun -find clang)"|export CC="$CCACHE_WRAP/clang"|; s|export CXX="$(xcrun -find clang++)"|export CXX="$CCACHE_WRAP/clang++"|' dxb/build.sh
grep -q 'export CC="$CCACHE_WRAP/clang"' dxb/build.sh

# build.sh only checks that xcodes exists; Xcode 15.4 is already in /Applications
mkdir -p bin "$CCACHE_WRAP"
printf '#!/bin/sh\nexit 0\n' > bin/xcodes
chmod +x bin/xcodes
for t in clang clang++; do
	# Assembly (.s) includes the generated assym.s, which ccache does not hash for
	# assembler input: a cached object kept stale structure offsets (TOPAZ_4K + AMP
	# moved machine.CpuDatap and machine_set_current_thread read the old one). No cache for it.
	printf '#!/bin/sh\nfor a; do case "$a" in *.s) exec %s "$@";; esac; done\nexec ccache %s "$@"\n' "$(xcrun -find $t 2>/dev/null)" "$(xcrun -find $t 2>/dev/null)" > "$CCACHE_WRAP/$t"
	chmod +x "$CCACHE_WRAP/$t"
done
xcodebuild -version 2>/dev/null | head -1 || true

ccache -z >/dev/null
(cd dxb && /opt/homebrew/bin/bash build.sh 2>&1 | tee ../build.log | grep -E "error:|\[ERROR\]|Building|DONE" || true)
ccache -s | grep -E "Hits|Misses" || true

rm -rf out && mkdir -p out
cp -R dxb/fakeroot/System/Library/Kernels/. out/ 2>/dev/null || true
test -s out/kernel.development.sweet || { echo "no kernel: see build.log" >&2; grep -E " error:" build.log | head -20 >&2; exit 1; }
(cd out && shasum -a 256 kernel.development.sweet > SHA256SUMS)
ls -la out
