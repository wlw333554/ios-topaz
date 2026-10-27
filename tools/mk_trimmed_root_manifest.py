#!/usr/bin/env python3
"""Pick the iOS root files that go into a small QEMU RAM-disk root, ported from
HakiOS' tools/select-system.py (github.com/oosexgang/hackios-xnu-kernel) to our
16 GiB HFS+ image listing (_files.json).

Why this exists: the QEMU virt RAM on this Windows host is host-backed
(memory-backend-ram only, no memory-backend-file), so a RAM-disk root
(patch 0091e, rd=ufsroot0) must be small enough to live in guest RAM, which is
capped by the ~5.6 GiB the host can give. Our full root is 10.87 GiB used; the
dyld shared cache alone is 3.11 GiB. This script applies HakiOS' trimming rules
to our tree and reports how small a bootable root can get, so we know whether it
fits QEMU before we spend a CI job building the image.

Output: _trimmed_keep.txt  (one path per line, the regular files to keep)
        stdout summary     (file count, total size, dyld-cache size, rest size)

The actual HFS+ image is then built on the Linux CI runner (hfsprogs mkfs.hfsplus)
from the files named here, extracted via the extents in _files.json.
"""
import json
import re
import stat
import sys

WHOLE = ('usr/libexec/', 'usr/sbin/', 'usr/bin/', 'usr/lib/', 'usr/share/', 'sbin/', 'bin/', 'private/etc/',
         'System/Library/', 'Applications/', 'Library/')
SKIP_SYSTEM_LIBRARY = ('LinguisticData', 'PreinstalledAssetsV2', 'NanoTimeKit', 'Audio',
                       'Assistant', 'AccessibilityBundles',
                       'VideoProcessors', 'ScreenReader', 'Photos', 'Carrier Bundles', 'NanoPreferenceBundles',
                       'ImagingNetworks', 'ProductDocuments', 'AppPlaceholders', 'UserNotifications',
                       'Caches')
FRAMEWORKS = ('System/Library/Frameworks/', 'System/Library/PrivateFrameworks/')
SMALL = 4 << 20
SKIP_EXT = ('.weights', '.weights_nonane', '.metallib', '.mov', '.pcm', '.scnz', '.espresso.net',
            '.espresso.shape', '.caf', '.m4a', '.aiff', '.mp4', '.heic', '.wav', '.art', '.scube',
            '.ds', '.dat', '.bin')
DATA_FRAMEWORKS = tuple(f'System/Library/{name}.framework/' for name in (
    'PrivateFrameworks/TextInputUI', 'PrivateFrameworks/CoreEmoji', 'Frameworks/CFNetwork'))
SKIP_APP_EXT = ('.mov', '.mp4', '.m4a', '.caf', '.wav', '.heic', '.aiff', '.metallib', '.scnz', '.usdz')
MAX_ASSET_CATALOG = 4 << 20
MAX_FONT = 25 << 20
TRIMMED_FONTS = ('System/Library/Fonts/CoreAddition/AppleColorEmoji-160px.ttc',
                 'System/Library/Fonts/LanguageSupport/PingFang.ttc')
MAX_STRING_TABLE = 16 << 20
STAGED_APPS = 'private/var/staged_system_apps/'
ALL_PICTURES = ('System/Library/PrivateFrameworks/ProgressUI.framework/',)
BIG_CATALOGS = ('System/Library/PrivateFrameworks/SFSymbols.framework/',)
HEALTH = 'System/Library/Health/'
SKIP_PREFIX = ('usr/share/tokenizer/', 'usr/share/firmware/', 'usr/share/mecabra/',
               'usr/standalone/', 'Library/Audio/', 'Library/Ringtones/',
               HEALTH + 'Assets/FitnessUIAssets.bundle/') + \
    tuple(f'System/Library/{name}/' for name in SKIP_SYSTEM_LIBRARY)
BIG_PROGRAM = 10 << 20
# Subtrees of System/Library we keep for an early-boot / reproduce-panic QEMU root.
# Everything else in System/Library is dropped: the dyld shared cache (3.11 GiB,
# resident in guest RAM) already provides the Frameworks/PrivateFrameworks binaries.
KEEP_SL = ('LaunchDaemons/', 'LaunchAgents/', 'CoreServices/', 'Frameworks/',
           'PrivateFrameworks/', 'Extensions/', 'Filesystems/', 'Preferences/',
           'Security/', 'xpc/', 'SystemConfiguration/', 'Accounts/', 'Keychains/')
AUDIO = ('Library/Audio/Plug-Ins/', 'Library/Audio/Tunings/D22/', 'Library/Audio/Tunings/Generic/',
         'System/Library/Audio/UISounds/')
SKIP_AUDIO = ('System/Library/Audio/UISounds/nano/',)
KEEP_LANG = ('en', 'Base', 'English', 'en_US', 'en_GB')
# our dyld shared cache hides under "HFS+ Private Data/iNode..." (3.11 GiB); keep it whole
DYLD_PREFIX = 'HFS+ Private Data/'


