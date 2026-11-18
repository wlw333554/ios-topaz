#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Split a CDX array-stream JSON list into N balanced parts (header kept).
Usage: python split_input.py in.json --parts-dir DIR --n 3
Writes DIR/part_01.json ... part_NN.json
"""
import argparse
import json
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
    # 表头行可能是 ["original",...] 或外层 [ 两种形态：
    # 统一解析成"表头对象"，与记录一起用 json.dump 写成标准 JSON 数组，
    # 保证表头与首条记录之间有逗号，json.load 不会报 Extra data。
    header_obj = None
    if header is not None:
        try:
            h = json.loads(header)
            if isinstance(h, list):
                header_obj = h
        except Exception:
            header_obj = None
    for i in range(n):
        take = q + (1 if i < r else 0)
        chunk = rows[start:start + take]
        start += take
        out = d / ("part_%02d.json" % (i + 1))
        # 每行记录是 JSON 数组文本，逐行 json.loads 还原
        entries = []
        for line in chunk:
            try:
                entries.append(json.loads(line))
            except Exception:
                entries.append(line)
        payload = ([header_obj] if header_obj is not None else []) + entries
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        print("[split] %s : %d entries" % (out.name, len(entries)))


if __name__ == "__main__":
    main()
