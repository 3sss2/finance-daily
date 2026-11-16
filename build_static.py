# -*- coding: utf-8 -*-
"""
build_static.py —— 生成可以放到任意静态托管（GitHub Pages / 对象存储 / CDN）的版本

产出：
  public/index.html   页面（与本地服务同一份前端，自动切换到静态数据源）
  public/data.json    当轮抓取结果

用法：
  python3 build_static.py

每小时由 .github/workflows/update.yml 调用一次；本地也可以手动跑。
"""

import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import fetchers  # noqa: E402

WEB_DIR = os.path.join(BASE_DIR, "web")
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
DATA_DIR = os.path.join(BASE_DIR, "data")
SEED_FILE = os.path.join(DATA_DIR, "seed.json")
AUTO_FILE = os.path.join(DATA_DIR, "auto.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
OUT_DATA = os.path.join(PUBLIC_DIR, "data.json")

CN = timezone(timedelta(hours=8))


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def sort_stamp(item):
    import re
    published = item.get("published") or ""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[ T]?(\d{2}:\d{2})?", published)
    if m:
        return m.group(1) + " " + (m.group(2) or "00:00")
    collected = (item.get("collected") or "").replace("T", " ")
    return collected[:16] if collected[:4].isdigit() else "1970-01-01 00:00"


def counts(items):
    by_cat = {}
    for item in items:
        cat = item.get("category") or "其他"
        by_cat[cat] = by_cat.get(cat, 0) + 1
    return {"total": len(items), "by_category": by_cat,
            "auto": sum(1 for i in items if i.get("kind") == "auto"),
            "brief": sum(1 for i in items if i.get("kind") != "auto")}


def merge(previous, fetched, cfg):
    """沿用上次的数据做底，合并新抓的内容，去重后按保留期裁剪"""
    seed = load_json(SEED_FILE, {"items": []}).get("items", [])
    retention = int(cfg.get("retention_days", 10))
    cutoff = (datetime.now(CN) - timedelta(days=retention)).strftime("%Y-%m-%d")

    existing_ids = {i.get("id") for i in previous + seed}
    existing_urls = {i.get("url") for i in previous + seed if i.get("url")}
    existing_titles = {i.get("norm_title") for i in previous + seed if i.get("norm_title")}

    added, batch_ids, batch_titles = [], set(), set()
    for item in fetched:
        if sort_stamp(item)[:10] < cutoff:
            continue
        key = item.get("id")
        title = item.get("norm_title")
        if not key or key in existing_ids or key in batch_ids:
            continue
        if item.get("url") and item["url"] in existing_urls:
            continue
        if title and (title in existing_titles or title in batch_titles):
            continue
        added.append(item)
        batch_ids.add(key)
        if title:
            batch_titles.add(title)

    merged = [i for i in previous if sort_stamp(i)[:10] >= cutoff]
    merged.extend(added)
    merged.sort(key=sort_stamp, reverse=True)

    limit = int(cfg.get("max_auto_items", 500))
    if len(merged) > limit:
        merged = merged[:limit]
    return seed, merged, len(added)


def main():
    cfg = load_json(CONFIG_FILE, {})
    cfg.setdefault("live_limit", 40)
    cfg.setdefault("max_auto_items", 500)
    cfg.setdefault("retention_days", 10)

    previous_payload = load_json(OUT_DATA, {})
    previous = previous_payload.get("items") or []
    previous_meta = previous_payload.get("meta") or {}

    fetched, errors, sources = fetchers.fetch_all(cfg)
    print("抓取 %d 条，错误 %d 个：%s" % (len(fetched), len(errors), sources), flush=True)

    seed, merged, added = merge(previous, fetched, cfg)

    if len(fetched) < 10 and previous:
        # 出口网络异常时不要用空数据覆盖线上内容
        print("本轮抓取过少（%d 条），保留上次数据" % len(fetched), flush=True)
        merged = previous
        added = 0
        status_note = "本轮数据源不可达，沿用上次内容"
    else:
        status_note = ""

    now = datetime.now(CN)
    items = list(seed) + [i for i in merged if i.get("kind") == "auto"]
    items.sort(key=sort_stamp, reverse=True)

    meta = {
        "version": int(now.timestamp()),
        "last_updated": now.isoformat(timespec="seconds"),
        "next_update_at": (now + timedelta(hours=1)).isoformat(timespec="seconds"),
        "interval_seconds": 3600,
        "last_run": {
            "at": now.isoformat(timespec="seconds"),
            "fetched": len(fetched),
            "added": added,
            "errors": errors[:8],
            "error_count": len(errors),
            "sources": sources,
        },
        "status_note": status_note,
        "counts": counts(items),
        "generated_at": now.isoformat(timespec="seconds"),
        "build": os.environ.get("GITHUB_RUN_ID") or "local",
    }

    os.makedirs(PUBLIC_DIR, exist_ok=True)
    with open(OUT_DATA, "w", encoding="utf-8") as fh:
        json.dump({"items": items, "meta": meta}, fh, ensure_ascii=False, indent=1)

    shutil.copyfile(os.path.join(WEB_DIR, "index.html"),
                    os.path.join(PUBLIC_DIR, "index.html"))
    # 本地服务也复用这批数据，避免两套内容不一致（CI 上没有 data/ 目录）
    if merged:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(AUTO_FILE, "w", encoding="utf-8") as fh:
            json.dump({"items": merged}, fh, ensure_ascii=False, indent=1)

    print("生成完成：%d 条（新增 %d），输出 public/data.json" % (len(items), added), flush=True)


if __name__ == "__main__":
    main()
