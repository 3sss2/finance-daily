# -*- coding: utf-8 -*-
"""
server.py —— 财经日报 · 本地服务

做什么：
  * 把网页挂在 http://127.0.0.1:8775 ，打开就是最新内容，不用再下载 HTML
  * 后台每小时自动跑一轮采集（华尔街见闻 / 同花顺 / 东方财富 / 统计局 / B站）
  * 自动去重、按分类归档、保留 10 天，网页每 30 秒轮询一次新数据
  * 依赖：仅 Python 标准库

运行：python3 server.py
"""

import json
import os
import re
import signal
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)      # 允许 python3 -I 启动时也能导入同目录模块

import fetchers  # noqa: E402

WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = os.path.join(BASE_DIR, "data")
SEED_FILE = os.path.join(BASE_DIR, "seed.json")
MANUAL_FILE = os.path.join(DATA_DIR, "manual.json")
AUTO_FILE = os.path.join(DATA_DIR, "auto.json")
META_FILE = os.path.join(DATA_DIR, "meta.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

CN = timezone(timedelta(hours=8))
LOCK = threading.RLock()
FETCH_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------
def now_iso():
    return datetime.now(CN).isoformat(timespec="seconds")


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_config():
    cfg = load_json(CONFIG_FILE, {})
    cfg.setdefault("port", 8775)
    cfg.setdefault("refresh_interval_seconds", 3600)
    cfg.setdefault("retention_days", 10)
    cfg.setdefault("max_auto_items", 500)
    cfg.setdefault("live_limit", 40)
    return cfg


CFG = load_config()
INTERVAL = max(300, int(CFG["refresh_interval_seconds"]))


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------
def empty_meta():
    return {
        "version": 1,
        "last_updated": None,
        "next_update_at": None,
        "interval_seconds": INTERVAL,
        "last_run": None,
        "last_error": None,
        "history": [],
    }


AUTO = [i for i in load_json(AUTO_FILE, {"items": []}).get("items", [])
        if not fetchers.is_broken_link(i.get("url"))]
MANUAL = load_json(MANUAL_FILE, {"items": []}).get("items", [])
SEED = load_json(SEED_FILE, {"items": []}).get("items", [])
META = {**empty_meta(), **load_json(META_FILE, {})}
META["interval_seconds"] = INTERVAL      # 配置改了间隔，以配置文件为准
try:                                     # 旧状态里可能存着更远的下一轮，启动时收紧
    _nxt = datetime.fromisoformat(META["next_update_at"])
    if _nxt - datetime.now(CN) > timedelta(seconds=INTERVAL):
        META["next_update_at"] = (datetime.now(CN) + timedelta(seconds=INTERVAL)).isoformat(timespec="seconds")
except (TypeError, ValueError):
    pass

UPDATING = False
SCHEDULER_STOP = threading.Event()


def item_key(item):
    return item.get("id") or ""


def sort_stamp(item):
    """published 有时是「发布时间待核实」这类文字，取不到就退回收录时间"""
    published = item.get("published") or ""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[ T]?(\d{2}:\d{2})?", published)
    if m:
        return m.group(1) + " " + (m.group(2) or "00:00")
    collected = (item.get("collected") or "").replace("T", " ")
    return collected[:16] if collected[:4].isdigit() else "1970-01-01 00:00"


def visible_items():
    """seed（整理稿）+ 手动 + 自动，按发布时间倒序"""
    with LOCK:
        merged = list(SEED) + list(MANUAL) + list(AUTO)
    seed_titles = {i.get("norm_title") or fetchers.norm_title(i.get("title", ""))
                   for i in SEED}
    merged = [i for i in merged
              if not fetchers.is_broken_link(i.get("url"))
              and (i.get("kind") != "auto"
                   or (i.get("norm_title") or fetchers.norm_title(i.get("title", "")))
                   not in seed_titles)]
    seen, out = set(), []
    for item in merged:
        key = item_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    out.sort(key=sort_stamp, reverse=True)

    # 单一来源不许刷屏（整理稿和手动收录不受限）
    default_cap = int(CFG.get("max_per_source", 120))
    source_caps = CFG.get("source_caps") or {}
    kept, per_source = [], {}
    for item in out:
        source = (item.get("source") or "").split(" · ")[0]
        # 整理稿、官方发布、金融数据类不受条数上限影响
        if (item.get("kind") != "auto" or item.get("verified")
                or item.get("category") == "金融数据"):
            kept.append(item)
            continue
        cap = int(source_caps.get(source, default_cap))
        if per_source.get(source, 0) >= cap:
            continue
        per_source[source] = per_source.get(source, 0) + 1
        kept.append(item)
    for item in kept:
        fetchers.ensure_video_outline(item)
        fetchers.normalize_categories(item)
    return kept


def prune_auto(items):
    """按保留天数和条数上限裁剪自动抓取的条目；官方发布保留更久"""
    days = int(CFG.get("retention_days", 10))
    verified_days = int(CFG.get("verified_retention_days", 90))
    now = datetime.now(CN)
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    verified_cutoff = (now - timedelta(days=verified_days)).strftime("%Y-%m-%d")
    kept = []
    for item in items:
        published = (item.get("published") or "")[:10]
        limit = verified_cutoff if item.get("verified") else cutoff
        if published and published < limit:
            continue
        kept.append(item)
    limit = int(CFG.get("max_auto_items", 500))
    if len(kept) > limit:
        kept.sort(key=lambda i: i.get("published") or "", reverse=True)
        kept = kept[:limit]
    for item in kept:
        fetchers.ensure_video_outline(item)
        fetchers.normalize_categories(item)
    return kept


def counts_payload(items):
    by_cat = {}
    for item in items:
        by_cat[item.get("category") or "其他"] = by_cat.get(item.get("category") or "其他", 0) + 1
    last_hour = (datetime.now(CN) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    fresh = sum(1 for i in items if (i.get("collected") or "")[:16] >= last_hour)
    return {
        "total": len(items),
        "by_category": by_cat,
        "recent_hour": fresh,
        "auto": sum(1 for i in items if i.get("kind") == "auto"),
        "brief": sum(1 for i in items if i.get("kind") != "auto"),
    }


def build_payload():
    items = visible_items()
    with LOCK:
        meta = dict(META)
    return {
        "items": items,
        "meta": {**meta, "counts": counts_payload(items), "updating": UPDATING,
                 "generated_at": now_iso()},
    }


# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------
def run_fetch(reason="schedule", full=True):
    """跑一轮采集；同一时间只允许一个任务。

    full=False 时只抓两个 7x24 快讯源——5 分钟一轮的高频刷新走这条，
    搜索、公众号、B 站这些接口经不起高频请求，仍然每小时跑一次全量。
    """
    global AUTO, META, UPDATING
    if not FETCH_LOCK.acquire(blocking=False):
        return {"skipped": True, "message": "已有更新在进行中"}
    UPDATING = True
    started = time.time()
    try:
        print("[fetch] 开始（%s）%s" % (reason, datetime.now(CN).strftime("%H:%M:%S")),
              flush=True)
        fetched, errors, sources = fetchers.fetch_all(CFG, live_only=not full)

        with LOCK:
            existing_ids = {item_key(i) for i in AUTO}
            existing_ids |= {item_key(i) for i in SEED}
            existing_ids |= {item_key(i) for i in MANUAL}
            existing_urls = {i.get("url") for i in AUTO + SEED + MANUAL if i.get("url")}
            existing_titles = {i.get("norm_title") for i in AUTO + SEED + MANUAL
                               if i.get("norm_title")}
            added, batch_ids, batch_titles = [], set(), set()
            stale_cutoff = (datetime.now(CN) - timedelta(
                days=int(CFG.get("retention_days", 10)))).strftime("%Y-%m-%d")
            for item in fetched:
                if sort_stamp(item)[:10] < stale_cutoff:
                    continue        # 搜索接口会翻出旧文，超过保留期的不再入库
                key = item_key(item)
                title = item.get("norm_title")
                if key in existing_ids or key in batch_ids:
                    continue
                if item.get("url") and item["url"] in existing_urls:
                    continue
                if title and (title in existing_titles or title in batch_titles):
                    continue
                added.append(item)
                batch_ids.add(key)
                if title:
                    batch_titles.add(title)

            AUTO = prune_auto(added + AUTO)
            save_json(AUTO_FILE, {"items": AUTO})

        finished = datetime.now(CN)
        run_info = {
            "at": finished.isoformat(timespec="seconds"),
            "reason": reason,
            "fetched": len(fetched),
            "added": len(added),
            "errors": errors[:8],
            "error_count": len(errors),
            "sources": sources,
            "duration": round(time.time() - started, 1),
        }
        with LOCK:
            META["last_updated"] = run_info["at"]
            META["next_update_at"] = (finished + timedelta(seconds=INTERVAL)).isoformat(timespec="seconds")
            META["last_run"] = run_info
            if full:
                META["last_full_at"] = run_info["at"]
            META["last_error"] = errors[0] if errors else None
            META["version"] = int(META.get("version", 1)) + 1
            history = list(META.get("history") or [])[-23:]
            history.append({"at": run_info["at"], "added": run_info["added"],
                            "fetched": run_info["fetched"], "errors": len(errors)})
            META["history"] = history
            save_json(META_FILE, META)
        print("[fetch] 完成：抓取 %d 条，新增 %d 条，耗时 %.1fs，错误 %d 个"
              % (len(fetched), len(added), run_info["duration"], len(errors)), flush=True)
        return run_info
    except Exception as exc:
        detail = traceback.format_exc(limit=3)
        print("[fetch] 失败：%s\n%s" % (exc, detail), flush=True)
        with LOCK:
            META["last_error"] = str(exc)
            META["next_update_at"] = (datetime.now(CN) + timedelta(seconds=INTERVAL)).isoformat(timespec="seconds")
            META["version"] = int(META.get("version", 1)) + 1
            save_json(META_FILE, META)
        return {"error": str(exc)}
    finally:
        UPDATING = False
        FETCH_LOCK.release()


def scheduler_loop():
    """到点就抓；进程刚起来时如果数据过期也会先补一轮"""
    with LOCK:
        stale = True
        if META.get("last_updated"):
            try:
                last = datetime.fromisoformat(META["last_updated"])
                stale = (datetime.now(CN) - last).total_seconds() > INTERVAL
            except ValueError:
                stale = True
    if stale:
        run_fetch(reason="startup", full=True)
    while not SCHEDULER_STOP.is_set():
        with LOCK:
            nxt = META.get("next_update_at")
        delay = INTERVAL
        if nxt:
            try:
                delay = max(15, (datetime.fromisoformat(nxt) - datetime.now(CN)).total_seconds())
            except ValueError:
                delay = INTERVAL
        delay = min(delay, INTERVAL)     # 旧状态里可能存着更远的下一轮，按当前间隔收紧
        if SCHEDULER_STOP.wait(delay):
            break
        with LOCK:
            last_full = META.get("last_full_at")
        need_full = True
        if last_full:
            try:
                need_full = (datetime.now(CN) - datetime.fromisoformat(last_full)
                             ).total_seconds() >= 3600
            except ValueError:
                need_full = True
        run_fetch(reason="schedule", full=need_full)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "FinanceDaily/1.0"

    def log_message(self, fmt, *args):        # 静音常规访问日志
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/data":
            return self._send(200, build_payload())
        if path == "/api/health":
            return self._send(200, {"ok": True, "time": now_iso(), "updating": UPDATING,
                                    "version": int(META.get("version", 1))})
        if path == "/api/export":
            payload = build_payload()
            stamp = datetime.now(CN).strftime("%Y%m%d-%H%M")
            return self._send(
                200, json.dumps(payload, ensure_ascii=False, indent=1),
                extra={"Content-Disposition":
                       'attachment; filename="finance-daily-%s.json"' % stamp})
        if path in ("/", ""):
            path = "/index.html"
        return self._serve_static(path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/refresh":
            if FETCH_LOCK.locked():
                return self._send(202, {"ok": True, "started": False,
                                        "message": "已有更新在进行中"})
            threading.Thread(target=run_fetch,
                             kwargs={"reason": "manual", "full": True},
                             daemon=True).start()
            return self._send(202, {"ok": True, "started": True})

        if parsed.path == "/api/items":
            data = self._read_json()
            title = str(data.get("title") or "").strip()
            url = str(data.get("url") or "").strip()
            if not title or not url.startswith(("http://", "https://")):
                return self._send(400, {"ok": False, "error": "标题或链接不合法"})
            category = str(data.get("category") or "市场快讯")
            if category not in ("宏观数据", "公司财报", "公司动态", "视频财经", "市场快讯"):
                category = "市场快讯"
            item = {
                "id": "manual:%d" % int(time.time() * 1000),
                "kind": "manual",
                "category": category,
                "title": title[:180],
                "url": url,
                "source": "本页手动收录",
                "published": datetime.now(CN).strftime("%Y-%m-%d %H:%M"),
                "period": "",
                "collected": now_iso(),
                "summary": str(data.get("summary") or "")[:1200],
                "metrics": [],
                "verified": bool(data.get("verified")),
                "status": "手动收录 · 待核验",
                "norm_title": fetchers.norm_title(title),
            }
            with LOCK:
                if any(i.get("url") == url for i in MANUAL + AUTO + SEED):
                    return self._send(409, {"ok": False, "error": "该链接已收录"})
                MANUAL.append(item)
                save_json(MANUAL_FILE, {"items": MANUAL})
                META["version"] = int(META.get("version", 1)) + 1
                save_json(META_FILE, META)
            return self._send(200, {"ok": True, "item": item})

        return self._send(404, {"ok": False, "error": "not found"})

    def _serve_static(self, path):
        rel = urllib.parse.unquote(path).lstrip("/")
        target = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not target.startswith(WEB_DIR + os.sep) and target != WEB_DIR:
            return self._send(403, "forbidden", "text/plain; charset=utf-8")
        if not os.path.isfile(target):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ctype = CONTENT_TYPES.get(os.path.splitext(target)[1], "application/octet-stream")
        with open(target, "rb") as fh:
            body = fh.read()
        return self._send(200, body, ctype)


def main():
    port = int(os.environ.get("FINANCE_PORT") or CFG["port"])
    threading.Thread(target=scheduler_loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.daemon_threads = True

    def shutdown(signum, frame):
        print("\n[server] 收到退出信号，正在停止…", flush=True)
        SCHEDULER_STOP.set()
        # shutdown() 不能在 serve_forever() 所在的线程里直接调用，否则会卡住
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print("[server] 财经日报已启动：http://127.0.0.1:%d （每 %d 分钟更新一次）"
          % (port, INTERVAL // 60), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
