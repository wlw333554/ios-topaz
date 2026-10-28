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


def log(*a):
    print("[build_trimmed_root]", *a, flush=True)


def select_from_source(src_root):
    """Walk the mounted source; return (reg, syms, dirs, total_bytes) of paths to keep."""
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
                    if rel.count('/') == 0 or any(rel.startswith(p) for p in M.WHOLE) or rel.startswith(M.DYLD_PREFIX):
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
                if rel.startswith(M.DYLD_PREFIX) or M.wanted_file(rel, size):
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
    a = ap.parse_args()

    if os.geteuid() != 0:
        log("WARNING: not root; loop mount will fail. Run under sudo.")

    src = ensure_mount(a.src_img, a.src_mount, ro=True)
    try:
        # total MUST be accumulated while the source is mounted -- once the source
        # is unmounted every stat() fails and the size would silently collapse to 0,
        # pinning the output image at --min-mib.
        reg, syms, dirs, total = select_from_source(src)
    finally:
        subprocess.run(['umount', src], stderr=subprocess.DEVNULL)

    log(f"kept bytes = {total/1024**3:.2f} GiB")
    if a.size_mib:
        size_mib = a.size_mib
    else:
        # minimal slack: a freshly built HFS+ needs ~5% headroom for the
        # catalog/extents/journal + a little room for sweet-root-tweaks to write
        # plists. Kept tight so the image stays RAM-loadable under QEMU.
        size_mib = max(a.min_mib, int(total / (1024 * 1024) * 1.05) + 64)
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
            os.symlink(os.readlink(srcp), sp)
        # 4) regular files (incl. dyld cache under HFS+ Private Data/)
        n = 0
        for f in sorted(reg):
            sp = os.path.join(a.src_mount, f)
            dp = os.path.join(tgt, f)
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            shutil.copy2(sp, dp)
            n += 1
            if n % 2000 == 0:
                log(f"  copied {n}/{len(reg)}")
        log(f"copied {n} regular files")
    finally:
        subprocess.run(['umount', tgt], stderr=subprocess.DEVNULL)

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
