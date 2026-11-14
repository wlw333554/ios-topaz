#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wayback Machine 列表下载器（时间回退版 + 真实浏览器环境拟真）
- 默认取每个 URL 的**最早**快照（timestamp 列），可用 --snapshot-field endtimestamp 改为最晚
- 支持 --before 参数：只下载小于等于该时间点的最新快照
- 如果某个URL只有晚于指定时间的快照，则跳过该URL
- 自动从列表中筛选最合适的快照时间点
- 拟真：完整浏览器指纹头、TLS/HTTP2 指纹伪装(curl_cffi 可选)、Cookie 会话、
        Referer 链、预热握手
- 速度：默认**不限速**（无间隔/无抖动/无休息/无退避）；需要拟真节奏时用
        -d/--jitter/--human-pause/--backoff 逐项开启
- 并发：**严格串行**，一次只发一个请求（不做多线程/异步）
- 去重：开抓前先扫本地——文件已存在就跳过（以磁盘文件为准，
  状态记录只作快速通道；记录说下过但文件没了会自动重下）
- 状态：快照 + 增量日志。每完成一条只追加一行，避免整份状态重写（O(n²)）
- 网络：自动探测可用代理（环境变量 / 系统注册表 / 直连），结果带缓存；
        链路故障时自动切换
- 列表格式（6列）：original, mimetype, timestamp, endtimestamp, groupcount, uniqcount
  [0]=URL  [2]=最早快照  [3]=最晚快照
