#!/usr/bin/env python3
"""Build a trimmed HFS+ root image for QEMU RAM-disk boot (patch 0091e, rd=ufsroot0).

Runs on Linux only (a CI runner or WSL2) where we have:
  - hfsprogs   (mkfs.hfsplus, fsck.hfsplus)
  - a kernel that can mount HFS+ read-write via loop

Why walk the mounted source instead of reading _files.json
--------------------------------------------------------
The trimming rules (ported from HakiOS' select-system.py) decide which files to
keep.  They only need each file's PATH and SIZE, both of which we get for free by
os.scandir-ing the mounted 16 GiB source.  That keeps this script self-contained:
CI just needs the source image, this script, mk_trimmed_root_manifest.py (the rule
table) and sweet-root-tweaks.py -- no precomputed catalog dump.

What stays (see mk_trimmed_root_manifest.wanted_file / DYLD_PREFIX):
  - the dyld shared cache: everything under "HFS+ Private Data/"  (3.12 GiB, mandatory)
  - the regular system trees (usr/, System/, sbin/, bin/, Library/,
    private/etc/ ...) gated by HakiOS' size/extension/language rules
  - symlinks inside the standard system trees (so /bin -> /usr/bin etc. resolve)
  - EVERY top-level symlink unconditionally: on iOS these are etc/tmp/var ->
    private/... and dropping them makes the trimmed root diverge from the phone
  - every directory on the path to a kept file

Flow:
  dd        -> size the output file
  mkfs.hfsplus -> empty HFS+
  mount src ro / mount out rw (loop)
  walk src -> select -> mkdir dirs, recreate symlinks, copy regular files
  python3 sweet-root-tweaks.py /mnt/out      (launchd.plist, containers, audio, prefs)
  fsck.hfsplus -fy out

Usage:
  build_trimmed_root.py SRC_IMG OUT_IMG [--size-mib N] [--tweaks PATH]
                         [--no-tweaks] [--src-mount /mnt/src] [--tgt-mount /mnt/tgt]
                         [--min-mib 4096]
"""
import argparse
import os
import shutil
import stat
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mk_trimmed_root_manifest as M   # WHOLE / SKIP_* / wanted_file / DYLD_PREFIX

# Where the dyld shared cache lives when it is stored as ORDINARY FILES (a root
# rebuilt from the IPSW DMGs) rather than as HFS+ private-data specials. The
# Linux hfsplus driver does not expose those specials at all, so HakiOS'
# DYLD_PREFIX ("HFS+ Private Data/") silently contributed 0 bytes on CI -- run
# 9's selection totalled 0.62 GiB instead of ~3.7 GiB. Without the cache nothing
# under /usr/lib or any framework can launch, so keep this tree unconditionally.
DYLD_CACHE_PREFIXES = ('System/Library/Caches/com.apple.dyld/',
                       'System/Library/dyld/')


def log(*a):
    print("[build_trimmed_root]", *a, flush=True)


def report_dyld_locations(src):
    """Log where the dyld shared cache actually lives on this source mount.

    Without this the cache can be dropped silently and the run still "succeeds",
    producing a small root that cannot boot at all -- so make it visible.
    """
    for p in (M.DYLD_PREFIX.rstrip('/'),) + tuple(x.rstrip('/') for x in DYLD_CACHE_PREFIXES):
        full = os.path.join(src, p)
        if not os.path.isdir(full):
            log("dyld cache location MISSING:", p)
            continue
        tot = cnt = 0
        for dirpath, _dirs, files in os.walk(full):
            for fn in files:
                try:
                    tot += os.stat(os.path.join(dirpath, fn)).st_size
                    cnt += 1
                except OSError:
                    pass
        log(f"dyld cache location {p}: {cnt} files, {tot / 2 ** 30:.2f} GiB")


