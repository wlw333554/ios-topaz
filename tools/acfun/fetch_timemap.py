#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下载 Wayback timemap/CDX JSON，原样保存为 .json。

使用 AcFun Web Archive Shard (AWAS) 的真人模拟机制：
  - curl_cffi TLS/HTTP2 指纹伪装（ impersonate 浏览器画像）
  - 真实浏览器请求头（build_headers）
  - 预热握手 + 共享 Cookie 会话
  - 遵守 Retry-After + 指数退避重试（对 503 等临时故障反复重试）
命名：从链接 url= 参数提取域名；保存前校验 JSON 可解析。
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests  # 仅作兜底；主路径走仓库的 RealisticFetcher

# 复用 AWAS 模块（与本脚本同目录 tools/acfun/）
sys.path.insert(0, str(Path(__file__).resolve().parent))
import wayback_crawler as awas


def split_urls(text: str):
    items = re.split(r"[\s,]+", (text or "").strip())
    return [u for u in items if u.lower().startswith(("http://", "https://"))]


def domain_of(link: str) -> str:
    target = ""
    try:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        target = params.get("url", [""])[0]
    except Exception:
        pass
    host = (urllib.parse.urlparse(target).netloc or urllib.parse.urlparse(target).path) \
        if target else urllib.parse.urlparse(link).netloc
    host = host.split("@")[-1].split(":")[0]
    host = re.sub(r"[^A-Za-z0-9._-]+", "_", host).strip("._")
    return host or "timemap"


def is_timemap_json(raw: bytes) -> bool:
    try:
        d = json.loads(raw.decode("utf-8", errors="replace"))
        return isinstance(d, list)
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls", default="", help="timemap 链接（按行/空格/逗号分隔）")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--timeout", type=int, default=300, help="单请求超时（秒）")
    ap.add_argument("--retries", type=int, default=12,
                    help="fetch() 内部单轮最大退避重试次数（默认 12）")
    ap.add_argument("--rounds", type=int, default=3,
                    help="整体重试轮数（fetch 仍失败时再来一轮；默认 3）")
    ap.add_argument("--delay", type=float, default=0.4,
                    help="基准请求间隔（秒，默认 0.4）")
    args = ap.parse_args()

    urls = split_urls(args.urls)
    if not urls:
        sys.exit("[ERROR] 没有提供任何 timemap 链接")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # ---- AWAS 真人环境 ----
    limiter = awas.RateLimiter(delay=args.delay, jitter=0.45,
                               human_pause=True, backoff=True)
    fetcher = awas.RealisticFetcher(
        profile_name=awas.DEFAULT_PROFILE,
        proxies=None,
        lang=None,
        cookie_path=out / ".wayback_cookies.txt",
        timeout=args.timeout,
        warmup=True,
        use_impersonate=True,
        http2=True,
    )
    awas.log("[INFO] 抓取后端: %s" % fetcher.backend)
    fetcher.warmup_session()          # 预热：访问归档首页建会话 / 拿 cookie
    limiter.acquire()

    used = {}
    saved = []
    for i, link in enumerate(urls, 1):
        name = domain_of(link)
        if name in used:
            used[name] += 1
            name = "%s_%d" % (name, used[name])
        else:
            used[name] = 1
        dest = out / (name + ".json")
        awas.log("[fetch %d/%d] %s -> %s" % (i, len(urls), link[:130], dest.name))

        # fetch() 内部对 503/429/5xx 按退避策略重试（max_retries 次）；
        # 返回 response 或 None。失败再整体重试，直到拿到合法 timemap JSON。
        ok = False
        for attempt in range(1, max(1, args.rounds) + 1):
            limiter.acquire()
            try:
                resp = fetcher.fetch(
                    link, kind="document", referer=awas.ARCHIVE_HOME,
                    max_retries=args.retries, limiter=limiter,
                    label=dest.name,
                )
            except Exception as e:
                awas.log("[WARN] 第 %d/%d 轮异常: %s" % (attempt, args.rounds, e))
                resp = None
            if resp is not None:
                raw = resp.content
                if is_timemap_json(raw):
                    dest.write_bytes(raw)
                    awas.log("  OK: %.1f KB, %d 行" %
                             (len(raw) / 1024, len(json.loads(raw.decode("utf-8", "replace")))))
                    ok = True
                    break
                else:
                    awas.log("[WARN] 响应不是 timemap JSON（%d 字节），重试" % len(raw))
            else:
                awas.log("[WARN] 第 %d/%d 轮抓取失败（内部已退避重试）" % (attempt, args.rounds))
        if not ok:
            awas.log("[ERROR] %s 多次重试后仍失败，跳过该链接" % dest.name)
            continue
        saved.append(dest)

    fetcher.save_cookies()
    awas.log("[done] 保存 %d 个文件到 %s" % (len(saved), out))
    if not saved:
        sys.exit("[ERROR] 没有任何文件保存成功")


if __name__ == "__main__":
    main()
