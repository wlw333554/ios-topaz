#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段 1：CDX 快照清单收集器
==========================

输入：CDX 概要列表（acfun.cn.json 这种 6 列数组流：original,mimetype,...）
输出：每个 URL 的完整快照清单（真实存在于 Wayback 的所有快照）

  out/cdx_full.jsonl      每行一条：{"url":..., "snapshots":[ts,...], "mimetypes":[...]}
  out/progress.json       断点状态（已处理到第几行 + shard 游标）
  out/stats.json          统计（URL 数、快照总数、失败数）

特性：
  * 复用「档案馆网页爬取列表版.py」的 RealisticFetcher（TLS/HTTP2 指纹、
    浏览器画像、代理探测/故障切换、cookie 会话）
  * CDX API 批量查询：一次请求可查多条 URL（collapse/urlkey 前缀查询），
    但精确到每条 URL 的全量快照仍需逐条查；默认并发 8 路
  * 断点续传：按 URL 记账，重跑自动跳过已完成
  * 自动重试 + 代理切换 + 失败队列（失败的可 --retry-failed 重跑）

用法：
  python collect_cdx.py --json acfun.cn.json --out E:\\acfun_cdx --workers 8
  python collect_cdx.py --resume --out E:\\acfun_cdx
  python collect_cdx.py --limit 100 --out test     # 小规模试跑
