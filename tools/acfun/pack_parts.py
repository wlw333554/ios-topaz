#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pack crawled web pages into 7z parts.

Each part holds --per-pack files, compressed with:
    -mx=9 -m0=LZMA:d=384m:fb=273 -ms=on   (ultra LZMA, 384M dict, 273 words)
"""
import argparse
import os
import shutil
import subprocess
import time
from pathlib import Path

SEVENZIP = (shutil.which("7z") or shutil.which("7za")
            or shutil.which("7z.exe")
            or r"C:\Program Files\7-Zip\7z.exe")
PACK_ARGS = ["-mx=9", "-m0=LZMA:d=384m:fb=273", "-ms=on"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", required=True)
    ap.add_argument("--parts", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--per-pack", type=int, default=100000)
    args = ap.parse_args()

    pages = Path(args.pages)
    parts = Path(args.parts)
    parts.mkdir(parents=True, exist_ok=True)

    files = []
    for dp, dns, fns in os.walk(pages):
        for fn in fns:
            if fn.startswith("."):
                continue
            files.append(os.path.join(dp, fn))
    files.sort()
    print("[pack] files:", len(files))
    if not files:
        raise SystemExit("no crawled files found")

    idx = 1
    i = 0
    while i < len(files):
        batch = files[i:i + args.per_pack]
        name = "%s_part_%05d.7z" % (args.tag, idx)
        out = parts / name
        lf = parts / (".lf_%05d.txt" % idx)
        lf.write_text("\n".join(batch), encoding="utf-8")
        print("[pack] %s : %d files" % (name, len(batch)))
        t0 = time.time()
        subprocess.run(
            [SEVENZIP, "a", "-t7z", str(out), "-y"] + PACK_ARGS +
            ["-spf2", "@%s" % str(lf)],
            check=True)
        print("[pack] %s done %.1f MB in %.0fs"
              % (name, out.stat().st_size / 1048576, time.time() - t0))
        lf.unlink(missing_ok=True)
        idx += 1
        i += len(batch)
    print("[pack] total parts:", idx - 1)


if __name__ == "__main__":
    main()