def select_from_source(src_root, keep_dyld=True):
    """Walk the mounted source; return (reg, syms, dirs, total_bytes) of paths to keep.

    keep_dyld=False drops the dyld shared cache (3+ GiB) so the result fits a
    small QEMU RAM-disk root (the ml_io_map ceiling is ~2 GiB). Without dyld,
    launchd cannot start, so this is only for catching kernel-path panics that
    fire before dyld is needed.
    """
    reg, syms, dirs = set(), set(), set()
    total = 0

    def add_ancestors(p):
        while '/' in p:
            p = p.rsplit('/', 1)[0]
            if p:
                dirs.add(p)

    stack = [src_root]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError as e:
            log("scandir skip", cur, e)
            continue
        for e in entries:
            rel = os.path.relpath(e.path, src_root).replace('\\', '/')
            if rel == '.':
                continue
            try:
                if e.is_symlink():
                    # Keep every top-level symlink (etc/tmp/var -> private/... on
                    # iOS) unconditionally: they are boot-critical, carry no data,
                    # and nothing is recorded under their own name, so there is no
                    # real-dir conflict. Nested symlinks keep the HakiOS rules.
                    if rel.count('/') == 0 or any(rel.startswith(p) for p in M.WHOLE) or (keep_dyld and rel.startswith(M.DYLD_PREFIX)):
                        syms.add(rel)
                        add_ancestors(rel)
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                    continue
                # regular file
                try:
                    size = e.stat(follow_symlinks=False).st_size
                except OSError as se:
                    # Non-ASCII / encoding-broken names (e.g. a Thai-localized
                    # .shortcut under WorkflowKit th.lproj) can fail to stat on the
                    # Linux HFS+ mount. Skip the entry instead of aborting the build.
                    log("stat skip", rel, se)
                    continue
                if ((rel.startswith(M.DYLD_PREFIX)
                        or any(rel.startswith(p) for p in DYLD_CACHE_PREFIXES))
                        and keep_dyld) or M.wanted_file(rel, size):
                    reg.add(rel)
                    add_ancestors(rel)
                    total += size
            except OSError as e:
                # is_symlink()/is_dir() can also throw on a broken entry; skip it.
                log("entry skip", rel, getattr(e, 'filename', None), e)
    log(f"keep: {len(reg)} regular files, {len(syms)} symlinks, {len(dirs)} dirs, {total/1073741824:.2f} GiB")
    return reg, syms, dirs, total


def ensure_mount(img, mp, ro):
    os.makedirs(mp, exist_ok=True)
    subprocess.run(['umount', mp], stderr=subprocess.DEVNULL)
    flags = 'loop,ro' if ro else 'loop,rw,force'
    r = subprocess.run(['mount', '-t', 'hfsplus', f'-o{flags}', img, mp])
    if r.returncode != 0:
        raise SystemExit(f"mount failed for {img} at {mp} (rc={r.returncode})")
    return mp