"""
import argparse
import importlib.util
import json
import os
import queue
import sys
import threading
import time
import warnings
from pathlib import Path
from urllib.parse import quote as url_quote

import requests

warnings.filterwarnings("ignore")

# ---- 复用桌面爬虫的拟真组件 ----
CRAWLER_SRC = Path(r"C:\Users\Administrator\Desktop\档案馆网页爬取列表版.py")
_spec = importlib.util.spec_from_file_location("wayback_crawler", CRAWLER_SRC)
wb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wb)

RealisticFetcher = wb.RealisticFetcher
log = wb.log

CDX = "https://web.archive.org/cdx/search/cdx"
FL = "original,mimetype,timestamp,endtimestamp,statuscode"
UA_PROFILE = "chrome146"


def load_urls(json_path: Path) -> list:
    """读 CDX 概要列表，返回 [(url, mimetype, ts, endts), ...]

    CDX 是 JSON 数组流：首行表头（尾逗号），每条一行（尾逗号），末行 ']'。
    单行不是合法 JSON，要先剥掉行尾逗号再解析。
    """
    urls = []
    with open(json_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s or s == "]":
                continue
            if s.endswith(","):
                s = s[:-1]
            try:
                row = json.loads(s)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, list) or len(row) < 2:
                continue
            if row[0] == "original":     # 表头
                continue
            urls.append((row[0], row[1], row[2], row[3]))
    return urls


class CdxCollector:
    def __init__(self, out_dir: Path, workers: int = 4, proxy=None,
                 cdx_interval: float = 1.0):
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.workers = workers
        self.cdx_interval = cdx_interval
        self.jsonl = self.out / "cdx_full.jsonl"
        self.progress_f = self.out / "progress.json"
        self.stats_f = self.out / "stats.json"
        self.failed_f = self.out / "failed_urls.txt"

        # 全局限流闸门：任何 worker 收到 429 都会让全体暂停
        self.gate_until = 0.0          # 时间戳：在此之前所有 CDX 请求挂起
        self.gate_lock = threading.Lock()

        # 代理：显式传入优先，否则探测系统代理
        proxies = None
        if proxy:
            proxies = {"http": proxy, "https": proxy}
        else:
            env = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            if env:
                proxies = {"http": env, "https": env}
            else:
                import requests as _r
                px = _r.utils.get_environ_proxies("https://web.archive.org")
                if px:
                    proxies = px
        self.proxies = proxies
        log("[proxy] %s" % (proxies or "direct"))

        # 每个 worker 一个 fetcher（各自 cookie 会话；代理全局共用）
        self.fetchers = [RealisticFetcher(UA_PROFILE, proxies=proxies,
                                          warmup=(i == 0))
                         for i in range(workers)]

        # 断点：以 jsonl 为权威来源重建 done 集合（progress.json 仅作加速）
        self.done = set()
        if self.jsonl.exists():
            with open(self.jsonl, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        self.done.add(json.loads(line)["url"])
                    except Exception:
                        continue
        log("[resume] jsonl 里已有 %d 条" % len(self.done))

        # jsonl 追加锁
        self.file_lock = threading.Lock()
        self.stats = {"urls": 0, "snapshots": 0, "failed": 0,
                      "skipped_by_resume": 0}
        self.stats_lock = threading.Lock()

    def _cdx_query(self, fetcher, url: str, max_rounds: int = 10):
        """查单条 URL 的全量快照。返回 [(ts, mime, code), ...] 或 None。

        直接用 fetcher._raw_get（拟真 TLS 层保留），绕开 fetch() 的重试
        包装——否则 429 会被 fetch 内部消化成 None，全局闸门看不到。
        429 = 全局配额：任一 worker 命中即全体暂停（指数退避上限 120s）。
        """
        q = ("{}/cdx/search/cdx?url={}&fl=mimetype,timestamp,statuscode"
             "&collapse=timestamp:6&limit=-50".format(
                 "https://web.archive.org", url_quote(url, safe="")))
        wait = 30.0
        attempts = 0
        while attempts < max_rounds:
            # 全局限流闸门
            while True:
                with self.gate_lock:
                    until = self.gate_until
                now = time.time()
                if now >= until:
                    break
                time.sleep(min(until - now, 5.0))
            time.sleep(self.cdx_interval)
            attempts += 1

            try:
                r = fetcher._raw_get(q, {})
            except Exception as e:
                log("[NETERR] %s: %s" % (url, str(e)[:90]))
                # 网络抖动：退避后原地重试（不计入 429 闸门）
                time.sleep(min(30.0, 2.0 * attempts))
                continue
            if r.status_code == 429:
                with self.gate_lock:
                    self.gate_until = max(self.gate_until,
                                          time.time() + wait)
                wait = min(wait * 2, 120.0)
                log("[429] 全局暂停 %.0fs（配额限流）" % wait)
                continue
            if r.status_code != 200:
                log("[SKIP] %s -> HTTP %d" % (url, r.status_code))
                return None
            rows = []
            for line in r.text.split("\n"):
                s = line.strip()
                if not s:
                    continue
                parts = s.split(" ")
                if len(parts) >= 3:
                    rows.append((parts[1], parts[0], parts[2]))
            return rows
        return None

    def worker(self, q: "queue.Queue", wid: int):
        fetcher = self.fetchers[wid]
        buf = []
        while True:
            item = q.get()
            if item is None:
                break
            url = item[0]
            rows = self._cdx_query(fetcher, url)
            rec = None
            if rows is None:
                with self.stats_lock:
                    self.stats["failed"] += 1
                # 失败记入失败队列
                with self.file_lock:
                    with open(self.failed_f, "a", encoding="utf-8") as f:
                        f.write(url + "\n")
            else:
                ts_list = [t for (t, _, _) in rows
                           if t and t != "-"]
                rec = {"url": url, "mimetypes": sorted({m for (_, m, _) in rows}),
                       "snapshots": ts_list}
                with self.stats_lock:
                    self.stats["urls"] += 1
                    self.stats["snapshots"] += len(ts_list)
                buf.append(rec)

            if rec is not None:
                with self.file_lock:
                    with open(self.jsonl, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self.done.add(url)
                # 每 500 条写一次 progress
                if len(self.done) % 500 == 0:
                    self._save_progress()
            q.task_done()

    def _save_progress(self):
        tmp = self.progress_f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"done": sorted(self.done)[-200000:],
                                   "count": len(self.done)},
                                  ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.progress_f)

    def run(self, urls, limit=0):
        todo = [u for u in urls if u[0] not in self.done]
        limit_note = ""
        if limit:
            todo = todo[:limit]
            limit_note = " (limit)"
        self.stats["skipped_by_resume"] = len(urls) - len(todo) - (
            0 if not limit_note else 0)
        log("[plan] 总 %d，已完成 %d，本次待查 %d%s"
            % (len(urls), len(self.done), len(todo), limit_note))
        if not todo:
            log("全部完成")
            return

        q = queue.Queue()
        threads = []
        for i in range(self.workers):
            t = threading.Thread(target=self.worker, args=(q, i), daemon=True)
            t.start()
            threads.append(t)
        for item in todo:
            q.put(item)
        for _ in threads:
            q.put(None)

        t0 = time.time()
        n0 = len(self.done)
        while any(t.is_alive() for t in threads):
            time.sleep(10)
            dn = len(self.done) - n0
            rate = dn / max(1e-9, time.time() - t0)
            eta = (len(todo) - dn) / max(rate, 1e-9) / 3600
            log("[progress] %d/%d (%.1f/s, ETA %.1fh) 快照累计 %d 失败 %d"
                % (dn, len(todo), rate, eta,
                   self.stats["snapshots"], self.stats["failed"]))

        self._save_progress()
        self.stats_f.write_text(json.dumps(self.stats, ensure_ascii=False),
                                encoding="utf-8")
        log("[done] %s" % json.dumps(self.stats, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description="CDX 快照清单收集器（阶段 1）")
    ap.add_argument("--json", default=None, help="CDX 概要列表 json 路径")
    ap.add_argument("--url", default=None,
                    help="输入直链（7z/json）——云端模式，先下载再处理")
    ap.add_argument("--workdir", default=None,
                    help="工作根目录（云端 /mnt/acfun/cdx）")
    ap.add_argument("--out", default=r"E:\acfun_cdx", help="输出目录")
    ap.add_argument("--shard-count", type=int, default=0)
    ap.add_argument("--shard-index", type=int, default=0)
    ap.add_argument("--shard-size", type=int, default=100000)
    ap.add_argument("--workers", type=int, default=4,
                    help="并发数（云端 IP 限流更狠，默认 4）")
    ap.add_argument("--limit", type=int, default=0, help="只查前 N 条（测试）")
    ap.add_argument("--interval", type=float, default=1.5,
                    help="每个 CDX 请求的固定间隔秒（默认 1.5；429 多就调大）")
    ap.add_argument("--proxy", default=None,
                    help="代理地址（默认自动探测系统代理；云端直连不用传）")
    ap.add_argument("--retry-failed", action="store_true",
                    help="只重跑上次失败的 URL")
    args = ap.parse_args()

    if args.url:
        # 云端模式：workdir 下 input/ 下载+解压，out=workdir/out
        import requests as _rq
        workdir = Path(args.workdir or "/mnt/acfun/cdx")
        inp = workdir / "input"
        inp.mkdir(parents=True, exist_ok=True)
        url = args.url.strip()
        name = url.split("?")[0].rstrip("/").split("/")[-1] or "input.bin"
        if "fn=" in url:
            name = url.split("fn=")[-1].split("&")[0]
        dest = inp / name
        if not (dest.exists() and dest.stat().st_size > 0):
            headers = {}
            if "github.com" in url or "githubusercontent.com" in url:
                tok = os.environ.get("GH_TOKEN")
                if tok:
                    headers["Authorization"] = "Bearer " + tok
            print("[input] downloading", url)
            with _rq.get(url, stream=True, timeout=(30, 300),
                         headers=headers) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            print("[input] got %.1f MB" % (dest.stat().st_size / 1048576))
        jsons = []
        if dest.suffix.lower() == ".json":
            jsons = [dest]
        else:
            outd = inp / dest.stem
            if not outd.exists():
                outd.mkdir(parents=True)
                subprocess.run(["7z", "x", "-y", str(dest), "-o" + str(outd)],
                               check=True, capture_output=True)
            jsons = sorted(outd.glob("*.json"))

        # 合并所有 json 成一个大列表文件（分片基于总条数）
        merged = workdir / "merged_all_lines.txt"
        if not merged.exists():
            n = 0
            with open(merged, "w", encoding="utf-8") as fo:
                for j in jsons:
                    with open(j, "r", encoding="utf-8",
                              errors="replace") as fi:
                        for line in fi:
                            s = line.strip()
                            if not s or s == "]":
                                continue
                            if s.endswith(","):
                                s = s[:-1]
                            # 表头行以 [["original" 开头（外层 [ + 内层 [）
                            if s.startswith('[["original"') or \
                                    '"original"' in s[:40]:
                                continue
                            fo.write(s + "\n")
                            n += 1
            print("[merge] %d entries -> %s" % (n, merged.name))

        out = workdir / "out"
        col = CdxCollector(out, workers=args.workers,
                           proxy=args.proxy, cdx_interval=args.interval)

        # 分片切片（行号区间）；断点由 out/cdx_full.jsonl 记账
        total = sum(1 for _ in open(merged, encoding="utf-8"))
        start = (args.shard_index - 1) * args.shard_size
        end = min(start + args.shard_size, total)
        print("[plan] total %d, shard %d/%d = lines [%d,%d)"
              % (total, args.shard_index, args.shard_count, start, end))

        urls = []
        with open(merged, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i < start:
                    continue
                if i >= end:
                    break
                s = line.strip()
                if not s:
                    continue
                # merged 行是 ["url","mimetype",...] 数组流的一行，
                # 解出真正的 URL 再查询
                try:
                    row = json.loads(s)
                    urls.append((row[0], "", "", ""))
                except json.JSONDecodeError:
                    urls.append((s, "", "", ""))
        shard_tag = "shard%02dof%02d" % (args.shard_index, args.shard_count)
        (workdir / "shard_tag.txt").write_text(shard_tag, encoding="utf-8")
        col.run(urls, limit=args.limit)
        return

    # ---- 本地模式（原样）----
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    col = CdxCollector(out, workers=args.workers, proxy=args.proxy,
                       cdx_interval=args.interval)

    if args.retry_failed:
        f = out / "failed_urls.txt"
        if not f.exists():
            log("没有失败队列")
            return
        urls = [(l.strip(), "", "", "")
                for l in f.read_text(encoding="utf-8").split("\n")
                if l.strip()]
        f.unlink()
        log("[retry] 待重跑失败 URL: %d" % len(urls))
    else:
        if not args.json:
            ap.error("需要 --json（除非 --retry-failed）")
        urls = load_urls(Path(args.json))
        log("[load] %d 条 URL" % len(urls))

    col.run(urls, limit=args.limit)


if __name__ == "__main__":
    main()
