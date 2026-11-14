#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prepare the CDX shard list for the crawler.

Downloads the input (7z or json direct link), extracts jsons, concatenates
them into one entry list, slices the requested shard, and writes a crawler-
ready CDX json (header line kept verbatim - it carries the trailing comma -
entries joined with commas, closed with ]).
"""
import argparse
import json
import subprocess
from pathlib import Path

import requests


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--shard-count", type=int, default=93)
    ap.add_argument("--shard-index", type=int, default=1)
    ap.add_argument("--shard-size", type=int, default=50000)
    ap.add_argument("--limit", type=int, default=0,
                    help="test: take first N entries of the whole list")
    args = ap.parse_args()

    base = Path(args.workdir)
    inp = base / "input"
    inp.mkdir(parents=True, exist_ok=True)

    url = args.url.strip()
    name = url.split("?")[0].rstrip("/").split("/")[-1] or "input.bin"
    if "fn=" in url:
        name = url.split("fn=")[-1].split("&")[0]
    dest = inp / name

    if dest.exists() and dest.stat().st_size > 0:
        print("[input] cached:", dest.name)
    else:
        headers = {}
        if "github.com" in url or "githubusercontent.com" in url:
            import os
            tok = os.environ.get("GH_TOKEN")
            if tok:
                headers["Authorization"] = "Bearer " + tok
                headers["Accept"] = "application/octet-stream"
        print("[input] downloading", url)
        with requests.get(url, stream=True, timeout=(30, 300),
                          headers=headers) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        print("[input] got %.1f MB" % (dest.stat().st_size / 1048576))

    if dest.suffix.lower() == ".json":
        jsons = [dest]
    else:
        outd = inp / dest.stem
        if not outd.exists():
            outd.mkdir(parents=True)
            subprocess.run(["7z", "x", "-y", str(dest), "-o" + str(outd)],
                           check=True, capture_output=True)
        jsons = sorted(outd.glob("*.json"))
    assert jsons, "no json found in input"
    print("[input] json files:", [j.name for j in jsons])

    # ---- concatenate + shard ----
    # CDX array-stream format: header line (trailing comma), one entry per
    # line each ending with a comma, final line "]". Keep the header verbatim.
    all_lines = []
    header = None
    for j in jsons:
        with open(j, "r", encoding="utf-8", errors="replace") as f:
            first = True
            for line in f:
                s = line.rstrip("\n")
                if not s.strip():
                    continue
                if first:
                    header = s
                    first = False
                    continue
                if s.strip() == "]":
                    continue
                all_lines.append(s.rstrip(","))
    total = len(all_lines)
    print("[plan] total entries:", total)

    if args.limit:
        sel = all_lines[:args.limit]
        shard_tag = "limit%d" % args.limit
    else:
        start = (args.shard_index - 1) * args.shard_size
        end = min(start + args.shard_size, total)
        assert start < total, "shard_index beyond total"
        sel = all_lines[start:end]
        shard_tag = "shard%02dof%02d" % (args.shard_index, args.shard_count)
    print("[plan] shard %s: %d entries" % (shard_tag, len(sel)))

    listfile = inp / ("cdx_%s.json" % shard_tag)
    with open(listfile, "w", encoding="utf-8") as f:
        f.write(header + "\n")
        f.write(",\n".join(sel))
        f.write("]\n")
    print("[plan] wrote", listfile)

    (base / "shard_tag.txt").write_text(shard_tag, encoding="utf-8")


if __name__ == "__main__":
    main()