def wanted_file(path, size):
    lproj = re.search(r'/([^/]+)\.lproj/', path)
    if lproj and lproj.group(1) not in KEEP_LANG:
        return False
    if path.startswith(AUDIO):
        return not path.startswith(SKIP_AUDIO) and '.espresso.' not in path
    if path.startswith(SKIP_PREFIX):
        return False
    # --- System/Library: aggressive trim for the QEMU RAM-disk root ---------------
    if path.startswith('System/Library/'):
        if path.startswith('System/Library/Fonts/'):
            return path in TRIMMED_FONTS
        if (path.startswith('System/Library/Frameworks/') or
                path.startswith('System/Library/PrivateFrameworks/')):
            # The dyld shared cache already provides the framework binaries. Keep
            # only the .framework metadata (Info.plist) and tiny resources so dyld
            # can still validate the cache; drop the shared/large binaries.
            leaf = path.rsplit('/', 1)[-1]
            return leaf == 'Info.plist' or size <= 8 * 1024
        if any(path.startswith('System/Library/' + k) for k in KEEP_SL):
            return True
        return False
    if path.startswith(ALL_PICTURES) and path.lower().endswith('.png'):
        return True
    if path.startswith(('usr/libexec/', 'usr/sbin/')):
        return size <= BIG_PROGRAM
    if path.startswith('Applications/'):
        return False
    if path.startswith(FRAMEWORKS):
        if re.search(r'/(XPCServices|Support|Helpers)/', path):
            return size <= 32 << 20 and not path.lower().endswith(('.png', '.car', '.loctable', '.caml'))
        if path.endswith('.car'):
            return size <= MAX_ASSET_CATALOG or path.startswith(BIG_CATALOGS)
        if path.endswith('.loctable'):
            return size <= MAX_STRING_TABLE
        if path.startswith(DATA_FRAMEWORKS) and path.lower().endswith(('.dat', '.bin')):
            return size <= SMALL
        return size <= SMALL and not path.lower().endswith(SKIP_EXT) and '.mlmodelc/' not in path
    return path.startswith(WHOLE)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else 'E:/iOS/_files.json'
    out = sys.argv[2] if len(sys.argv) > 2 else 'E:/iOS/_trimmed_keep.txt'
    recs = json.load(open(src, encoding='utf-8', errors='replace'))

    meta = {}            # path -> (mode, size); trim NULs from the dyld paths
    for r in recs:
        path = r['path'].replace('\x00', '')
        mode = r.get('mode', 0)
        size = r.get('size', 0)
        # normalize: no leading slash, lowercase drive noise already absent
        if path.startswith('/'):
            path = path[1:]
        meta[path] = (mode, size)

    chosen = set()
    dyld_size = 0
    rest_size = 0
    nfiles = 0
    for path, (mode, size) in meta.items():
        if not stat.S_ISREG(mode):
            continue
        if path.startswith(DYLD_PREFIX):
            keep = True
        else:
            keep = wanted_file(path, size)
        if keep:
            chosen.add(path)
            nfiles += 1
            if path.startswith(DYLD_PREFIX):
                dyld_size += size
            else:
                rest_size += size

    # keep symlinks + every directory on the way to a chosen file
    for path in list(chosen):
        p = path
        while '/' in p:
            p = p.rsplit('/', 1)[0]
            chosen.add(p)

    # report the directories/symlinks count too (cheap to add as dirs)
    ndirs = sum(1 for p, (m, _) in meta.items() if stat.S_ISDIR(m) and p in chosen)

    with open(out, 'w', encoding='utf-8') as f:
        for p in sorted(chosen):
            if p in meta and stat.S_ISREG(meta[p][0]):
                f.write(p + '\n')

    total = dyld_size + rest_size
    print(f'selected {nfiles} regular files, {ndirs} directories')
    print(f'  dyld shared cache : {dyld_size/1073741824:.2f} GiB')
    print(f'  everything else  : {rest_size/1073741824:.2f} GiB')
    print(f'  TOTAL selected   : {total/1073741824:.2f} GiB  ({total/1048576:.0f} MiB)')
    print(f'  keep-list written: {out}')
    # feasibility note against the QEMU RAM ceiling (~5.6 GiB host, guest RAM must be <= host)
    # Host frees to ~6.6 GiB after closing apps; QEMU commits guest RAM in host
    # memory (no memory-backend-file), so guest RAM must be <= free host RAM.
    # -m 6G covers root_addr(~2.95G) + image, so image must be <= ~4.05 GiB =>
    # TOTAL selected <= ~3.8 GiB. The dyld cache alone is 3.11 GiB (immovable).
    ceiling = 3.8  # GiB total selected we can keep and still boot under -m 6G
    print(f'  QEMU -m 6G needs TOTAL selected <= {ceiling:.1f} GiB; '
          + ('FITS' if total/1073741824 <= ceiling else 'TOO BIG -- trim more / smaller cache'))


if __name__ == '__main__':
    main()
