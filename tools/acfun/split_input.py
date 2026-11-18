#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Split a CDX array-stream JSON list into N balanced parts (header kept).
Usage: python split_input.py in.json --parts-dir DIR --n 3
Writes DIR/part_01.json ... part_NN.json
"""
import argparse
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--parts-dir", required=True)
    ap.add_argument("--n", type=int, default=3)
    args = ap.parse_args()
    d = Path(args.parts_dir)
    d.mkdir(parents=True, exist_ok=True)

    header = None
    rows = []
    with open(args.input, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.rstrip("\n")
            if not s.strip():
                continue
            if header is None:
                header = s                      # 表头行（含外层 '['）
                continue
            if s.strip() == "]":
                continue
            rows.append(s.rstrip(","))

    n = max(1, args.n)
    # 均分：前 r 份多一条
    q, r = divmod(len(rows), n)
    start = 0
    for i in range(n):
        take = q + (1 if i < r else 0)
        chunk = rows[start:start + take]
        start += take
        out = d / ("part_%02d.json" % (i + 1))
        with open(out, "w", encoding="utf-8") as f:
            f.write(header + "\n")
            f.write(",\n".join(chunk))
            f.write("\n]\n")
        print("[split] %s : %d entries" % (out.name, len(chunk)))


if __name__ == "__main__":
    main()
