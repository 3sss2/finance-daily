# -*- coding: utf-8 -*-
"""
build_static.py —— 生成可以放到任意静态托管（GitHub Pages / 对象存储 / CDN）的版本

产出：
  public/index.html   页面（与本地服务同一份前端，自动切换到静态数据源）
  public/data.json    当轮抓取结果

用法：
  python3 build_static.py

由 .github/workflows/update.yml 调用：每 5 分钟抓一次快讯（FETCH_MODE=live），
每小时做一次全量（搜索、公众号、B 站等）。本地也可以手动跑。
"""

import hashlib
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
SEED_FILE = os.path.join(BASE_DIR, "seed.json")
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
    verified_retention = int(cfg.get("verified_retention_days", 90))
    now = datetime.now(CN)
    cutoff = (now - timedelta(days=retention)).strftime("%Y-%m-%d")
    verified_cutoff = (now - timedelta(days=verified_retention)).strftime("%Y-%m-%d")

    existing_ids = {i.get("id") for i in previous + seed}
    existing_urls = {i.get("url") for i in previous + seed if i.get("url")}
    existing_titles = {i.get("norm_title") for i in previous + seed if i.get("norm_title")}

    added, batch_ids, batch_titles = [], set(), set()
    for item in fetched:
        item_cutoff = verified_cutoff if item.get("verified") else cutoff
        if sort_stamp(item)[:10] < item_cutoff:
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

    merged = [i for i in previous
              if sort_stamp(i)[:10] >= (verified_cutoff if i.get("verified") else cutoff)]
    merged.extend(added)
    merged.sort(key=sort_stamp, reverse=True)

    limit = int(cfg.get("max_auto_items", 500))
    if len(merged) > limit:
        merged = merged[:limit]
    return seed, merged, len(added)


def item_fingerprint(items):
    """只看内容本身，不含生成时间，用来判断这一轮是否真的有变化"""
    blob = json.dumps(items, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def main():
    cfg = load_json(CONFIG_FILE, {})
    cfg.setdefault("live_limit", 40)
    cfg.setdefault("max_auto_items", 500)
    cfg.setdefault("retention_days", 10)

    previous_payload = load_json(OUT_DATA, {})
    previous = [i for i in (previous_payload.get("items") or [])
                if not fetchers.is_broken_link(i.get("url"))]
    previous_meta = previous_payload.get("meta") or {}

    live_only = os.environ.get("FETCH_MODE", "full").lower() == "live"
    fetched, errors, sources = fetchers.fetch_all(cfg, live_only=live_only)
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
    # 整理稿里已经收过的同一篇，不再重复出现（例如用户给的安佑那篇）
    seed_titles = {i.get("norm_title") or fetchers.norm_title(i.get("title", ""))
                   for i in seed}
    auto_items = [i for i in merged if i.get("kind") == "auto"
                  and (i.get("norm_title") or fetchers.norm_title(i.get("title", "")))
                  not in seed_titles]
    items = list(seed) + auto_items
    items.sort(key=lambda i: (sort_stamp(i), i.get("id") or ""), reverse=True)

    # 单一来源不许刷屏：每个来源最多留 N 条（整理稿不受限）
    cap = int(cfg.get("max_per_source", 120))
    kept, per_source = [], {}
    for item in items:
        source = (item.get("source") or "").split(" · ")[0]
        # 整理稿、官方发布、金融数据类不受条数上限影响
        if (item.get("kind") != "auto" or item.get("verified")
                or item.get("category") == "金融数据"):
            kept.append(item)
            continue
        if per_source.get(source, 0) >= cap:
            continue
        per_source[source] = per_source.get(source, 0) + 1
        kept.append(item)
    items = kept
    for item in items:
        fetchers.ensure_video_outline(item)

    with open(os.path.join(WEB_DIR, "index.html"), "rb") as fh:
        html_sha = hashlib.sha256(fh.read()).hexdigest()
    changed = (item_fingerprint(items) != item_fingerprint(previous)
               or html_sha != previous_meta.get("html_sha"))
    if changed or not previous_meta.get("last_updated"):
        last_updated = now.isoformat(timespec="seconds")
        version = int(now.timestamp())
    else:
        # 内容没变：保留上次的内容更新时间，页面上的「上次更新」才诚实
        last_updated = previous_meta.get("last_updated")
        version = previous_meta.get("version") or int(now.timestamp())

    meta = {
        "version": version,
        "last_updated": last_updated,
        "checked_at": now.isoformat(timespec="seconds"),
        "html_sha": html_sha,
        "next_update_at": (now + timedelta(minutes=5)).isoformat(timespec="seconds"),
        "interval_seconds": 300,
        "mode": "live" if live_only else "full",
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

    print("生成完成：%d 条（新增 %d，内容%s），输出 public/data.json"
          % (len(items), added, "有更新" if changed else "无变化"), flush=True)

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write("changed=%s\n" % ("true" if changed else "false"))


if __name__ == "__main__":
    main()
