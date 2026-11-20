#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下载 Wayback timemap/CDX JSON，原样保存为 .json。

下载引擎策略复刻自 AB Download Manager (amir1376/ab-download-manager)
的 OkHttpHttpDownloaderClient + HttpDownloaderClient + HttpPartDownloader：
  1) 真实 Chrome UA（ABDM UserAgent.DEFAULT_USER_AGENT）
  2) 强制 accept-encoding: identity（ABDM defaultHeadersInLast，拿原始未压缩字节）
  3) 完整浏览器导航头（Accept / Sec-Fetch-* / Upgrade-Insecure-Requests）
  4) 持久连接 + 自动重定向（httpx.Client，等价 OkHttpClient）
  5) Range 探测 bytes=0-255（ABDM test/head）判断链路是否正常
  6) 对 503/429/5xx 指数退避 + 遵守 Retry-After，多轮重试
  7) 失败轮换 UA 变体（等价 ABDM 的链路重试思路）

命名：从链接 url= 参数提取域名；保存前校验是 timemap JSON 数组。
"""
import argparse
import json
import random
import re
import sys
import time
import urllib.parse
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

# ---- ABDM UserAgent.kt DEFAULT_USER_AGENT ----
ABDM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
UA_VARIANTS = [
    ABDM_UA,
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

RETRY_CODES = {408, 425, 429, 500, 502, 503, 504}


def split_urls(text):
    return [u for u in re.split(r"[\s,]+", (text or "").strip())
            if u.lower().startswith(("http://", "https://"))]


def domain_of(link):
    target = ""
    try:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        target = params.get("url", [""])[0]
    except Exception:
        pass
    host = (urllib.parse.urlparse(target).netloc or urllib.parse.urlparse(target).path) \
        if target else urllib.parse.urlparse(link).netloc
    host = host.split("@")[-1].split(":")[0]
    return re.sub(r"[^A-Za-z0-9._-]+", "_", host).strip("._") or "timemap"


def is_timemap_json(raw):
    try:
        return isinstance(json.loads(raw.decode("utf-8", errors="replace")), list)
    except Exception:
        return False


def nav_headers(ua):
    """ABDM 风格导航头；accept-encoding: identity 放最后强制覆盖。"""
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "application/json;q=0.8,*/*;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
        "Accept-Encoding": "identity",   # ABDM: defaultHeadersInLast 强制覆盖
    }


def retry_after_seconds(value):
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        dt = parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - time.time())
    except Exception:
        return None


def fetch_one(client, link, ua, timeout, label):
    """一次完整 GET，返回 (raw, status)；不做重试。"""
    r = client.get(link, headers=nav_headers(ua), timeout=timeout,
                   follow_redirects=True)
    return r.content, r.status_code, r.headers


def download(link, out, timeout, retries, base_delay, label):
    """ABDM 风格：连接复用 + Range 探测 + 指数退避重试 + UA 轮换。"""
    with httpx.Client(http2=True, follow_redirects=True,
                      limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
                      timeout=httpx.Timeout(timeout)) as client:
        # ABDM test(): Range 探测 bytes=0-255，确认链路可达（失败不致命，继续正常 GET）
        try:
            probe = client.get(link, headers={**nav_headers(UA_VARIANTS[0]),
                                              "Range": "bytes=0-255"},
                               timeout=min(30, timeout), follow_redirects=True)
            print(f"[probe] Range -> HTTP {probe.status_code}, {len(probe.content)} bytes", flush=True)
        except Exception as e:
            print(f"[probe] Range 探测失败（忽略继续）: {type(e).__name__}", flush=True)

        attempt = 0
        while attempt < max(1, retries):
            attempt += 1
            ua = UA_VARIANTS[(attempt - 1) % len(UA_VARIANTS)]
            try:
                raw, code, hdrs = fetch_one(client, link, ua, timeout, label)
            except Exception as e:
                back = min(30.0, base_delay * (2 ** (attempt - 1))) * random.uniform(0.8, 1.3)
                print(f"[RETRY] {label} {type(e).__name__}，退避 {back:.1f}s ({attempt}/{retries})",
                      flush=True)
                time.sleep(back)
                continue

            if code == 200 and is_timemap_json(raw):
                return raw
            if code in RETRY_CODES or not is_timemap_json(raw):
                ra = retry_after_seconds(hdrs.get("Retry-After"))
                if ra is not None:
                    back = ra + random.uniform(0.5, 2.0)
                    src = "Retry-After"
                else:
                    back = min(30.0, base_delay * (2 ** (attempt - 1))) * random.uniform(0.8, 1.4)
                    src = "exponential"
                print(f"[RETRY] {label} HTTP {code}，{src} 退避 {back:.1f}s "
                      f"({attempt}/{retries})", flush=True)
                time.sleep(back)
                continue
            # 其它非 200（如 404）
            print(f"[ERROR] {label} HTTP {code}，不可恢复", flush=True)
            return None
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls", default="", help="timemap 链接（按行/空格/逗号分隔）")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--timeout", type=int, default=300, help="单请求超时（秒）")
    ap.add_argument("--retries", type=int, default=12, help="单链接最大尝试次数")
    ap.add_argument("--delay", type=float, default=1.0, help="退避基准秒（默认 1）")
    args = ap.parse_args()

    urls = split_urls(args.urls)
    if not urls:
        sys.exit("[ERROR] 没有提供任何 timemap 链接")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    used, saved = {}, []
    for i, link in enumerate(urls, 1):
        name = domain_of(link)
        if name in used:
            used[name] += 1
            name = "%s_%d" % (name, used[name])
        else:
            used[name] = 1
        dest = out / (name + ".json")
        print(f"[fetch {i}/{len(urls)}] {link[:130]} -> {dest.name}", flush=True)
        raw = download(link, out, args.timeout, args.retries, args.delay, dest.name)
        if raw is None:
            print(f"[ERROR] {dest.name} 多次重试后仍失败，跳过", flush=True)
            continue
        dest.write_bytes(raw)
        rows = len(json.loads(raw.decode("utf-8", "replace")))
        print(f"  OK: {len(raw)/1024:.1f} KB, {rows} 行", flush=True)
        saved.append(dest)

    print(f"[done] 保存 {len(saved)} 个文件到 {out}", flush=True)
    if not saved:
        sys.exit("[ERROR] 没有任何文件保存成功")


if __name__ == "__main__":
    main()