"""

import re
import time
import json
import random
import argparse
import threading
import urllib.parse
import posixpath
import html
import warnings
import sys
import os
import http.cookiejar
from pathlib import Path
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

# curl_cffi 可选：用于 TLS/HTTP2 指纹伪装
try:
    from curl_cffi import CurlHttpVersion
except Exception:  # 未安装时留空，走 requests 回退
    CurlHttpVersion = None

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)


# 带锁输出：保证单条日志不被拆散（也便于将来扩展）
_PRINT_LOCK = threading.Lock()


def log(*args, **kwargs):
    """带锁的 print，保证一条日志完整输出。"""
    kwargs.setdefault("flush", True)
    with _PRINT_LOCK:
        print(*args, **kwargs)

# ============================================================
#  真实浏览器环境拟真配置
# ============================================================

# 编码能力探测：只在确实能解码时才声明 br / zstd，否则会拿到无法解开的响应
def _detect_brotli() -> bool:
    try:
        import brotli  # noqa: F401
        return True
    except Exception:
        pass
    try:
        import brotlicffi  # noqa: F401
        return True
    except Exception:
        return False


def _detect_zstd() -> bool:
    try:
        import zstandard  # noqa: F401
        return True
    except Exception:
        return False


_HAVE_BROTLI = _detect_brotli()
_HAVE_ZSTD = _detect_zstd()

ACCEPT_ENCODING = "gzip, deflate"
if _HAVE_BROTLI:
    ACCEPT_ENCODING += ", br"
if _HAVE_ZSTD:
    ACCEPT_ENCODING += ", zstd"

# 浏览器画像。
#
# 关键设计：装了 curl_cffi 时，**不再手写 UA / sec-ch-ua**。
# 因为 impersonate 模板自带的 UA、sec-ch-ua、Accept-Encoding、头顺序都是
# 该浏览器版本的真实取值；手写反而会与 TLS 指纹矛盾
# （例如 curl_cffi 的 chrome 指纹是 macOS，手写 Windows UA 就是破绽）。
# 我们只覆盖确实需要按目标站点调整的字段：Accept-Language 与 Referer。
#
# requests 回退分支才使用下面的手工头字段。
BROWSER_PROFILES = {
    "chrome146": {
        "impersonate": "chrome146",
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"),
        "sec_ch_ua": '"Google Chrome";v="146", "Chromium";v="146", "Not_A Brand";v="24"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
        "lang": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    },
    "chrome131": {
        "impersonate": "chrome131",
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
        "lang": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    },
    "chrome124": {
        "impersonate": "chrome124",
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
        "lang": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    },
    "edge101": {
        "impersonate": "edge101",
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/101.0.0.0 Safari/537.36 Edg/101.0.0.0"),
        "sec_ch_ua": '"Microsoft Edge";v="101", "Chromium";v="101", "Not-A.Brand";v="99"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
        "lang": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    },
    "firefox147": {
        "impersonate": "firefox147",
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:147.0) "
               "Gecko/20100101 Firefox/147.0"),
        "lang": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
        "extra_nav_headers": {"TE": "trailers"},
    },
    "firefox133": {
        "impersonate": "firefox133",
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) "
               "Gecko/20100101 Firefox/133.0"),
        "lang": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
        "extra_nav_headers": {"TE": "trailers"},
    },
}

DEFAULT_PROFILE = "chrome146"

# 资源类型 -> Accept / Sec-Fetch-* （对齐真实 Chrome / Firefox 的取值）
KIND_ACCEPT = {
    "document": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                 "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"),
    "style": "text/css,*/*;q=0.1",
    "script": "*/*",
    "image": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "font": "*/*",
    "empty": "*/*",
}
KIND_FETCH_MODE = {
    "document": "navigate",
    "style": "no-cors",
    "script": "no-cors",
    "image": "no-cors",
    "font": "cors",
    "empty": "cors",
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".avif", ".bmp", ".ico", ".apng"}
SCRIPT_EXTS = {".js", ".mjs", ".cjs"}
FONT_EXTS = {".woff", ".woff2", ".ttf", ".otf", ".eot"}
DOC_EXTS = {".html", ".htm", ".xhtml", ".shtml", ".php", ".asp", ".aspx", ".jsp", ".do", ".cgi"}


def detect_kind(url: str) -> str:
    """按扩展名判断资源类型，决定请求头怎么写（无扩展名按页面导航处理）"""
    try:
        path = urllib.parse.urlparse(url).path.lower()
    except Exception:
        return "document"
    ext = posixpath.splitext(path)[1]
    if ext in DOC_EXTS or not ext:
        return "document"
    if ext == ".css":
        return "style"
    if ext in SCRIPT_EXTS:
        return "script"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in FONT_EXTS:
        return "font"
    return "empty"


def build_headers(profile: dict, kind: str, referer: str | None, lang: str,
                  native: bool = False) -> dict:
    """
    构造请求头。

    native=True（curl_cffi 路径）：只发送需要覆盖的字段。
        impersonate 模板已经生成了该浏览器版本的真实 UA / sec-ch-ua /
        Accept-Encoding / 头顺序，这里只改语言、来源页和资源类型。
        若重写 UA 会与 TLS 指纹冲突——curl_cffi 的 chrome 指纹是 macOS，
        手写一个 Windows UA 就是破绽。
        资源类型仍必须覆盖：模板默认按文档导航生成 Sec-Fetch-*，
        抓 CSS/图片时要改成对应的 Dest/Mode，否则头与实际资源类型对不上。

    native=False（requests 回退）：完整手工构造，头顺序对齐真实 Chrome。
    """
    h = {}
    is_nav = (kind == "document")
    is_firefox = "firefox" in (profile.get("impersonate") or "").lower()

    if native:
        # UA / sec-ch-ua / Accept-Encoding 交给 impersonate 模板，只覆盖必要项
        h["Accept"] = KIND_ACCEPT[kind]
        h["Accept-Language"] = lang
        h["Sec-Fetch-Dest"] = kind
        h["Sec-Fetch-Mode"] = KIND_FETCH_MODE[kind]
        h["Sec-Fetch-Site"] = "same-origin" if referer else "none"
        if is_nav:
            h["Sec-Fetch-User"] = "?1"
            h["Upgrade-Insecure-Requests"] = "1"
        if referer:
            h["Referer"] = referer
        return h

    if not is_firefox:
        h["sec-ch-ua"] = profile["sec_ch_ua"]
        h["sec-ch-ua-mobile"] = profile["sec_ch_ua_mobile"]
        h["sec-ch-ua-platform"] = profile["sec_ch_ua_platform"]
        if is_nav:
            h["Upgrade-Insecure-Requests"] = "1"
        h["User-Agent"] = profile["ua"]
        h["Accept"] = KIND_ACCEPT[kind]
        h["Sec-Fetch-Site"] = "none" if not referer else "same-origin"
        h["Sec-Fetch-Mode"] = KIND_FETCH_MODE[kind]
        if is_nav:
            h["Sec-Fetch-User"] = "?1"
        h["Sec-Fetch-Dest"] = kind
        h["Accept-Encoding"] = ACCEPT_ENCODING
        h["Accept-Language"] = lang
    else:  # firefox 回退路径
        h["User-Agent"] = profile["ua"]
        h["Accept"] = KIND_ACCEPT[kind]
        h["Accept-Language"] = lang
        h["Accept-Encoding"] = ACCEPT_ENCODING
        if is_nav:
            h["Upgrade-Insecure-Requests"] = "1"
        h["Sec-Fetch-Dest"] = kind
        h["Sec-Fetch-Mode"] = KIND_FETCH_MODE[kind]
        h["Sec-Fetch-Site"] = "none" if not referer else "same-origin"
        if is_nav:
            h["Sec-Fetch-User"] = "?1"
            if profile.get("extra_nav_headers"):
                h.update(profile["extra_nav_headers"])

    if referer:
        h["Referer"] = referer
    return h


CDX_API = "https://web.archive.org/cdx/search/cdx"
WAYBACK_BASE = "https://web.archive.org"
ARCHIVE_HOME = "https://web.archive.org/"

# 不保存的HTTP状态码
SKIP_HTTP_CODES = {400, 401, 402, 403, 404, 405, 406, 407, 408, 409, 410, 411, 412, 413, 414, 415, 416, 417, 418, 421, 422, 423, 424, 425, 426, 428, 429, 431, 451, 500, 501, 502, 503, 504, 505, 506, 507, 508, 510, 511}

# 值得重试的状态码（限流 / 服务端临时故障）
RETRYABLE_CODES = {408, 425, 429, 500, 502, 503, 504}
# 明确的终态：重试没有意义，立刻放弃
TERMINAL_CODES = {404, 410, 401, 402, 405, 406, 407, 451}
# 反爬/封禁类：值得退避后再试
GUARD_CODES = {403}

# 错误页面重试次数
ERROR_RETRY_COUNT = 5

# 状态保存间隔
# 状态快照间隔（秒）；平时只追加增量日志，不再每条重写整份状态
STATUS_SAVE_INTERVAL = 30

# 广告脚本模式
AD_SCRIPT_PATTERNS = [
    'asumi.shinobi.jp/encount',
    'asumi.shinobi.jp/fire',
    '__sys__inactive_message.js',
    'googletagmanager.com',
    'google-analytics.com',
    'doubleclick.net',
    'facebook.com/tr',
    'adsystem',
    'adserver',
    'adservice',
]

HTML_LINK_ATTRS = {
    "a": ["href"],
    "img": ["src", "data-src", "data-original", "data-lazy-src", "data-srcset"],
    "script": ["src", "data-src"],
    "link": ["href"],
    "iframe": ["src", "data-src"],
    "video": ["src", "poster", "data-src"],
    "audio": ["src", "data-src"],
    "source": ["src", "srcset", "data-src", "data-srcset"],
    "embed": ["src"],
    "object": ["data"],
    "form": ["action"],
    "body": ["background"],
    "table": ["background"],
    "td": ["background"],
    "th": ["background"],
}

CUSTOM_DATA_ATTRS = [
    "data-background", "data-image", "data-url", "data-src", "data-srcset"
]

RESOURCE_EXTS = {'.html', '.htm', '.css', '.js', '.json', '.jpg', '.jpeg', '.png',
                 '.gif', '.svg', '.webp', '.ico', '.bmp', '.tiff', '.woff2', '.woff',
                 '.ttf', '.otf', '.eot', '.mid', '.mp3', '.wav', '.ogg', '.aac',
                 '.flac', '.mp4', '.webm', '.ogv', '.avi', '.flv', '.pdf', '.zip',
                 '.gz', '.rar', '.7z', '.bz2', '.tar'}

# MIME类型到扩展名的映射
MIME_TO_EXT = {
    "text/css": ".css",
    "text/javascript": ".js",
    "application/javascript": ".js",
    "application/x-javascript": ".js",
    "application/json": ".json",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "font/woff2": ".woff2",
    "font/woff": ".woff",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
    "font/eot": ".eot",
    "audio/midi": ".mid",
    "audio/x-midi": ".mid",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/ogg": ".ogv",
    "video/x-msvideo": ".avi",
    "video/x-flv": ".flv",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/gzip": ".gz",
    "application/x-rar-compressed": ".rar",
    "application/x-7z-compressed": ".7z",
    "application/x-bzip2": ".bz2",
    "application/x-tar": ".tar",
}

_WIN_RESERVED = {
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
}


class RateLimiter:
    """
    节流器。默认**不做任何等待**（delay=0、无抖动、无驻留），全速抓取。

    如需拟真节奏可自行开启：
      --delay 0.5       基准间隔
      --jitter 0.45     间隔抖动
      --human-pause     随机驻留与周期休息（模拟真人看页面）

    所有停顿都由 MAX_PAUSE 上限约束，避免出现超长等待。
    """

    # 单次停顿的硬上限（秒）
    MAX_PAUSE = 10.0

    def __init__(self, delay: float = 0.0, jitter: float = 0.0,
                 human_pause: bool = False, rest_every: int = 28,
                 backoff: bool = False):
        self.base_delay = max(0.0, delay)
        self.jitter = max(0.0, min(1.0, jitter))
        self.human_pause = human_pause
        self.rest_every = max(0, rest_every)
        self.backoff_enabled = backoff
        self.last_request = 0.0
        self.lock = threading.Lock()
        self.counter = 0
        # 零延时快速路径：完全没有配置停顿时不进锁、不算时间
        self.enabled = (self.base_delay > 0 or self.human_pause)

    @classmethod
    def _cap(cls, seconds: float) -> float:
        return max(0.0, min(seconds, cls.MAX_PAUSE))

    def _next_gap(self) -> float:
        # 主体：围绕基准值的长尾抖动
        gap = self.base_delay * random.uniform(1.0 - self.jitter, 1.0 + self.jitter * 1.6)
        if self.human_pause:
            # 约 6% 概率出现一次明显的驻留（页面渲染 / 换标签）
            if random.random() < 0.06:
                gap += random.uniform(0.8, 3.0)
            # 约 1.5% 概率真的走神了一会儿（同样压在上限内）
            if random.random() < 0.015:
                gap += random.uniform(2.0, self.MAX_PAUSE)
        return self._cap(max(0.0, gap))

    def acquire(self, extra: float = 0.0):
        if not self.enabled:
            return                     # 零延时：直接放行
        with self.lock:
            now = time.time()
            gap = self._next_gap() + (self._cap(extra) if self.human_pause else 0.0)
            wait = self.last_request + gap - now
            self.counter += 1
            do_rest = (self.rest_every and self.counter % self.rest_every == 0 and self.human_pause)
            rest = random.uniform(3.0, self.MAX_PAUSE) if do_rest else 0.0
            self.last_request = now + gap + rest
        if wait > 0:
            time.sleep(wait)
        if rest:
            log(f"[PACE] 模拟浏览休息 {rest:.1f}s")
            time.sleep(rest)

    def penalize(self, seconds: float):
        """遇到限流/封禁时额外加长下一次等待（仍受上限约束）"""
        if not self.enabled:
            return
        with self.lock:
            self.last_request += self._cap(seconds)


class StateManager:
    """
    断点续传状态。

    设计：快照 + 增量日志
      - .wayback_state.json    完整快照（偶尔写一次）
      - .wayback_journal.jsonl 每完成一条追加一行（几乎零成本）

    为什么要这样：原来每 30 条就把全部已完成 URL 重写一遍，是 O(n²)。
    实测 11 万条时单次写 25MB / 932ms，全程累计约 8.5GB 写入、
    光磁盘就要多花近半小时。改成追加日志后，每条只有几十字节。
    """

    FLUSH_EVERY = 25          # 每缓冲多少行刷一次盘

    def __init__(self, state_file: Path):
        self.file = state_file
        self.journal = state_file.with_name(".wayback_journal.jsonl")
        self.lock = threading.Lock()
        self._fh = None
        self._pending = 0

    # ---------- 读 ----------
    def load(self):
        downloaded, url_to_local, url_types = set(), {}, {}

        if self.file.exists():
            try:
                data = json.loads(self.file.read_text(encoding="utf-8"))
                downloaded = set(data.get("downloaded", []))
                url_to_local = {k: Path(v) for k, v in data.get("url_to_local", {}).items()}
                url_types = data.get("url_types", {})
            except Exception as e:
                log(f"[WARN] 状态快照恢复失败: {e}")

        # 重放增量日志，覆盖/补上快照之后完成的部分
        replayed = 0
        if self.journal.exists():
            try:
                with open(self.journal, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            # 崩溃时最后一行可能是半截，跳过即可
                            continue
                        u = rec.get("u")
                        if not u:
                            continue
                        downloaded.add(u)
                        if rec.get("p"):
                            url_to_local[u] = Path(rec["p"])
                        if rec.get("t"):
                            url_types[u] = rec["t"]
                        replayed += 1
            except Exception as e:
                log(f"[WARN] 增量日志重放失败: {e}")
        if replayed:
            log(f"[INFO] 从增量日志补回 {replayed} 条记录")
        return downloaded, url_to_local, url_types

    # ---------- 写 ----------
    def record(self, url: str, local_path=None, ftype: str | None = None):
        """追加一条完成记录（几十字节的写入，取代整份重写）"""
        rec = {"u": url}
        if local_path is not None:
            rec["p"] = str(local_path)
        if ftype:
            rec["t"] = ftype
        with self.lock:
            try:
                if self._fh is None:
                    self._fh = open(self.journal, "a", encoding="utf-8", buffering=8192)
                self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self._pending += 1
            except Exception as e:
                log(f"[WARN] 增量日志写入失败: {e}")

    def flush_journal(self, force: bool = False):
        with self.lock:
            if self._fh is None:
                return
            if not force and self._pending < self.FLUSH_EVERY:
                return
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._pending = 0
            except Exception:
                pass

    def close_journal(self):
        with self.lock:
            if self._fh is None:
                return
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._fh.close()
            except Exception:
                pass
            self._fh = None
            self._pending = 0

    def save(self, downloaded, url_to_local, url_types):
        """写完整快照。只在时间间隔到点或结束时调用。"""
        with self.lock:
            try:
                state = {
                    "downloaded": list(downloaded),
                    "url_to_local": {k: str(v) for k, v in url_to_local.items()},
                    "url_types": url_types,
                }
                self.file.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
            except Exception as e:
                log(f"[WARN] 状态快照保存失败: {e}")


def _parse_proxy_server(proxy_server: str) -> dict:
    """解析 Windows 注册表的 ProxyServer 值，支持 http=...;https=... 与单一地址两种写法"""
    proxies = {}
    if "=" in proxy_server:
        for part in proxy_server.split(";"):
            if "=" in part:
                proto, addr = part.split("=", 1)
                proto, addr = proto.strip().lower(), addr.strip()
                if proto in ("http", "https", "socks", "socks5", "socks4"):
                    if not addr.startswith(("http://", "https://", "socks4://", "socks5://")):
                        addr = f"http://{addr}"
                    proxies[proto] = addr
    else:
        addr = proxy_server.strip()
        if addr:
            if not addr.startswith(("http://", "https://", "socks4://", "socks5://")):
                addr = f"http://{addr}"
            proxies["http"] = addr
            proxies["https"] = addr
    return proxies


def _env_proxy_candidates():
    """环境变量里的代理（可能有多组，都要试）"""
    out = []
    http_p = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    https_p = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    all_p = os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")

    if https_p or http_p:
        px = {}
        if https_p:
            px["https"] = https_p
        if http_p:
            px["http"] = http_p
        # 单边配置时补全另一边，否则 http 资源会走直连
        if https_p and not http_p:
            px["http"] = https_p
        if http_p and not https_p:
            px["https"] = http_p
        out.append(("环境变量", px))
    if all_p:
        out.append(("环境变量 ALL_PROXY", {"http": all_p, "https": all_p}))
    return out


def _registry_proxy_candidates():
    """Windows 系统代理（可能与环境变量不同，两套都要试）"""
    if sys.platform != "win32":
        return []
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            proxy_enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not proxy_enable:
                return []
            proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
        finally:
            winreg.CloseKey(key)
        px = _parse_proxy_server(proxy_server)
        return [("系统代理(注册表)", px)] if px else []
    except Exception:
        return []


def collect_proxy_candidates(explicit=None):
    """
    收集所有候选代理，按优先级排序，去重。
    返回 [(标签, proxies_dict 或 None)]，None 表示直连。

    顺序：命令行显式指定 > 环境变量 > 系统注册表 > 直连
    显式传空字符串表示强制直连。
    """
    if explicit is not None:
        if not explicit.strip():
            return [("强制直连(--proxy 为空)", None)]
        px = _parse_proxy_server(explicit) or {"http": explicit, "https": explicit}
        return [(f"命令行指定({explicit})", px)]

    out = []
    for label, px in _env_proxy_candidates():
        out.append((label, px))
    for label, px in _registry_proxy_candidates():
        out.append((label, px))
    out.append(("直连", None))

    # 按协议+地址去重，保留先出现的
    seen, uniq = set(), []
    for label, px in out:
        k = tuple(sorted((px or {}).items()))
        if k in seen:
            continue
        seen.add(k)
        uniq.append((label, px))
    return uniq


def _is_proxy_error(exc: Exception) -> bool:
    """判断是否为代理链路故障（而非目标站点故障），这类错误值得换代理重试"""
    msg = str(exc).lower()
    markers = ("connect tunnel failed", "proxy", "502", "tunnel connection",
               "proxyerror", "cannot connect to proxy")
    return any(m in msg for m in markers)


def _is_ssl_connect_error(exc: Exception) -> bool:
    """
    判断是否为 TLS 握手被中断。

    实测（曾用多线程时）：并发越高越容易出现
    "BoringSSL SSL_connect: Connection closed abruptly (SSL_ERROR_SYSCALL)"。
    这是链路压力导致连接被对端/代理丢弃，属于传输问题而非代理配置问题，
    所以不该触发「换代理」——否则会无效地切到直连再失败一遍。
    """
    msg = str(exc).lower()
    if "ssl" not in msg and "tls" not in msg:
        return False
    return any(m in msg for m in ("ssl_connect", "ssl_error_syscall", "ssl handshake",
                                  "connection closed abruptly", "_ssl.c"))


class ProxyResolver:
    """
    代理自动选择与故障切换。

    本机实测情况（2026-10）：
      - 环境变量代理 127.0.0.1:7383  恒返回 502 CONNECT tunnel failed（已死）
      - 系统代理     127.0.0.1:6542  可用，但**慢且不稳**：平均 16s，约 1/4 超时
    原实现优先读环境变量，正好选中死掉的那个，于是全程 502。

    因此这里做了两件事：
      1. 把候选全部实测一遍，选真正能通的（单次超时不算死，要连续失败才淘汰）
      2. 运行中若链路连续失败，自动切到下一个候选
    """

    # 探测点：归档站首页。
    # 曾经用 CDX 接口做探测，但实测 CDX 服务端处理很慢（199 字节的响应要 7~9 秒），
    # 每次探测白等好几秒；首页 ~350ms 就能验证"代理能到归档站"这件事。
    PROBE_URL = "https://web.archive.org/"

    def __init__(self, explicit=None, timeout: float = 20.0, attempts: int = 2,
                 enable_probe: bool = True, verbose: bool = True,
                 cache_file=None, use_cache: bool = True, cache_ttl: float = 1800.0):
        self.candidates = collect_proxy_candidates(explicit)
        self.explicit = explicit
        self.timeout = timeout
        self.attempts = max(1, attempts)
        self.enable_probe = enable_probe
        self.verbose = verbose
        self.index = -1
        self.label = None
        self.proxies = None
        self.dead = set()          # 已确认不可用的候选下标
        self._lock = threading.Lock()
        # 探测结果缓存：避免每次启动都重探（坏代理要 10s 才吐 502）
        self.use_cache = use_cache
        self.cache_ttl = max(0.0, cache_ttl)
        self.cache_file = Path(cache_file) if cache_file else (
            Path.home() / ".wayback_proxy_cache.json" if use_cache else None)

    # ---------- 探测 ----------
    def _make_session(self, proxies):
        try:
            from curl_cffi import requests as _cr
            sess = _cr.Session(impersonate="chrome146")
        except Exception:
            sess = requests.Session()
        sess.trust_env = False        # 只用我们显式指定的代理，避免 env 再次泄漏
        sess.proxies = dict(proxies) if proxies else {}
        return sess

    def _probe(self, proxies) -> tuple:
        """
        探测一条链路。返回 (是否可用, 说明)。

        区分两类失败（这是启动速度的关键）：
          - **明确拒绝**（502 / CONNECT tunnel failed）：代理已经给出确定答复，
            立刻判死，不重试。实测本机那个坏代理要卡 10.2s 才吐 502，
            重试 2 次就是白等 20 秒——这正是之前启动要 24s 的原因。
          - **超时 / 传输异常**：结果不确定（慢代理偶发超时是常态），
            才重试 attempts 次，避免把可用代理误判为死掉。
        """
        last_err = ""
        for i in range(self.attempts):
            sess = None
            try:
                sess = self._make_session(proxies)
                r = sess.get(self.PROBE_URL, timeout=self.timeout)
                if r.status_code < 500:
                    return True, f"HTTP {r.status_code}"
                if r.status_code == 502:
                    return False, "代理拒绝连接(HTTP 502)"
                last_err = f"HTTP {r.status_code}"
            except Exception as e:
                msg = str(e)
                if "502" in msg or "connect tunnel" in msg.lower():
                    return False, "代理拒绝连接(502)"      # 确定性失败，不重试
                last_err = "超时" if "(28)" in msg else type(e).__name__
            finally:
                if sess is not None:
                    try:
                        sess.close()
                    except Exception:
                        pass
            if i + 1 < self.attempts and self.verbose:
                log(f"重试({i+1}/{self.attempts})...", end="", flush=True)
        return False, last_err

    # ---------- 结果缓存：避免每次启动都重新探测 ----------
    def _load_cache(self) -> bool:
        if not self.use_cache or self.explicit is not None or not self.cache_file:
            return False
        try:
            if not self.cache_file.exists():
                return False
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if time.time() - float(data.get("ts", 0)) > self.cache_ttl:
                return False
            label, px = data.get("label"), data.get("proxies")
            if label is None:
                return False
            idx = next((i for i, (l, p) in enumerate(self.candidates)
                        if l == label and (p or {}) == (px or {})), None)
            if idx is None:
                return False
            self.index, self.label, self.proxies = idx, label, px
            return True
        except Exception:
            return False

    def _save_cache(self):
        if not self.use_cache or self.cache_file is None:
            return
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps({
                "label": self.label, "proxies": self.proxies,
                "ts": time.time(), "url": self.PROBE_URL,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def invalidate_cache(self):
        """请求真的失败时调用：丢弃缓存，下次启动重新探测"""
        if not self.use_cache or self.cache_file is None:
            return
        try:
            self.cache_file.unlink(missing_ok=True)
        except Exception:
            pass

    def resolve(self):
        """优先用缓存；否则依次探测候选，选中第一个可用的"""
        if self._load_cache():
            if self.verbose:
                age = int(time.time() - self.cache_file.stat().st_mtime)
                log(f"[PROXY] 使用缓存结果: {self.describe()}（{age}s 前验证过）")
            return self.label, self.proxies

        if not self.enable_probe:
            self.index = 0
            self.label, self.proxies = self.candidates[0]
            return self.label, self.proxies

        n = len(self.candidates)
        for i, (label, px) in enumerate(self.candidates):
            if self.verbose:
                tag = (px.get("https") or px.get("http")) if px else "直连"
                log(f"[PROXY] 探测 {i+1}/{n} {label} -> {tag} ...", end="", flush=True)
            ok, detail = self._probe(px)
            if self.verbose:
                log(f" {'可用 ' + detail if ok else '不可用 (' + detail + ')'}")
            if ok:
                self.index = i
                self.label, self.proxies = label, px
                self._save_cache()
                return self.label, self.proxies
            self.dead.add(i)

        if self.verbose:
            log("[PROXY] 所有候选均不可用，将直连尝试")
        self.index = n
        self.label, self.proxies = "直连(兜底)", None
        return self.label, self.proxies

    def failover(self):
        """当前链路失效，切到下一个尚未确认死亡的候选"""
        with self._lock:
            for i in range(self.index + 1, len(self.candidates)):
                if i in self.dead:
                    continue
                label, px = self.candidates[i]
                if self.verbose:
                    log(f"[PROXY] 链路故障，尝试切换 -> {label}")
                if not self.enable_probe or self._probe(px)[0]:
                    self.index = i
                    self.label, self.proxies = label, px
                    return self.label, self.proxies
                self.dead.add(i)
                if self.verbose:
                    log(f"[PROXY] {label} 也不可用，继续找")
            return None, None

    def describe(self):
        if not self.proxies:
            return "直连"
        return f"{self.label} ({self.proxies.get('https') or self.proxies.get('http')})"


def get_system_proxy():
    """兼容旧调用：返回首个候选代理（优先环境变量）"""
    cands = collect_proxy_candidates()
    for label, px in cands:
        if px:
            return px
    return None


class RealisticFetcher:
    """
    带真实浏览器环境的抓取器。

    真实性要点：
      1. TLS 握手指纹（JA3/JA4）、扩展顺序、ALPN、密码套件顺序
         —— curl_cffi 的 impersonate 按浏览器版本精确复现，这是最强的一层
      2. HTTP/2 帧指纹：SETTINGS 参数、窗口大小、伪头顺序、优先级
         —— curl_cffi 内建支持，默认协商 h2
      3. 请求头：交给 impersonate 模板生成该版本的真实取值与顺序，
         只覆盖语言 / 来源页 / 资源类型
      4. 持久 Cookie 会话 + 连接复用（真浏览器不会每个资源重新握手）
      5. Referer 链：资源请求带上发起页面
      6. 预热握手：先访问归档首页拿 cookie、建连接，再开始抓
      7. 遵守 Retry-After；退避可用 --backoff 开启
    """

    def __init__(self, profile_name: str, proxies=None, lang: str | None = None,
                 cookie_path: Path | None = None, timeout: int = 30,
                 warmup: bool = True, use_impersonate: bool = True,
                 http2: bool = True):
        self.profile = BROWSER_PROFILES.get(profile_name) or BROWSER_PROFILES[DEFAULT_PROFILE]
        self.profile_name = profile_name
        self.lang = lang or self.profile.get("lang", "ja-JP,ja;q=0.9,en;q=0.8")
        self.proxies = proxies
        self.timeout = timeout
        self.warmup = warmup
        self.use_impersonate = use_impersonate
        self.http2 = http2
        self._local = threading.local()
        self._sessions = []
        self._sessions_lock = threading.Lock()
        self._gen_lock = threading.Lock()        # 保护 session 代号与失败计数
        self._failover_lock = threading.Lock()   # 保证切换过程不被重入
        self._session_gen = 0                    # 换代理时递增，驱动各线程重建 Session
        self._ssl_error_count = 0                # TLS 握手中断次数（仅用于提示）
        self._ssl_hint_shown = False
        # 由 downloader 注入：代理链路故障时用它切到下一个候选
        self.proxy_resolver = None
        self._net_fail_streak = 0

        # 共享 cookie jar，模拟同一浏览器连续访问
        self._cookie_jar = None
        if cookie_path:
            try:
                jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
                if cookie_path.exists():
                    jar.load(ignore_discard=True, ignore_expires=True)
                self._cookie_jar = jar
            except Exception as e:
                log(f"[WARN] Cookie 载入失败，将使用内存会话: {e}")
                self._cookie_jar = None

        self.backend = "requests"
        self.native_headers = False
        self._curl_cffi = None
        if use_impersonate:
            try:
                from curl_cffi import requests as _cffi_requests
                self._curl_cffi = _cffi_requests
                self.native_headers = True
                self.backend = f"curl_cffi {getattr(_cffi_requests, '__version__', '?')}"
            except Exception:
                self._curl_cffi = None

    # ---------- 会话管理 ----------
    def _new_session(self):
        if self._curl_cffi is not None:
            target = self.profile.get("impersonate")
            try:
                sess = self._curl_cffi.Session(impersonate=target)
                # 真实 Chrome 用 ALPN 协商 h2，失败时回落 h1，所以用 V2TLS 而非强制 V2_0
                if self.http2 and CurlHttpVersion is not None:
                    try:
                        sess.http_version = CurlHttpVersion.V2TLS
                    except Exception as e:
                        log(f"[WARN] HTTP/2 设置失败，保持默认协商: {e}")
                if self.proxies:
                    try:
                        sess.proxies = dict(self.proxies)
                    except Exception:
                        pass
                # 桥接共享 cookie jar
                if self._cookie_jar is not None:
                    try:
                        sess.cookies.jar = self._cookie_jar
                    except Exception as e:
                        log(f"[WARN] curl_cffi cookie 桥接失败: {e}")
                return sess
            except Exception as e:
                log(f"[WARN] curl_cffi 会话创建失败（{e}），回退 requests")
                self._curl_cffi = None
                self.native_headers = False
                self.backend = "requests"

        sess = requests.Session()
        # 连接复用：真浏览器不会每个资源都重新握手
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=0)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        if self.proxies:
            sess.proxies = dict(self.proxies)
        if self._cookie_jar is not None:
            sess.cookies = self._cookie_jar
        return sess

    def session(self):
        """
        取当前 Session；用「代」号判断是否需要在换代理后重建。
        Session 会被长期复用，以保证连接复用（keep-alive）。
        """
        entry = getattr(self._local, "session", None)
        with self._gen_lock:
            gen = self._session_gen
        if entry is not None and entry[0] == gen:
            return entry[1]
        if entry is not None:
            self._retire(entry[1])       # 旧代的 Session 延后回收
        sess = self._new_session()
        self._local.session = (gen, sess)
        with self._sessions_lock:
            self._sessions.append(sess)
        return sess

    def _retire(self, sess):
        """回收过期 Session；关闭失败也不影响主流程"""
        try:
            sess.close()
        except Exception:
            pass
        with self._sessions_lock:
            try:
                self._sessions.remove(sess)
            except ValueError:
                pass

    def set_proxy(self, proxies):
        """
        运行时切换代理。
        只递增「代」号，下次取 session 时自行重建，
        避免直接关闭正在使用中的连接。
        """
        with self._gen_lock:
            self.proxies = proxies or None
            self._session_gen += 1
            self._net_fail_streak = 0
        log(f"[PROXY] 已切换代理: {proxies or '直连'}（将重建连接）")

    def _note_ssl_error(self):
        """
        TLS 握手被中断的计数与提示。
        这类中断会增加重试次数、拖慢整体速度；只提示一次，避免刷屏。
        """
        with self._gen_lock:
            self._ssl_error_count += 1
            should_hint = (not self._ssl_hint_shown and self._ssl_error_count >= 3)
            if should_hint:
                self._ssl_hint_shown = True
            count = self._ssl_error_count
        if should_hint:
            log(f"[WARN] 已出现 {count} 次 TLS 握手中断（SSL_connect 被对方丢弃）。"
                  f"\n[WARN] 这通常是链路拥塞所致，重试一般能成功；"
                  f"若持续出现可加大 --timeout 或稍后重试")

    def note_network_result(self, ok: bool):
        """
        记录一次网络结果。
        当前代理慢且不稳，单次超时是常态，所以用连续失败计数触发切换，
        比按错误文本匹配（超时消息里并没有 proxy 字样）可靠。
        """
        with self._gen_lock:
            if ok:
                self._net_fail_streak = 0
            else:
                self._net_fail_streak += 1

    def should_failover(self, threshold: int = 3) -> bool:
        with self._gen_lock:
            return (self.proxy_resolver is not None
                    and self._net_fail_streak >= threshold)

    def try_failover(self, urgent: bool = False):
        """
        尝试切换代理。加锁保证切换过程不被重入，
        避免并发调用时把候选列表一次跳好几级。
        返回 (label, proxies)；无需切换或已无候选时返回 (None, None)。
        """
        with self._failover_lock:
            if self.proxy_resolver is None:
                return None, None
            with self._gen_lock:
                need = urgent or self._net_fail_streak >= 3
                if not need:
                    return None, None
                self._net_fail_streak = 0
            label, px = self.proxy_resolver.failover()
            if label is None:
                return None, None
            return label, px

    def save_cookies(self):
        if self._cookie_jar is None:
            return
        try:
            self._cookie_jar.save(ignore_discard=True, ignore_expires=True)
        except Exception as e:
            log(f"[WARN] Cookie 保存失败: {e}")

    # ---------- 预热 ----------
    def warmup_session(self, entry_url: str | None = None):
        """
        预热：只访问**归档站首页**建立会话、拿 cookie、把 TLS 连接立起来。

        曾经还会去访问目标站（原站）的首页，但那纯属浪费：
        我们所有请求都发给 web.archive.org，原站的 cookie 一次也用不上，
        而且原站若不可达还要白等一个超时。已移除。
        """
        if not self.warmup:
            return
        headers = build_headers(self.profile, detect_kind(ARCHIVE_HOME), None,
                                self.lang, native=self.native_headers)
        try:
            # 预热用更短的超时：握手不通就快速放弃，不拖慢整体启动
            self.session().get(ARCHIVE_HOME, headers=headers,
                               timeout=min(self.timeout, 12),
                               allow_redirects=True)
            log(f"[WARMUP] 已建立会话: {ARCHIVE_HOME}")
        except Exception as e:
            log(f"[WARN] 预热失败 {ARCHIVE_HOME}: {type(e).__name__}（继续抓取）")

    # ---------- 单次请求 ----------
    def _raw_get(self, url: str, headers: dict, allow_redirects=True, timeout=None,
                 direct: bool = False):
        """
        direct=True 时忽略代理重试，但**保持同一浏览器指纹**
        （不用 requests 直连，否则 TLS 指纹会突变，等于换了个客户端）。
        """
        sess = self.session()
        kwargs = dict(headers=headers, allow_redirects=allow_redirects,
                      timeout=timeout or self.timeout)
        if self.proxies and not direct and self._curl_cffi is not None:
            kwargs["proxies"] = dict(self.proxies)
        if self.proxies and not direct and self._curl_cffi is None:
            kwargs["proxies"] = dict(self.proxies)
        return sess.get(url, **kwargs)

    def fetch(self, url: str, kind: str | None = None, referer: str | None = None,
              max_retries: int = 10, limiter: "RateLimiter | None" = None,
              label: str | None = None):
        """
        带重试的单次抓取。返回 response 或 None。
        退避策略：base * 2^(n-1) * 抖动，并优先遵守 Retry-After。
        """
        if kind is None:
            kind = detect_kind(url)
        label = label or url
        attempt = 0
        while True:
            attempt += 1
            headers = build_headers(self.profile, kind, referer, self.lang,
                                    native=self.native_headers)

            # 节流（默认零延时，acquire 直接放行）
            if limiter:
                limiter.acquire()

            try:
                resp = self._raw_get(url, headers)
            except Exception as e:
                # TLS 握手被中断：并发过高的信号，不当作代理故障，只提示降并发
                if _is_ssl_connect_error(e):
                    self._note_ssl_error()
                else:
                    self.note_network_result(False)
                    # 立即失败型（502/代理拒绝）：立刻换候选
                    # 超时型：累计到阈值再换（慢代理偶发超时是常态，不该动辄切换）
                    new_label, new_px = self.try_failover(urgent=_is_proxy_error(e))
                    if new_label is not None:
                        self.set_proxy(new_px)
                        back = min(2.0, self._backoff(1, None, limiter))
                        log(f"[RETRY] {label} 链路故障，"
                              f"换用 {self.proxy_resolver.describe()} 后重试")
                        time.sleep(back)
                        continue

                if self.proxies:
                    log(f"[INFO] 代理请求失败，尝试直连（保持同一浏览器指纹）: {url}")
                    try:
                        # 用同一个 impersonate 会话直连，只是不走代理
                        resp = self._raw_get(url, headers, direct=True)
                    except Exception as e:
                        if attempt >= max_retries:
                            log(f"[NETERR] {label} 失败 (重试 {max_retries} 次): {e}")
                            return None
                        back = self._backoff(attempt, None, limiter)
                        log(f"[RETRY] {label} {type(e).__name__}，退避 {back:.1f}s "
                              f"后重试 ({attempt}/{max_retries})")
                        time.sleep(back)
                        continue
                else:
                    if attempt >= max_retries:
                        log(f"[NETERR] {label} 失败 (重试 {max_retries} 次)")
                        return None
                    back = self._backoff(attempt, None, limiter)
                    log(f"[RETRY] {label} 退避 {back:.1f}s 后重试 ({attempt}/{max_retries})")
                    time.sleep(back)
                    continue

            code = resp.status_code
            self.note_network_result(True)

            if code in TERMINAL_CODES:
                log(f"[SKIP] 不可恢复状态 {label} (HTTP {code})")
                return None

            if code in SKIP_HTTP_CODES:
                if code in RETRYABLE_CODES or code in GUARD_CODES:
                    if attempt >= min(ERROR_RETRY_COUNT, max_retries):
                        log(f"[SKIP] 重试 {attempt} 次后仍失败: {label} (HTTP {code})")
                        return None
                    back = self._backoff(attempt, resp.headers.get("Retry-After"), limiter)
                    log(f"[RETRY] {label} (HTTP {code}) 退避 {back:.1f}s 后重试 "
                          f"({attempt}/{min(ERROR_RETRY_COUNT, max_retries)})")
                    time.sleep(back)
                    continue
                log(f"[SKIP] 错误页面 {label} (HTTP {code})")
                return None

            # 成功：修正编码判定
            # 只对文本类内容做字符集探测——对图片/字体跑 apparent_encoding
            # 是纯浪费（它要对整个 body 做字符分布统计），而且结果也没人用。
            if kind in ("document", "style", "script", "empty"):
                if (getattr(resp, "encoding", None) is None
                        or (resp.encoding or "").lower() == "iso-8859-1"):
                    head = resp.content[:4]
                    if head.startswith(b"\xef\xbb\xbf"):
                        resp.encoding = "utf-8-sig"
                    elif head.startswith(b"\xff\xfe"):
                        resp.encoding = "utf-16-le"
                    elif head.startswith(b"\xfe\xff"):
                        resp.encoding = "utf-16-be"
                    elif (getattr(resp, "encoding", None) is None
                            or (resp.encoding or "").lower() == "iso-8859-1"):
                        resp.encoding = resp.apparent_encoding
            return resp

    @staticmethod
    def _backoff(attempt: int, retry_after, limiter) -> float:
        """
        重试退避。默认关闭（返回 0，立刻重试），可用 --backoff 开启。

        开启时：指数退避 + 抖动；服务端给了 Retry-After 就照办
        （那是服务器指令，不属于拟真停顿）；自算退避压在 MAX_PAUSE 以内。
        """
        if limiter is not None and not limiter.backoff_enabled:
            return 0.0
        if retry_after:
            try:
                ra = float(retry_after)
                if 0 < ra <= 300:
                    return ra + random.uniform(0.5, 3.0)
            except (TypeError, ValueError):
                pass
        base = min(RateLimiter.MAX_PAUSE, 2.0 * (2 ** (attempt - 1)))
        wait = base * random.uniform(0.8, 1.4)
        if limiter:
            limiter.penalize(wait * 0.5)
        return wait


def parse_time_string(time_str: str) -> str:
    """
    解析各种时间格式，返回14位时间戳
    支持格式:
    - YYYYMMDDHHMMSS (14位)
    - YYYYMMDD (8位)
    - YYYY-MM-DD
    - YYYY/MM/DD
    - YYYY年MM月DD日
    - YYYY (年份)
    - YYYYMM (年月)
    """
    time_str = time_str.strip()

    # 已经是14位数字
    if re.match(r'^\d{14}$', time_str):
        return time_str

    # 8位数字 (YYYYMMDD)
    if re.match(r'^\d{8}$', time_str):
        return time_str + "000000"

    # 6位数字 (YYYYMM)
    if re.match(r'^\d{6}$', time_str):
        return time_str + "01000000"

    # 4位数字 (YYYY)
    if re.match(r'^\d{4}$', time_str):
        return time_str + "0101000000"

    # YYYY-MM-DD
    match = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', time_str)
    if match:
        return f"{match.group(1)}{match.group(2)}{match.group(3)}000000"

    # YYYY/MM/DD
    match = re.match(r'^(\d{4})/(\d{2})/(\d{2})$', time_str)
    if match:
        return f"{match.group(1)}{match.group(2)}{match.group(3)}000000"

    # YYYY年MM月DD日
    match = re.match(r'^(\d{4})年(\d{1,2})月(\d{1,2})日$', time_str)
    if match:
        return f"{match.group(1)}{int(match.group(2)):02d}{int(match.group(3)):02d}000000"

    # YYYY-MM
    match = re.match(r'^(\d{4})-(\d{2})$', time_str)
    if match:
        return f"{match.group(1)}{match.group(2)}01000000"

    # YYYY/MM
    match = re.match(r'^(\d{4})/(\d{2})$', time_str)
    if match:
        return f"{match.group(1)}{match.group(2)}01000000"

    # 尝试解析为datetime
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y年%m月%d日 %H:%M:%S"]:
        try:
            dt = datetime.strptime(time_str, fmt)
            return dt.strftime("%Y%m%d%H%M%S")
        except ValueError:
            continue

    raise ValueError(f"无法解析时间格式: {time_str}，请使用 YYYYMMDD 或 YYYY-MM-DD 格式")


def filter_entries_by_before(entries, before_ts, snapshot_field="timestamp"):
    """
    根据 before 时间过滤条目
    对于每个URL，找到最接近但不晚于 before_ts 的快照
    如果某个URL只有晚于 before_ts 的快照，则跳过该URL
    """
    # 按URL分组
    url_groups = {}
    for entry in entries:
        url = entry[0]
        ts = entry[3] if snapshot_field == "endtimestamp" else entry[2]
        # 确保时间戳是14位
        if len(ts) == 8:
            ts = ts + "000000"

        if url not in url_groups:
            url_groups[url] = []
        url_groups[url].append((ts, entry))

    filtered = []
    skipped_no_older = 0
    skipped_all_newer = 0

    for url, snapshots in url_groups.items():
        # 按时间戳排序（升序）
        snapshots.sort(key=lambda x: x[0])

        # 找到最接近但不晚于 before_ts 的快照
        best_entry = None
        best_ts = None

        for ts, entry in snapshots:
            if ts <= before_ts:
                best_entry = entry
                best_ts = ts
            else:
                # 因为已经排序，后面的都会更大
                break

        if best_entry is not None:
            filtered.append(best_entry)
            log(f"[FILTER] {url} -> 选择快照 {best_ts} (<= {before_ts})")
        else:
            # 检查是否有晚于 before_ts 的快照
            if snapshots:
                earliest_ts = snapshots[0][0]
                if earliest_ts > before_ts:
                    log(f"[SKIP] {url} 只有晚于 {before_ts} 的快照 (最早: {earliest_ts})")
                    skipped_all_newer += 1
                else:
                    # 理论上不应该到这里
                    log(f"[SKIP] {url} 没有合适的快照")
                    skipped_no_older += 1

    log(f"\n[SUMMARY] 原始条目: {len(entries)}, 过滤后: {len(filtered)}")
    log(f"         跳过的URL (只有更新的快照): {skipped_all_newer}")
    log(f"         跳过的URL (其他原因): {skipped_no_older}")

    return filtered


class WaybackListDownloader:
    def __init__(self, json_path, delay=0.0, output_dir="archive",
                 resume=True, proxy=None, snapshot_field="timestamp",
                 from_date=None, to_date=None, before_date=None, use_cdx_fallback=True,
                 profile=DEFAULT_PROFILE, lang=None, jitter=0.0, human_pause=False,
                 warmup=True, use_impersonate=True, timeout=60, order="list",
                 cookie_file=None, max_retries=10, http2=True, probe_proxy=True,
                 probe_timeout=20.0, probe_attempts=2, backoff=False,
                 progress_interval=15.0, snapshot_interval=300.0,
                 proxy_cache=True, proxy_cache_file=None):
        self.delay = delay
        self.resume = resume
        self.progress_interval = max(1.0, progress_interval)   # 进度打印间隔（秒）
        self.snapshot_interval = max(10.0, snapshot_interval)  # 完整快照间隔（秒）
        self._last_snapshot = time.time()
        self.snapshot_field = snapshot_field
        self.use_cdx_fallback = use_cdx_fallback
        self.from_date = self._ensure_14digit(from_date) if from_date else None
        self.to_date = self._ensure_14digit(to_date) if to_date else None
        self.before_date = self._ensure_14digit(before_date) if before_date else None
        self.max_retries = max_retries

        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.limiter = RateLimiter(delay, jitter=jitter, human_pause=human_pause,
                                   backoff=backoff)
        self.downloaded: set = set()
        self.url_to_local: dict[str, Path] = {}
        self.url_types: dict[str, str] = {}
        self.redirect_map: dict[str, str] = {}
        self.data_lock = threading.Lock()
        self.count_lock = threading.Lock()
        self.entry_referer: dict[str, str] = {}

        # 状态管理器
        self.state_manager = StateManager(self.output_dir / ".wayback_state.json")

        # 代理设置：优先用缓存，否则收集候选并实测，选真正能通的那条
        log("[INFO] 正在确定可用代理...")
        self.proxy_resolver = ProxyResolver(
            explicit=proxy, timeout=probe_timeout, attempts=probe_attempts,
            enable_probe=probe_proxy, use_cache=proxy_cache,
            cache_file=proxy_cache_file,
        )
        self.proxy_resolver.resolve()
        self.proxies = self.proxy_resolver.proxies
        log(f"[INFO] 代理: {self.proxy_resolver.describe()}")
        if len(self.proxy_resolver.candidates) > 1:
            log(f"[INFO] 候选链路共 {len(self.proxy_resolver.candidates)} 条，"
                  f"失败时会自动切换")

        # 拟真抓取器
        cookie_path = None
        if cookie_file:
            cookie_path = Path(cookie_file)
        elif resume:
            cookie_path = self.output_dir / ".wayback_cookies.txt"
        self.fetcher = RealisticFetcher(
            profile_name=profile, proxies=self.proxies, lang=lang,
            cookie_path=cookie_path, timeout=timeout, warmup=warmup,
            use_impersonate=use_impersonate, http2=http2,
        )
        self.fetcher.proxy_resolver = self.proxy_resolver
        # 启动时明确告知当前生效的拟真层级，便于判断是否需要调整
        log(f"[INFO] 浏览器画像: {profile} -> "
              f"{self.fetcher.profile.get('impersonate', '?')}")
        log(f"[INFO] 传输层: {self.fetcher.backend} | "
              f"HTTP/2: {'启用' if http2 else '关闭'} | "
              f"请求头: {'浏览器模板原生' if self.fetcher.native_headers else '手工构造'}")
        if self.fetcher.native_headers:
            log("[INFO] 已启用 TLS(JA3/JA4) + HTTP/2 指纹伪装，"
                  "请求头由浏览器模板生成")
        else:
            log("[INFO] 提示: 未启用 TLS 指纹伪装。"
                  "安装 curl_cffi 可获得最强拟真（pip install curl_cffi）")

        # 节流状态：默认全速，明确打印出来避免误以为在限速
        if not self.limiter.enabled and not backoff:
            log("[INFO] 节流: 已关闭（无请求间隔、无抖动、无休息、无退避）")
        else:
            parts = []
            if self.limiter.base_delay > 0:
                parts.append(f"基准 {self.limiter.base_delay}s")
            if jitter > 0:
                parts.append(f"抖动 ±{jitter:.0%}")
            if human_pause:
                parts.append("拟真驻留")
            if backoff:
                parts.append("重试退避")
            log(f"[INFO] 节流: {' / '.join(parts) if parts else '已关闭'}")

        # 扫描本地文件
        log("[INFO] 正在扫描本地文件...")
        self._scan_local_files()

        # 恢复状态
        if resume:
            loaded_downloaded, loaded_url_to_local, loaded_url_types = self.state_manager.load()
            self.downloaded.update(loaded_downloaded)
            self.url_to_local.update(loaded_url_to_local)
            self.url_types.update(loaded_url_types)
            log(f"[INFO] 断点续传已启用，已恢复 {len(loaded_downloaded)} 个资源")

        # 加载JSON列表
        with open(json_path, 'r', encoding='utf-8-sig') as f:
            raw = json.load(f)

        if isinstance(raw, list) and len(raw) > 0 and isinstance(raw[0], list) and raw[0][0] == "original":
            entries = raw[1:]
        else:
            entries = raw

        log(f"[INFO] 加载 {len(entries)} 个条目")

        # 时间范围过滤（from/to）
        if self.from_date or self.to_date:
            filtered_by_range = []
            for e in entries:
                ts = e[3] if self.snapshot_field == "endtimestamp" else e[2]
                ts = self._ensure_14digit(ts)
                if self.from_date and ts < self.from_date:
                    continue
                if self.to_date and ts > self.to_date:
                    continue
                filtered_by_range.append(e)
            log(f"[INFO] 时间范围过滤后剩余 {len(filtered_by_range)} 个")
            entries = filtered_by_range

        # before 过滤（每个URL选择最接近的快照）
        if self.before_date:
            log(f"[INFO] 应用 before 过滤: 选择 <= {self.before_date} 的最新快照")
            entries = filter_entries_by_before(entries, self.before_date, self.snapshot_field)
        else:
            which = "最早快照(timestamp)" if self.snapshot_field == "timestamp" else "最晚快照(endtimestamp)"
            log(f"[INFO] 未设置 before 参数，将使用列表中的{which}")

        # 与本地文件比对
        self.entries = entries

        # 开抓前先做本地去重：以磁盘文件为准，状态记录只作为快速通道
        by_state, by_file, redo = self._prefilter_existing()
        log(f"[INFO] 本地去重: 已有文件跳过 {by_state + by_file} 个"
            f"（状态记录 {by_state} / 文件系统 {by_file}）")
        if redo:
            log(f"[INFO] 其中 {redo} 个虽有记录但文件已丢失，将重新下载")
        log(f"[INFO] 剩余 {len(self.entries)} 个待下载")

        # 请求顺序：默认按列表顺序；page-first 模拟"先打开页面，再取资源"
        if order == "page-first":
            self.entries = self._sort_page_first(self.entries)
            log("[INFO] 请求顺序: 页面优先（HTML -> 样式/脚本 -> 图片/字体）")

        self.processed_count = 0

        # 预热：先建立会话再开抓
        first_url = self.entries[0][0] if self.entries else None
        self.fetcher.warmup_session(first_url)
        self._build_referer_map()

    # ---------- 辅助 ----------
    def _ensure_14digit(self, ts: str) -> str:
        ts = ts.strip()
        if len(ts) == 8:
            return ts + "000000"
        if len(ts) == 6:
            return ts + "01000000"
        if len(ts) == 4:
            return ts + "0101000000"
        return ts

    def _sort_page_first(self, entries):
        rank = {"document": 0, "style": 1, "script": 1, "image": 2, "font": 3, "empty": 4}
        return sorted(entries, key=lambda e: rank.get(detect_kind(e[0]), 5))

    def _build_referer_map(self):
        """
        为每个资源挑一个 Referer：优先同站点的入口页面，
        这与真实浏览器"从某页面点进来加载资源"的行为一致。
        """
        pages_by_netloc = {}
        for e in self.entries:
            u = e[0]
            if detect_kind(u) != "document":
                continue
            netloc = urllib.parse.urlparse(u).netloc
            pages_by_netloc.setdefault(netloc, u)
        for e in self.entries:
            u = e[0]
            netloc = urllib.parse.urlparse(u).netloc
            if detect_kind(u) == "document":
                self.entry_referer[u] = ARCHIVE_HOME
            else:
                self.entry_referer[u] = pages_by_netloc.get(netloc, ARCHIVE_HOME)

    def _scan_local_files(self):
        """
        扫描输出目录，建立"已存在文件"索引。

        注意这里**不再靠文件名反推 URL**（旧实现会把 http 写成 https、
        把 /index.html 还原成 /，导致根本匹配不上、形同虚设）。
        改成：先把磁盘上所有文件登记成索引，判断时拿 URL 正向算出目标路径去查。
        """
        self._existing_index = {}
        if not self.output_dir.exists():
            return

        count = 0
        for file_path in self.output_dir.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.name.startswith(".wayback"):
                continue
            try:
                rel = file_path.relative_to(self.output_dir).as_posix()
            except ValueError:
                continue
            self._existing_index[self._index_key(rel)] = file_path
            count += 1

        log(f"[INFO] 本地扫描完成，发现 {count} 个已存在的文件")

    @staticmethod
    def _index_key(rel_posix: str) -> str:
        """索引键：统一用 / 分隔；Windows 文件系统不区分大小写，故统一小写"""
        key = rel_posix.replace("\\", "/")
        return key.lower() if os.name == "nt" else key

    def _local_candidates(self, url: str) -> list:
        """
        由 URL 正向推导出「它可能被存成的本地相对路径」。
        逻辑与 _save_resource 保持一致（含无扩展名时按 MIME 补后缀的分支）。
        """
        try:
            p = urllib.parse.urlparse(url)
            netloc_dir = self._sanitize_path(p.netloc)
            path = urllib.parse.unquote(p.path)
        except Exception:
            return []
        if not netloc_dir:
            return []

        if not path or path == "/":
            path = "/index.html"

        out = []

        def add(safe_path: str):
            if safe_path:
                out.append(f"{netloc_dir}/{safe_path}")

        ext = posixpath.splitext(path)[1]
        if path.endswith("/") or not ext:
            # 可能是页面（补 index.html），也可能是无扩展名资源（按 MIME 补后缀）
            add(self._sanitize_path(path.rstrip("/") + "/index.html"))
            base = path.rstrip("/")
            add(self._sanitize_path(base))
            for e in (".bin", ".ico", ".html", ".htm", ".json", ".png", ".jpg", ".gif",
                      ".css", ".js", ".svg", ".webp", ".pdf", ".txt", ".xml"):
                add(self._sanitize_path(base + e))
        else:
            add(self._sanitize_path(path))
        return out

    def _find_existing_local(self, url: str):
        """本地已有该 URL 的成品文件吗？有则返回其路径"""
        if not self._existing_index:
            return None
        for rel in self._local_candidates(url):
            hit = self._existing_index.get(self._index_key(rel))
            if hit is not None:
                return hit
        return None

    def _rel_key_of(self, p) -> str | None:
        """把记录里的本地路径转成索引键（拿不到就返回 None，交给候选查找兜底）"""
        if p is None:
            return None
        try:
            rel = Path(p).relative_to(self.output_dir).as_posix()
        except Exception:
            return None
        return self._index_key(rel)

    def _prefilter_existing(self):
        """
        开抓之前先过一遍本地文件：已存在的直接跳过，不进下载队列。
        这样即使状态文件丢失，也不会重复下载。

        **以磁盘文件为最终裁判**：状态记录说下过了、但文件其实不在
        （被删了 / 换了目录 / 记录失效），就重新下载，而不是傻跳过。

        返回 (状态命中数, 文件系统命中数, 记录失效重下数)。
        """
        kept = []
        by_state = 0
        by_file = 0
        redo = 0

        for e in self.entries:
            url = e[0]

            if url in self.downloaded:
                # 再用记录路径去索引里核对（O(1)，不额外 stat 磁盘）
                if self._rel_key_of(self.url_to_local.get(url)) in self._existing_index:
                    by_state += 1
                    continue
                # 记录路径对不上，再按 URL 正向找一次
                hit = self._find_existing_local(url)
                if hit is not None:
                    self.url_to_local[url] = hit
                    self.url_types[url] = self._type_of_local(hit)
                    by_state += 1
                    continue
                # 状态说下过了，磁盘上却找不到 -> 记录失效，重新下载
                log(f"[WARN] 状态记录的文件已不存在，将重新下载: {url}")
                self.downloaded.discard(url)
                self.url_to_local.pop(url, None)
                self.url_types.pop(url, None)
                redo += 1
                kept.append(e)
                continue

            hit = self._find_existing_local(url)
            if hit is not None:
                self.downloaded.add(url)
                self.url_to_local[url] = hit
                ftype = self._type_of_local(hit)
                self.url_types[url] = ftype
                # 顺手补进日志，下次就能走状态记录快速跳过
                self.state_manager.record(url, hit, ftype)
                by_file += 1
                continue

            kept.append(e)

        self.entries = kept
        return by_state, by_file, redo

    @staticmethod
    def _type_of_local(file_path) -> str:
        ext = file_path.suffix.lower()
        if ext in (".html", ".htm"):
            return "html"
        if ext == ".css":
            return "css"
        return "binary"

    @staticmethod
    def _sanitize_path(path: str) -> str:
        parts = path.lstrip("/").split("/")
        cleaned = []
        for p in parts:
            p = p.strip(". ")
            p = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", p)
            if not p:
                p = "_"
            if p.lower() in _WIN_RESERVED:
                p = f"_{p}_"
            if len(p) > 200:
                name, ext = posixpath.splitext(p)
                p = name[:190] + ext
            cleaned.append(p)
        return "/".join(cleaned)

    @staticmethod
    def _remove_script_hiding(html: str) -> str:
        def repl(m):
            inner = m.group(2)
            inner = inner.replace('<!--', '').replace('//-->', '')
            return f'<script{m.group(1)}>{inner}</script>'
        return re.sub(r'<script(.*?)>(.*?)</script>', repl, html, flags=re.DOTALL | re.IGNORECASE)

    @staticmethod
    def _remove_ad_scripts(soup):
        for script in soup.find_all('script'):
            src = script.get('src', '')
            script_text = script.string or ''
            if any(pattern in src for pattern in AD_SCRIPT_PATTERNS):
                script.decompose()
                continue
            if any(pattern in script_text for pattern in AD_SCRIPT_PATTERNS):
                script.decompose()

    @staticmethod
    def _ensure_utf8_meta(soup):
        head = soup.find('head')
        if not head:
            head = soup.new_tag('head')
            if soup.html:
                soup.html.insert(0, head)
            else:
                html_tag = soup.new_tag('html')
                html_tag.append(head)
                soup.insert(0, html_tag)

        charset_meta = soup.find('meta', attrs={'charset': True})
        if charset_meta:
            charset_meta['charset'] = 'utf-8'
            return

        content_type_meta = soup.find('meta', attrs={'http-equiv': 'Content-Type'})
        if content_type_meta:
            content_type_meta['content'] = 'text/html; charset=utf-8'
            return

        meta = soup.new_tag('meta', charset='utf-8')
        head.insert(0, meta)

    @staticmethod
    def _write_html_utf8(local_path: Path, html_str: str):
        soup = BeautifulSoup(html_str, "lxml") if isinstance(html_str, str) else html_str

        WaybackListDownloader._ensure_utf8_meta(soup)

        output = str(soup)
        output = html.unescape(output)

        local_path.write_text(output, encoding='utf-8')

    # ==================== 修改点：URL 格式添加 id_ ====================
    def _wayback_url(self, original_url: str, timestamp: str) -> str:
        if not timestamp or not original_url:
            raise ValueError(f"Invalid timestamp or URL: ts={timestamp}, url={original_url}")
        # 添加 id_ 以获取原始资源，不注入 Wayback 交互脚本
        return f"{WAYBACK_BASE}/web/{timestamp}id_/{original_url}"
    # ================================================================

    def _canonical_url(self, url: str) -> str:
        p = urllib.parse.urlparse(url)
        return urllib.parse.urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, ""))

    def _resolve_url(self, url: str, base_url: str) -> str | None:
        if not url:
            return None
        url = url.strip()
        if url.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
            return None
        if url.startswith("//"):
            url = f"https:{url}"
        absolute = urllib.parse.urljoin(base_url, url)
        absolute = absolute.split("#")[0]
        return absolute or None

    def _is_same_domain(self, url: str, base_netloc: str) -> bool:
        p = urllib.parse.urlparse(url)
        if not p.netloc:
            return True
        netloc = p.netloc.lower().replace(":80", "").replace(":443", "")
        base = base_netloc.lower().replace(":80", "").replace(":443", "")
        return netloc == base or netloc == f"www.{base}" or base == f"www.{netloc}"

    @staticmethod
    def _content_type(resp):
        return resp.headers.get("Content-Type", "").lower().split(";")[0].strip()

    def _sniff_html(self, resp):
        ct = self._content_type(resp)
        if ct in ("text/html", "application/xhtml+xml"):
            return True
        if ct and not (ct.startswith("text/") or ct in ("application/octet-stream", "")):
            return False
        data = resp.content[:512]
        if data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]
        stripped = data.lstrip().lower()
        signatures = (
            b"<!doctype html", b"<html", b"<head", b"<body", b"<title",
            b"<script", b"<style", b"<link", b"<meta", b"<a ",
        )
        return any(stripped.startswith(sig) for sig in signatures)

    def _download_with_retry(self, original_url: str, timestamp: str, max_retries=10):
        """走拟真抓取器：带完整浏览器头 / Referer / 退避重试"""
        wayback_url = self._wayback_url(original_url, timestamp)
        kind = detect_kind(original_url)
        referer = self.entry_referer.get(original_url, ARCHIVE_HOME)
        return self.fetcher.fetch(
            wayback_url, kind=kind, referer=referer,
            max_retries=min(max_retries, self.max_retries),
            limiter=self.limiter, label=original_url,
        )

    def _save_resource(self, original_url, resp, timestamp, is_html=None):
        p = urllib.parse.urlparse(original_url)
        path = urllib.parse.unquote(p.path)

        if is_html is None:
            is_html = self._sniff_html(resp)
        is_css = False

        if not path or path == "/":
            path = "/index.html"

        if is_html:
            if not posixpath.splitext(path)[1] or path.endswith("/"):
                path = path.rstrip("/") + "/index.html"
            ftype = "html"
        else:
            ct = self._content_type(resp)
            ext = MIME_TO_EXT.get(ct, "")
            if ext:
                if not posixpath.splitext(path)[1]:
                    path += ext
            elif not posixpath.splitext(path)[1]:
                ext = posixpath.splitext(urllib.parse.urlparse(original_url).path)[1].lower()
                if not ext:
                    if 'favicon' in original_url.lower():
                        path += '.ico'
                    else:
                        path += '.bin'
            ftype = "binary"

        safe_path = self._sanitize_path(path)
        safe_netloc = self._sanitize_path(p.netloc)
        local_path = self.output_dir / safe_netloc / safe_path
        local_path.parent.mkdir(parents=True, exist_ok=True)

        if local_path.exists():
            log(f"[CACHE] 已存在，跳过写入: {local_path}")
        else:
            try:
                if is_html:
                    html_text = self._get_html_text(resp)
                    clean_html = self._remove_script_hiding(html_text)
                    soup = BeautifulSoup(clean_html, "lxml")
                    self._remove_ad_scripts(soup)
                    self._write_html_utf8(local_path, soup)
                else:
                    local_path.write_bytes(resp.content)
                log(f"[SAVE] {local_path}")
            except Exception as e:
                log(f"[ERROR] 保存失败 {local_path}: {e}")
                return None, None

        with self.data_lock:
            self.url_to_local[original_url] = local_path
            self.url_types[original_url] = ftype
            final_url = self._canonical_url(resp.url)
            if final_url != original_url:
                self.url_to_local[final_url] = local_path
                self.url_types[final_url] = ftype
                self.redirect_map[original_url] = final_url

        # 同步登记到索引，便于同一次运行内后续条目命中
        try:
            rel = local_path.relative_to(self.output_dir).as_posix()
            self._existing_index[self._index_key(rel)] = local_path
        except Exception:
            pass

        return local_path, ftype

    @staticmethod
    def _get_html_text(resp) -> str:
        try:
            return resp.content.decode('utf-8')
        except UnicodeDecodeError:
            pass

        content = resp.content[:4096]
        match = re.search(rb'charset=["\']?([a-zA-Z0-9\-_]+)["\']?', content, re.I)
        if match:
            charset = match.group(1).decode('ascii').lower()
            try:
                return resp.content.decode(charset)
            except:
                pass

        for encoding in ['shift-jis', 'euc-jp', 'cp932', 'gbk', 'big5']:
            try:
                return resp.content.decode(encoding)
            except:
                continue

        return resp.content.decode('utf-8', errors='replace')

    def process_entry(self, entry):
        original_url = entry[0]
        timestamp = entry[3] if self.snapshot_field == "endtimestamp" else entry[2]

        if original_url in self.downloaded:
            log(f"[SKIP] 已下载: {original_url}")
            return

        # 兜底：状态里没有，但磁盘上已经有成品文件 -> 直接跳过，不发请求
        hit = self._find_existing_local(original_url)
        if hit is not None:
            self.downloaded.add(original_url)
            self.url_to_local[original_url] = hit
            ftype = self._type_of_local(hit)
            self.url_types[original_url] = ftype
            self.state_manager.record(original_url, hit, ftype)
            log(f"[SKIP] 本地已存在: {hit}")
            return

        kind = detect_kind(original_url)
        log(f"\n[PROCESS] {original_url} (快照: {timestamp}, 类型: {kind})")

        resp = self._download_with_retry(original_url, timestamp)
        if resp is None:
            log(f"[SKIP] 下载失败: {original_url}")
            return

        local_path, ftype = self._save_resource(original_url, resp, timestamp)
        if not local_path:
            return

        self.downloaded.add(original_url)
        self.processed_count += 1
        # 只追加一行日志，避免整份状态重写（串行路径的主要开销来源）
        self.state_manager.record(original_url, local_path, ftype)

    @staticmethod
    def _fmt_eta(seconds: float) -> str:
        seconds = int(max(0, seconds))
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds//60}m{seconds%60:02d}s"
        return f"{seconds//3600}h{(seconds%3600)//60:02d}m"

    def run(self):
        """严格串行：一个请求发完才发下一个"""
        total = len(self.entries)
        if total == 0:
            log("[INFO] 没有待下载的条目")
            return

        start = time.time()
        next_progress = time.time() + self.progress_interval

        for i, entry in enumerate(self.entries, 1):
            self.process_entry(entry)

            now = time.time()
            if now >= next_progress:
                next_progress = now + self.progress_interval
                done = i
                elapsed = max(0.001, now - start)
                rate = done / elapsed
                eta = (total - done) / rate if rate > 0 else 0
                log(f"[PROGRESS] {done}/{total} ({done*100.0/total:.1f}%) | "
                    f"成功 {self.processed_count} | {rate:.2f} 条/秒 | "
                    f"已用 {self._fmt_eta(elapsed)} | 预计剩余 {self._fmt_eta(eta)}")

        self._flush_state(final=True)
        self.fetcher.save_cookies()

        elapsed = time.time() - start
        log(f"\n[INFO] 全部完成，共下载 {self.processed_count} 个文件，"
            f"耗时 {self._fmt_eta(elapsed)}"
            + (f"（平均 {elapsed/total*1000:.0f}ms/条）" if total else ""))
        log(f"[INFO] 文件保存在: {self.output_dir.absolute()}")

    def _flush_state(self, final: bool = False):
        """
        落盘策略（串行路径的关键优化）：

        原来每 30 条就把「全部已完成 URL + 路径映射」重写一遍，是 O(n²)：
        实测 11 万条时单次 932ms / 25MB，全程要写约 8.5GB、耗掉近半小时。
        现在改为：
          - 每完成一条只**追加一行**到 journal（几十字节，几乎零成本）
          - 完整快照只在超过 N 秒（默认 300s）或结束时写一次
        恢复时 = 快照 + 重放 journal。
        """
        if not self.resume:
            return
        if final:
            with self.data_lock:
                self.state_manager.save(self.downloaded, self.url_to_local, self.url_types)
            self.state_manager.close_journal()
            return
        if time.time() - self._last_snapshot < self.snapshot_interval:
            return
        self._last_snapshot = time.time()
        with self.data_lock:
            self.state_manager.save(self.downloaded, self.url_to_local, self.url_types)

    def _maybe_save(self):
        """每完成一条调用一次：追加 journal；按时间间隔偶尔写完整快照"""
        if not self.resume:
            return
        self.state_manager.flush_journal()
        self._flush_state()


def main():
    parser = argparse.ArgumentParser(
        description="Wayback Machine 列表下载器（时间回退版 + 真实浏览器环境拟真）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  # 默认：严格串行、不限速 + TLS(JA3/JA4)/HTTP2 指纹伪装\n"
               "  python 档案馆网页爬取列表版.py list.json --before 2020-01-01\n\n"
               "  # 需要拟真节奏时（默认都是关的）\n"
               "  python 档案馆网页爬取列表版.py list.json -d 0.5 --jitter 0.45 --human-pause --backoff\n\n"
               "  # 换浏览器指纹（macOS Chrome / Windows Edge / Firefox）\n"
               "  python 档案馆网页爬取列表版.py list.json --profile firefox147\n"
               "  python 档案馆网页爬取列表版.py list.json --profile edge101\n\n"
               "  # 关闭指纹伪装（回退 requests，无需 curl_cffi）\n"
               "  python 档案馆网页爬取列表版.py list.json --no-tls-fingerprint\n\n"
               "  # 强制直连（跳过所有代理）\n"
               "  python 档案馆网页爬取列表版.py list.json --proxy \"\"\n\n"
               "  # 手动指定代理\n"
               "  python 档案馆网页爬取列表版.py list.json --proxy 127.0.0.1:7890\n",
    )
    parser.add_argument("json_file", help="JSON 列表文件路径")
    parser.add_argument("-o", "--output", default="archive", help="输出目录")
    parser.add_argument("-d", "--delay", type=float, default=0.0,
                        help="请求基准间隔（秒，默认 0 = 不限速）")
    parser.add_argument("--progress", type=float, default=15.0, metavar="SEC",
                        help="进度打印间隔（秒，默认 15；0 关闭）")
    parser.add_argument("--snapshot-interval", type=float, default=300.0, metavar="SEC",
                        help="完整状态快照间隔（秒，默认 300）；平时只追加增量日志")
    parser.add_argument("--no-resume", action="store_true", help="禁用断点续传")
    parser.add_argument("--proxy", default=None, help="手动指定代理地址（留空字符串表示强制直连）")
    parser.add_argument("--no-proxy-probe", action="store_false", dest="probe_proxy",
                        help="跳过代理可用性探测，直接用第一个候选（启动更快）")
    parser.add_argument("--probe-timeout", type=float, default=20.0,
                        help="单个候选代理的探测超时（秒，默认 20）")
    parser.add_argument("--probe-attempts", type=int, default=2,
                        help="每个候选的探测次数，慢代理单次超时不判死（默认 2）")
    parser.add_argument("--no-proxy-cache", action="store_false", dest="proxy_cache",
                        help="不使用代理探测结果缓存（每次启动都重新探测）")
    parser.add_argument("--proxy-cache-ttl", type=float, default=1800.0, metavar="SEC",
                        help="代理缓存有效期（秒，默认 1800）")
    parser.add_argument("--snapshot-field", default="timestamp", choices=["timestamp", "endtimestamp"],
                        help="快照时间字段: timestamp(最早，默认) 或 endtimestamp(最晚)")
    parser.add_argument("--from", dest="from_date", default=None,
                        help="起始日期 (YYYYMMDD 或 YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", default=None,
                        help="结束日期 (格式同上)")
    parser.add_argument("--before", dest="before_date", default=None,
                        help="只下载 <= 该时间的快照，每个URL选择最接近的时间点 (如: 2020-01-01)")
    parser.add_argument("--no-cdx-fallback", action="store_false", dest="use_cdx_fallback",
                        help="禁止通过 CDX API 查找更早的快照")

    g = parser.add_argument_group("环境拟真")
    g.add_argument("--profile", default=DEFAULT_PROFILE, choices=sorted(BROWSER_PROFILES.keys()),
                   help=f"浏览器画像（默认 {DEFAULT_PROFILE}）")
    g.add_argument("--lang", default=None,
                   help="Accept-Language（默认跟随画像，如 ja-JP,ja;q=0.9,en;q=0.8）")
    g.add_argument("--no-tls-fingerprint", action="store_false", dest="use_impersonate",
                   help="不使用 curl_cffi 的 TLS/HTTP2 指纹伪装（回退 requests）")
    g.add_argument("--no-http2", action="store_false", dest="http2",
                   help="强制 HTTP/1.1（默认优先 h2，与真实 Chrome 一致）")
    g.add_argument("--no-warmup", action="store_false", dest="warmup",
                   help="不执行预热握手（不预先访问归档首页建立会话）")
    g.add_argument("--cookie-file", default=None,
                   help="Cookie jar 路径（默认输出目录下的 .wayback_cookies.txt）")
    g.add_argument("--jitter", type=float, default=0.0,
                   help="请求间隔抖动比例 0~1（默认 0，即无抖动）")
    g.add_argument("--human-pause", action="store_true", dest="human_pause",
                   help="开启随机驻留与周期休息（默认关闭；模拟真人看页面）")
    g.add_argument("--backoff", action="store_true", dest="backoff",
                   help="重试时启用指数退避（默认关闭，失败立即重试）")
    g.add_argument("--order", default="list", choices=["list", "page-first"],
                   help="请求顺序: list(列表顺序) 或 page-first(先页面后资源)")
    g.add_argument("--timeout", type=int, default=30, help="单请求超时（秒）")
    g.add_argument("--max-retries", type=int, default=10, help="单条目最大重试次数")
    args = parser.parse_args()

    # 解析时间参数
    from_date = None
    to_date = None
    before_date = None

    if args.from_date:
        from_date = parse_time_string(args.from_date)
        log(f"[INFO] 起始时间: {from_date}")

    if args.to_date:
        to_date = parse_time_string(args.to_date)
        log(f"[INFO] 结束时间: {to_date}")

    if args.before_date:
        before_date = parse_time_string(args.before_date)
        log(f"[INFO] before时间: {before_date} (只下载 <= 该时间的快照)")

    downloader = WaybackListDownloader(
        json_path=args.json_file,
        delay=args.delay,
        output_dir=args.output,
        progress_interval=args.progress if args.progress > 0 else 1e9,
        snapshot_interval=args.snapshot_interval,
        resume=not args.no_resume,
        proxy=args.proxy,
        snapshot_field=args.snapshot_field,
        from_date=from_date,
        to_date=to_date,
        before_date=before_date,
        use_cdx_fallback=args.use_cdx_fallback,
        profile=args.profile,
        lang=args.lang,
        jitter=args.jitter,
        human_pause=args.human_pause,
        backoff=args.backoff,
        warmup=args.warmup,
        use_impersonate=args.use_impersonate,
        timeout=args.timeout,
        order=args.order,
        cookie_file=args.cookie_file,
        max_retries=args.max_retries,
        http2=args.http2,
        probe_proxy=args.probe_proxy,
        probe_timeout=args.probe_timeout,
        probe_attempts=args.probe_attempts,
        proxy_cache=args.proxy_cache,
    )
    downloader.run()


if __name__ == "__main__":
    main()