def run(cmd):
    log('+', ' '.join(cmd))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise SystemExit(f"FAILED: {' '.join(cmd)} rc={r.returncode}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src_img')
    ap.add_argument('out_img')
    ap.add_argument('--size-mib', type=int, help='output size in MiB (default: auto from kept bytes)')
    ap.add_argument('--min-mib', type=int, default=4096, help='floor for auto size (default 4096)')
    ap.add_argument('--tweaks', default=os.path.join(HERE, 'sweet-root-tweaks.py'))
    ap.add_argument('--no-tweaks', action='store_true')
    ap.add_argument('--src-mount', default='/mnt/evergo_src')
    ap.add_argument('--tgt-mount', default='/mnt/evergo_tgt')
    ap.add_argument('--no-dyld', action='store_true',
                    help='drop the dyld shared cache (3+ GiB) so the result fits a small QEMU RAM-disk root; launchd will not start')
    a = ap.parse_args()

    if os.geteuid() != 0:
        log("WARNING: not root; loop mount will fail. Run under sudo.")

    src = ensure_mount(a.src_img, a.src_mount, ro=True)
    # total MUST be accumulated while the source is mounted -- once the source
    # is unmounted every stat() fails and the size would silently collapse to 0,
    # pinning the output image at --min-mib.
    #
    # The source must ALSO stay mounted for the copy phase further down: the
    # symlink and regular-file steps read their bytes straight off
    # /mnt/evergo_src. Unmounting it right after selection (as this used to do)
    # made every readlink()/copy2() fail with ENOENT -- CI run 8 died on the very
    # first symlink, /Library/Keychains. It is unmounted only after the copy.
    report_dyld_locations(src)
    if a.no_dyld:
        log("NOTE: --no-dyld set: the dyld shared cache is EXCLUDED; the root will be small enough for the QEMU RAM-disk gate but launchd cannot start")
    reg, syms, dirs, total = select_from_source(src, keep_dyld=not a.no_dyld)

    log(f"kept bytes = {total/1024**3:.2f} GiB")
    if a.size_mib:
        size_mib = a.size_mib
    else:
        # Sizing = content + worst-case 4 KiB block rounding per file + room for
        # the catalog/extents/journal and for sweet-root-tweaks to write plists.
        # The old "+64 MiB" ignored block rounding entirely: with 52791 files that
        # is ~200 MiB of slack, and CI run 9 ran out of space (ENOSPC) with 741
        # files left uncopied. Kept as tight as is safe so the image still fits
        # in guest RAM.
        blocks_mib = len(reg) * 4 // 1024
        size_mib = max(a.min_mib, int(total / (1024 * 1024) * 1.05) + blocks_mib + 256)
    log(f"output image size = {size_mib} MiB")

    # 1) size the output file and make an empty HFS+
    if os.path.exists(a.out_img):
        os.remove(a.out_img)
    run(['dd', 'if=/dev/zero', f'of={a.out_img}', 'bs=1M', f'count={size_mib}', 'status=none'])
    run(['mkfs.hfsplus', '-v', 'evergo-trimmed', a.out_img])

    tgt = ensure_mount(a.out_img, a.tgt_mount, ro=False)
    try:
        # 2) directories first (shallow -> deep)
        for d in sorted(dirs, key=lambda p: p.count('/')):
            if d in syms:
                continue  # a kept symlink owns this path; don't make a real dir
            dp = os.path.join(tgt, d)
            if not os.path.isdir(dp):
                os.makedirs(dp, exist_ok=True)
        # 3) symlinks
        for s in sorted(syms):
            sp = os.path.join(tgt, s)
            os.makedirs(os.path.dirname(sp), exist_ok=True)
            srcp = os.path.join(a.src_mount, s)
            if os.path.lexists(sp):
                continue
            # A real (empty) directory may already exist here if a kept file's
            # ancestor tree created it; drop it so the symlink can take over.
            if os.path.isdir(sp) and not os.path.islink(sp):
                try:
                    os.rmdir(sp)
                except OSError:
                    pass
            # readlink() is a path lookup on the SOURCE mount, so it hits the
            # exact same HFS+ encoding hazard as stat() did during selection:
            # a symlink whose name cannot be looked up (non-ASCII / decomposed)
            # raises FileNotFoundError. Skip it rather than killing the build.
            try:
                link_target = os.readlink(srcp)
            except OSError as e:
                log("readlink skip", s, getattr(e, 'filename', None), e)
                continue
            os.symlink(link_target, sp)
        # 4) regular files (incl. dyld cache under HFS+ Private Data/)
        n = 0
        skipped = 0
        for f in sorted(reg):
            sp = os.path.join(a.src_mount, f)
            dp = os.path.join(tgt, f)
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            # Same class of failure as readlink/stat: a source path that cannot
            # be opened on the HFS+ mount. Log and move on instead of aborting a
            # 14-minute CI job -- the skip count is reported so a large number
            # of skips (i.e. something actually wrong) is visible.
            try:
                shutil.copy2(sp, dp)
            except OSError as e:
                skipped += 1
                if skipped <= 20:
                    log("copy skip", f, getattr(e, 'filename', None), e)
                continue
            n += 1
            if n % 2000 == 0:
                log(f"  copied {n}/{len(reg)}")
        log(f"copied {n} regular files ({skipped} skipped)")
        # Any skipped file means an INCOMPLETE image. CI run 9 hit ENOSPC and
        # quietly dropped 741 of 52791 files yet still reached this point; a
        # partial root looks fine on disk and then fails somewhere unrelated, so
        # fail here instead where the cause is obvious.
        if skipped or (reg and n == 0):
            raise SystemExit(
                f"FATAL: {skipped} of {len(reg)} selected files could not be copied "
                f"(copied {n}); the image would be incomplete")
    finally:
        subprocess.run(['umount', tgt], stderr=subprocess.DEVNULL)

    # Only now is the source expendable -- every step above read from it.
    subprocess.run(['umount', src], stderr=subprocess.DEVNULL)

    # 5) tweaks
    if not a.no_tweaks and os.path.exists(a.tweaks):
        tgt2 = ensure_mount(a.out_img, a.tgt_mount, ro=False)
        try:
            run([sys.executable, a.tweaks, tgt2])
        finally:
            subprocess.run(['umount', tgt2], stderr=subprocess.DEVNULL)

    # 6) fsck
    run(['fsck.hfsplus', '-fy', a.out_img])
    log("DONE ->", a.out_img, f"({os.path.getsize(a.out_img)/1024**2:.0f} MiB)")


if __name__ == '__main__':
    main()
