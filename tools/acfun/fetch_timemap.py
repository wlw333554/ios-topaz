#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下载一批 Wayback timemap/CDX JSON 链接，原样保存为 .json 文件。

输入：timemap 链接（多个，按行 / 空格 / 逗号分隔，或 --urls-file）
命名：从链接的 url= 参数提取域名（url=http%3A%2F%2F127.0.0.1 -> 127.0.0.1.json）
输出：每个链接一个 .json 文件（原样保存响应文本，并校验可解析）
"""
import argparse
import json
import re
import sys
import urllib.parse
from pathlib import Path

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def split_urls(text: str):
    """按行/空白/逗号拆分链接，只保留 http(s) 开头的。"""
    items = re.split(r"[\s,]+", text.strip())
    return [u for u in items if u.lower().startswith(("http://", "https://"))]


def domain_of(link: str) -> str:
    """从 timemap 链接的 url= 参数提取域名；提取不到则用链接自身 host。"""
    target = ""
    try:
        qs = urllib.parse.urlparse(link).query
        params = urllib.parse.parse_qs(qs)
        target = params.get("url", [""])[0]
    except Exception:
        pass
    if target:
        p = urllib.parse.urlparse(target)
        host = p.netloc or p.path
    else:
        host = urllib.parse.urlparse(link).netloc
    # 去掉 userinfo 和端口
    host = host.split("@")[-1].split(":")[0]
    host = re.sub(r"[^A-Za-z0-9._-]+", "_", host).strip("._")
    return host or "timemap"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls", default="", help="链接文本（按行/空格/逗号分隔）")
    ap.add_argument("--urls-file", default=None, help="链接清单文件（每行一个）")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--timeout", type=int, default=300, help="单链接读取超时（秒）")
    args = ap.parse_args()

    text = args.urls
    if args.urls_file:
        text += "\n" + Path(args.urls_file).read_text(encoding="utf-8", errors="replace")
    urls = split_urls(text)
    if not urls:
        sys.exit("[ERROR] 没有提供任何 timemap 链接")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    used = {}
    saved = []
    sess = requests.Session()
    for i, link in enumerate(urls, 1):
        name = domain_of(link)
        if name in used:            # 域名重复时加序号，避免覆盖
            used[name] += 1
            name = "%s_%d" % (name, used[name])
        else:
            used[name] = 1
        dest = out / (name + ".json")
        print("[fetch %d/%d] %s -> %s" % (i, len(urls), link[:140], dest.name), flush=True)
        try:
            r = sess.get(link, headers={"User-Agent": UA}, timeout=(30, args.timeout))
            r.raise_for_status()
        except Exception as e:
            print("  [ERROR] 下载失败: %s" % e, flush=True)
            continue
        raw = r.content
        dest.write_bytes(raw)
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
            rows = len(data) if isinstance(data, list) else 1
            print("  OK: %.1f KB, %s 行" % (len(raw) / 1024, rows), flush=True)
        except Exception as e:
            print("  [WARN] 不是合法 JSON（%s），已原样保存" % e, flush=True)
        saved.append(dest)

    print("[done] 保存 %d 个文件到 %s" % (len(saved), out), flush=True)
    if not saved:
        sys.exit("[ERROR] 没有任何文件保存成功")


if __name__ == "__main__":
    main()
