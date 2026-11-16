# -*- coding: utf-8 -*-
"""
fetchers.py —— 财经日报的数据采集器（仅标准库）

数据来源（都是公开接口，按小时轮询）：
  * 华尔街见闻 7x24 快讯
  * 同花顺财经 7x24 直播
  * 东方财富 资讯搜索（按跟踪公司 / 宏观关键词）
  * 华尔街见闻 文章搜索
  * 国家统计局「数据发布」最新稿件
  * B 站 视频搜索（财经关键词，可直接在网页内播放）

每次返回统一结构的 item，交给 server.py 去重、合并、落盘。
"""

import hashlib
import html as htmllib
import http.cookiejar
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))
_cookiejar = http.cookiejar.CookieJar()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

MACRO_WORDS = ("CPI", "PPI", "PMI", "GDP", "M1", "M2", "社融", "LPR", "央行",
               "统计局", "财政部", "降准", "降息", "逆回购", "MLF", "国债",
               "社会消费品零售", "固定资产投资", "进出口", "外贸", "人民币汇率",
               "物价", "就业", "失业率", "财政政策", "货币政策")
EARNINGS_WORDS = ("财报", "季报", "年报", "半年报", "业绩", "营收", "营业收入",
                  "净利润", "归母", "扣非", "预增", "预亏", "毛利率", "分红")
FINANCE_WORDS = ("社融", "社会融资", "M1", "M2", "新增信贷", "人民币贷款", "金融统计数据",
                 "存款", "信贷", "货币政策", "LPR", "逆回购", "MLF", "债券融资",
                 "票据融资", "非银贷款")

# 7x24 快讯里混着社会新闻，用财经词做一道粗筛
RELEVANT_WORDS = ("股", "债", "汇", "央行", "财政", "经济", "涨", "跌", "盘", "指数",
                  "银行", "保险", "券商", "基金", "期货", "黄金", "原油", "铜", "钢",
                  "煤", "锂", "芯片", "半导体", "光伏", "楼市", "房地产", "汽车",
                  "出口", "进口", "关税", "美联储", "通胀", "价格", "产量", "销量",
                  "融资", "回购", "IPO", "财报", "业绩", "营收", "利润", "投资",
                  "产能", "补贴", "监管", "政策", "能源", "电力", "算力", "消费",
                  "统计局", "财政部", "降准", "降息", "汇率", "市值")

# B 站搜索结果里个人 vlog 与券商广告很多，视频只留像财经内容的
VIDEO_TITLE_WORDS = ("A股", "港股", "美股", "大盘", "收盘", "涨停", "跌停", "财报",
                     "季报", "年报", "营收", "净利", "业绩", "央行", "降息", "降准",
                     "LPR", "CPI", "PPI", "PMI", "GDP", "社融", "M1", "M2", "基金",
                     "ETF", "债券", "债市", "汇率", "美联储", "关税", "半导体", "芯片",
                     "新能源", "光伏", "锂电", "白酒", "茅台", "比亚迪", "宁德", "腾讯",
                     "阿里", "银行", "券商", "保险", "地产", "楼市", "原油", "煤炭",
                     "市值", "估值", "通胀", "通缩", "加息", "板块")
VIDEO_TITLE_EXCLUDE = ("黄金眼", "游戏", "动漫", "鬼畜", "我的世界", "王者荣耀",
                       "演唱会", "直播回放", "合集", "预告片")

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"[ \t\r\f\v]+")
EMPTY_LINE_RE = re.compile(r"\n{3,}")


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def http_get(url, timeout=15, headers=None, retries=2):
    req_headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if headers:
        req_headers.update(headers)
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:          # 网络抖动时重试一次
            last_err = exc
            if attempt < retries:
                time.sleep(1.2 * (attempt + 1))
    raise last_err


def http_get_text(url, **kwargs):
    raw = http_get(url, **kwargs)
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def http_get_json(url, **kwargs):
    text = http_get_text(url, **kwargs).strip()
    if text.startswith("x("):             # 东方财富的 JSONP 包装
        text = text[2:]
        if text.endswith(")"):
            text = text[:-1]
    return json.loads(text)


def clean(text):
    if not text:
        return ""
    text = htmllib.unescape(str(text))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = TAG_RE.sub("", text)          # 行内标签直接去掉，避免每个词占一行
    text = text.replace(" ", " ").replace("　", " ")
    text = SPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return EMPTY_LINE_RE.sub("\n\n", text).strip()


def now_cn():
    return datetime.now(CN)


def fmt_ts(epoch):
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(int(epoch), CN).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return ""


def norm_title(text):
    """用于跨来源去重的标题指纹"""
    text = clean(text).lower()
    text = re.sub(r"[\s　，。、；：！？,.;:!?\"'“”‘’（）()\[\]【】《》—\-·|/\\]", "", text)
    return text[:70]


BROKEN_URL_MARKERS = (
    "/detail/152/",      # 早期按 id 拼的同花顺 7x24 详情，实际是 404
)


def is_broken_link(url):
    url = url or ""
    return any(marker in url for marker in BROKEN_URL_MARKERS)


def make_id(prefix, key):
    return prefix + ":" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def guess_category(text, source=""):
    blob = (text or "") + " " + (source or "")
    if any(word in blob for word in EARNINGS_WORDS):
        return "公司财报"
    if any(word in blob for word in FINANCE_WORDS):
        return "金融数据"
    if any(word in blob for word in MACRO_WORDS):
        return "宏观数据"
    return "市场快讯"


def extract_metrics(text, limit=4):
    """从正文里保守地抽取「指标 + 数值」，标注为自动抽取，避免把口径写死"""
    pattern = re.compile(
        r"([一-龥A-Za-z]{2,12}?(?:同比|环比)?)"
        r"(增长|下降|上涨|下跌|上升|回落|减少|增加|扩大|收窄|为)?"
        r"\s*(?:了)?\s*(-?\d+(?:\.\d+)?)\s*(%|个百分点|万亿元|亿元|万亿)"
    )
    keep = ("营收", "收入", "净利", "利润", "毛利", "CPI", "PPI", "PMI", "GDP",
            "M1", "M2", "社融", "社会融资", "融资", "贷款", "存款", "信贷", "票据",
            "出口", "进口", "投资", "零售", "消费", "产量", "销量", "价格",
            "指数", "增速", "市占")
    metrics, seen = [], set()
    for m in pattern.finditer(text):
        label = m.group(1).strip()
        if not any(word in label for word in keep):
            continue
        unit = m.group(4)
        value = m.group(3).replace("-", "−")
        key = (label, value, unit)
        if key in seen:
            continue
        seen.add(key)
        metrics.append({
            "label": label,
            "value": value,
            "unit": unit,
            "note": "自动抽取 · 原文「" + (m.group(0).strip()[:34] + ("…" if len(m.group(0).strip()) > 34 else "")) + "」",
        })
        if len(metrics) >= limit:
            break
    return metrics


def make_item(*, kind, category, title, url, source, summary, published="",
              period="", collected=None, status="", verified=False,
              metrics=None, extra=None, dedupe_key=None):
    key = dedupe_key or url or title
    item = {
        "id": make_id(kind, key),
        "kind": kind,                      # auto / brief / manual
        "category": category,
        "title": clean(title)[:180],
        "url": url,
        "source": source,
        "published": published,
        "period": period,
        "collected": collected or now_cn().isoformat(timespec="seconds"),
        "summary": clean(summary)[:1200],
        "metrics": metrics or [],
        "verified": bool(verified),
        "status": status,
        "norm_title": norm_title(title),
    }
    if extra:
        item.update(extra)
    return item


def _split_lead(text):
    """把「【标题】正文」拆成标题和正文"""
    text = clean(text)
    m = re.match(r"^[【\[]([^】\]]{4,80})[】\]]\s*(.*)$", text, re.S)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", text


def _shorten(text, n=90):
    text = clean(text).replace("\n", " ")
    return text if len(text) <= n else text[:n].rstrip() + "…"


def flash_title_summary(text):
    """快讯没有【标题】时，用第一句做标题，剩下的放进摘要"""
    lead, body = _split_lead(text)
    if lead:
        return lead, body
    if len(text) <= 60:
        return text, ""
    m = re.match(r"^(.{2,60}?[。！？])", text, re.S)
    if m:
        return m.group(1), text[len(m.group(1)):].strip()
    return text[:60].rstrip() + "…", text[60:].strip()


def is_relevant(text):
    return any(word in text for word in RELEVANT_WORDS)


OUTLINE_NOISE = re.compile(r"#\\S*|https?://\\S+|点击关注|关注我|一键三连|点赞|投币|收藏|转发|"
                           r"记得|欢迎订阅|更多精彩|视频加载中|【[^】]{0,12}】")
OUTLINE_SPLIT = re.compile(r"(?<=[。！？!?；;])\\s*")
FINANCE_TERMS = ("营收", "净利", "利润", "毛利", "股价", "市值", "估值", "财报", "季报",
                 "同比", "环比", "增长", "下滑", "下跌", "上涨", "板块", "指数", "大盘",
                 "成交", "资金", "北向", "主力", "机构", "基金", "债", "汇率", "利率",
                 "通胀", "CPI", "PPI", "PMI", "GDP", "社融", "美联储", "降息", "加息",
                 "政策", "产能", "销量", "订单", "库存", "价格")


def build_video_outline(title, desc, duration_s, tags=None):
    """用标题 + 简介做抽取式大纲（不读语音，没有字幕就靠这两样）"""
    desc = desc or ""
    if desc.startswith("B站视频《"):      # 我们自己生成的占位摘要，不是简介
        desc = ""
    text = OUTLINE_NOISE.sub(" ", clean(desc))
    lines = []
    for sent in OUTLINE_SPLIT.split(text):
        sent = sent.strip(" ，,、\t")
        if 8 <= len(sent) <= 90:
            lines.append(sent)

    def score(sent):
        hits = sum(1 for w in FINANCE_TERMS if w in sent)
        digits = sum(ch.isdigit() for ch in sent)
        return hits * 3 + min(digits, 12)

    picked, seen = [], set()
    for sent in sorted(lines, key=score, reverse=True):
        key = sent[:18]
        if key in seen:
            continue
        seen.add(key)
        picked.append(sent)
        if len(picked) >= 3:
            break

    if not picked:
        # 没有简介时，用标题的分句当要点
        title_clean = clean(title or "")
        parts = [p.strip() for p in re.split(r"[，,；;｜|：:？?！!]", title_clean)
                 if len(p.strip()) >= 4]
        if len(parts) >= 2:
            picked = parts[:3]
        else:
            return [{"type": "point", "text": "主题：" + title_clean},
                    {"type": "note", "text": "该视频没有文字简介，大纲据标题推断"}]

    outline = [{"type": "point", "text": p} for p in picked]
    tag_list = tags or []
    if tag_list:
        outline.append({"type": "tags", "text": "标签：" + "、".join(tag_list[:6])})
    else:
        mentions = sorted({w for w in FINANCE_TERMS if w in (title or "") + (desc or "")})[:6]
        if mentions:
            outline.append({"type": "tags", "text": "涉及：" + "、".join(mentions)})
    if duration_s:
        outline.append({"type": "meta", "text": "时长约 %d 分钟" % round(duration_s / 60)})
    return outline


def extract_article_image(page, base):
    """从正文里挑一张像图表的配图（东方财富的 np-newspic）"""
    import re as _re
    m = _re.search(r'https?://[^"\'\s]*(?:np-newspic|dfcfw)[^"\'\s]*\.(?:jpg|jpeg|png)', page, _re.I)
    if m:
        return m.group(0)
    return extract_og_image(page, base)


def enrich_images(items, limit=20, only_prefix="东方财富"):
    """给缺图的文章补一张首图；只处理最新的若干条，避免请求过多"""
    picked = [i for i in items
              if not i.get("image") and not i.get("bvid")
              and (i.get("source") or "").startswith(only_prefix)
              and (i.get("url") or "").startswith("http")]
    picked.sort(key=lambda i: i.get("published") or "", reverse=True)
    done = 0
    for item in picked[:limit]:
        try:
            page = http_get_text(item["url"], headers={"Referer": "https://finance.eastmoney.com/"})
            image = extract_article_image(page, item["url"])
            if image:
                item["image"] = image
                done += 1
        except Exception:
            continue
        time.sleep(0.3)
    return done


def normalize_categories(item):
    """历史数据里的热榜条目当时被归成「市场快讯」，统一改成「社媒热议」"""
    source = item.get("source") or ""
    if source.startswith(("抖音", "微博")):
        item["category"] = "社媒热议"
    return item


def ensure_video_outline(item):
    """给历史视频补大纲；已经是我们生成过的，按最新规则重算一次"""
    if not item.get("bvid"):
        return item
    ours = "标题与简介自动提炼，未读取视频语音"
    if item.get("outline") and item.get("outline_source") != ours:
        return item
    seconds = 0
    dur = item.get("duration") or ""
    if isinstance(dur, str) and ":" in dur:
        parts = [x for x in dur.split(":") if x.isdigit()]
        for part in parts:
            seconds = seconds * 60 + int(part)
    item["outline"] = build_video_outline(item.get("title", ""), item.get("summary", ""), seconds)
    item["outline_source"] = "标题与简介自动提炼，未读取视频语音"
    return item


def is_finance_video(title, duration, plays):
    """B 站粗筛：标题像财经、时长不短、播放量有基本盘"""
    if not any(word in title for word in VIDEO_TITLE_WORDS):
        return False
    if any(word in title for word in VIDEO_TITLE_EXCLUDE):
        return False
    if not duration or duration < 45:           # 十几秒的多是切片/广告
        return False
    if (plays or 0) < 300:                       # 兼顾中小博主，但不要零播放
        return False
    return True


# --------------------------------------------------------------------------
# 1. 华尔街见闻 7x24
# --------------------------------------------------------------------------
def fetch_wallstreetcn_lives(limit=40):
    url = ("https://api-one.wallstcn.com/apiv1/content/lives"
           "?channel=global-channel&client=pc&limit=%d" % limit)
    data = http_get_json(url, headers={"Referer": "https://wallstreetcn.com/"})
    items = []
    for row in (data.get("data") or {}).get("items") or []:
        content = clean(row.get("content") or "")
        if len(content) < 8 or not is_relevant(content):
            continue
        title, summary = flash_title_summary(content)
        lead = bool(_split_lead(content)[0])
        published = fmt_ts(row.get("display_time") or row.get("created_at"))
        st = row.get("score")
        items.append(make_item(
            kind="auto",
            category=guess_category(content, "华尔街见闻"),
            title=title,
            url=row.get("uri") or "https://wallstreetcn.com/livenews",
            source="华尔街见闻 · 7x24",
            summary=summary,
            published=published,
            status="自动抓取 · 待核验",
            metrics=extract_metrics(content, 4 if lead else 2),
            dedupe_key="wscn-live-%s" % (row.get("id") or norm_title(title)),
            extra={"hot": st} if isinstance(st, (int, float)) else None,
        ))
    return items


# --------------------------------------------------------------------------
# 2. 同花顺财经 7x24
# --------------------------------------------------------------------------
def fetch_ths_lives(size=40):
    url = ("https://zhibo.sina.com.cn/api/zhibo/feed"
           "?page=1&page_size=%d&zhibo_id=152&tag_id=0&dire=f&dpc=1" % size)
    data = http_get_json(url, headers={"Referer": "https://zhibo.sina.com.cn/"})
    rows = (((data.get("result") or {}).get("data") or {}).get("feed") or {}).get("list") or []
    items = []
    for row in rows:
        text = (row.get("rich_text") or "").replace("<.PSI>", "")
        lead, body = _split_lead(text)
        if len(lead + body) < 8 or not is_relevant(lead + body):
            continue
        title, summary = flash_title_summary(text)
        lead = bool(lead)
        # 详情页地址在 docurl 里；早先按 id 拼的 /detail/152/xxx 是 404
        docurl = (row.get("docurl") or "").strip()
        if not docurl:
            try:
                docurl = (json.loads(row.get("ext") or "{}") or {}).get("docurl") or ""
            except (ValueError, TypeError):
                docurl = ""
        if not docurl.startswith("http"):
            docurl = "https://zhibo.sina.com.cn/"
        items.append(make_item(
            kind="auto",
            category=guess_category(text, "同花顺"),
            title=title,
            url=docurl,
            source="同花顺财经 · 7x24",
            summary=summary,
            published=(row.get("create_time") or "")[:16],
            status="自动抓取 · 待核验",
            metrics=extract_metrics(text, 4 if lead else 2),
            dedupe_key="ths-live-%s" % (row.get("id") or norm_title(title)),
        ))
    return items


# --------------------------------------------------------------------------
# 3. 东方财富 资讯搜索
# --------------------------------------------------------------------------
def fetch_eastmoney_search(keyword, page_size=8):
    param = {
        "uid": "", "keyword": keyword,
        "type": ["cmsArticleWebOld"], "client": "web", "clientType": "web",
        "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "time",
            "pageIndex": 1, "pageSize": page_size}},
    }
    url = ("https://search-api-web.eastmoney.com/search/jsonp?cb=x&param="
           + urllib.parse.quote(json.dumps(param, ensure_ascii=False)))
    data = http_get_json(url, headers={"Referer": "https://so.eastmoney.com/"})
    rows = ((data.get("result") or {}).get("cmsArticleWebOld")) or []
    items = []
    for row in rows:
        title = clean(row.get("title") or "")
        summary = clean(row.get("content") or "")
        if not title:
            continue
        code = row.get("code") or ""
        link = ("https://finance.eastmoney.com/a/%s.html" % code) if code else ""
        if not link:
            continue
        items.append(make_item(
            kind="auto",
            category="公司财报" if any(w in title + summary for w in EARNINGS_WORDS)
            else ("宏观数据" if any(w in title + summary for w in MACRO_WORDS) else "公司动态"),
            title=title,
            url=link,
            source="东方财富 · 资讯（关键词：%s）" % keyword,
            summary=summary,
            published=(row.get("date") or "")[:16],
            status="自动抓取 · 待核验",
            metrics=extract_metrics(title + " " + summary),
            dedupe_key=link,
        ))
    return items


# --------------------------------------------------------------------------
# 4. 华尔街见闻 文章搜索
# --------------------------------------------------------------------------
def fetch_wallstreetcn_search(keyword, limit=8):
    url = ("https://api-one.wallstcn.com/apiv1/search/article?query=%s&limit=%d"
           % (urllib.parse.quote(keyword), limit))
    data = http_get_json(url, headers={"Referer": "https://wallstreetcn.com/"})
    rows = (data.get("data") or {}).get("items") or []
    items = []
    for row in rows:
        title = clean(row.get("title") or "")
        summary = clean(row.get("content_short") or row.get("content") or "")
        article_id = row.get("id")
        if not title or not article_id:
            continue
        items.append(make_item(
            kind="auto",
            category="公司财报" if any(w in title + summary for w in EARNINGS_WORDS)
            else ("宏观数据" if any(w in title + summary for w in MACRO_WORDS) else "公司动态"),
            title=title,
            url="https://wallstreetcn.com/articles/%s" % article_id,
            source="华尔街见闻 · 文章（关键词：%s）" % keyword,
            summary=summary,
            published=fmt_ts(row.get("display_time")),
            status="自动抓取 · 待核验",
            metrics=extract_metrics(title + " " + summary),
            dedupe_key="wscn-article-%s" % article_id,
        ))
    return items


# --------------------------------------------------------------------------
# 5. 国家统计局 数据发布
# --------------------------------------------------------------------------
def fetch_stats_releases(limit=10):
    url = "https://www.stats.gov.cn/sj/zxfb/"
    page = http_get_text(url, headers={"Referer": "https://www.stats.gov.cn/"})
    blocks = re.findall(
        r'<a class="fl pc_1600" href="([^"]+)"[^>]*title=\'([^\']+)\'.*?</a>\s*<span>\s*([\d\-]+)\s*</span>',
        page, re.S)
    items = []
    for href, title, date in blocks[:limit]:
        link = urllib.parse.urljoin(url, href)
        items.append(make_item(
            kind="auto",
            category="宏观数据",
            title=title,
            url=link,
            source="国家统计局 · 数据发布",
            summary="国家统计局最新发布稿件：%s。点击「查看原文」读取全文与分项数据。" % title,
            published=date,
            status="官方发布 · 待整理要点",
            verified=True,
            dedupe_key=link,
        ))
    return items


# --------------------------------------------------------------------------
# 6. B 站 视频搜索
# --------------------------------------------------------------------------
def _duration_seconds(text):
    parts = [p for p in str(text or "").split(":") if p.strip().isdigit()]
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total


# B 站接口对无 Cookie 的密集请求会返回 412，这里带 Cookie 并限速重试
_BILI_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_cookiejar))
_BILI_WARMED = False


def _bili_warm_up():
    global _BILI_WARMED
    if _BILI_WARMED:
        return
    try:
        req = urllib.request.Request("https://www.bilibili.com/", headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        with _BILI_OPENER.open(req, timeout=15) as resp:
            resp.read(2048)
        _BILI_WARMED = True
    except Exception:
        pass


def _bili_get_json(path):
    _bili_warm_up()
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://search.bilibili.com/",
        "Origin": "https://search.bilibili.com",
    }
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request("https://api.bilibili.com" + path,
                                         headers=headers)
            with _BILI_OPENER.open(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as exc:
            last = exc
            time.sleep(2.5 * (attempt + 1))    # 412 限流，退避后再试
        except Exception as exc:
            last = exc
            time.sleep(1.5)
    raise last


def fetch_bilibili_search(keyword, pages=1, max_age_days=10):
    """按关键词抓候选视频，交给 fetch_bilibili_videos 统一筛"""
    items, cutoff = [], time.time() - max_age_days * 86400
    for page in range(1, pages + 1):
        path = ("/x/web-interface/search/type?search_type=video&order=pubdate"
                "&page=%d&keyword=%s" % (page, urllib.parse.quote(keyword)))
        data = _bili_get_json(path)
        rows = (data.get("data") or {}).get("result") or []
        for row in rows:
            bvid = row.get("bvid") or ""
            if not bvid or not re.fullmatch(r"BV[0-9A-Za-z]+", bvid):
                continue
            if int(row.get("pubdate") or 0) < cutoff:
                continue
            title = clean(re.sub(r"</?em[^>]*>", "", row.get("title") or ""))
            pic = row.get("pic") or ""
            if pic.startswith("//"):
                pic = "https:" + pic
            desc = clean(row.get("description") or "")
            items.append({
                "bvid": bvid,
                "title": title,
                "author": row.get("author") or "未知作者",
                "pubdate": row.get("pubdate"),
                "plays": row.get("play") or 0,
                "duration": row.get("duration"),
                "duration_s": _duration_seconds(row.get("duration")),
                "thumb": pic,
                "desc": desc,
                "keyword": keyword,
            })
    return items


def fetch_bilibili_videos(keywords, total=12, max_per_author=2):
    """汇总多个关键词的候选，按财经相关度与新鲜度挑出要展示的视频"""
    candidates, errors = [], []
    for idx, keyword in enumerate(keywords):
        try:
            if idx:
                time.sleep(1.0)        # 关键词之间留间隔，避免触发限流
            candidates.extend(fetch_bilibili_search(keyword))
        except Exception as exc:
            errors.append("B站·%s：%s" % (keyword, exc))

    picked, author_count, seen = [], {}, set()
    candidates.sort(key=lambda c: c.get("pubdate") or 0, reverse=True)
    for row in candidates:
        if row["bvid"] in seen:
            continue
        if not is_finance_video(row["title"], row["duration_s"], row["plays"]):
            continue
        if author_count.get(row["author"], 0) >= max_per_author:
            continue
        seen.add(row["bvid"])
        author_count[row["author"]] = author_count.get(row["author"], 0) + 1
        tags = []
        try:
            time.sleep(0.4)
            tag_data = _bili_get_json("/x/tag/archive/tags?bvid=" + row["bvid"])
            tags = [t.get("tag_name") for t in (tag_data.get("data") or []) if t.get("tag_name")][:6]
        except Exception:
            tags = []
        picked.append(make_item(
            kind="auto",
            category="视频财经",
            title=row["title"],
            url="https://www.bilibili.com/video/%s" % row["bvid"],
            source="B站 · %s" % row["author"],
            summary=row["desc"] or "B站视频《%s》，可直接在本页播放。" % row["title"],
            published=fmt_ts(row["pubdate"]),
            status="视频已收录 · 内容待整理",
            verified=False,
            dedupe_key="bili-%s" % row["bvid"],
            extra={
                "bvid": row["bvid"],
                "thumb": row["thumb"],
                "plays": row["plays"],
                "duration": row["duration"],
                "outline": build_video_outline(row["title"], row["desc"], row["duration_s"], tags),
                "tags": tags,
                "outline_source": "标题与简介自动提炼，未读取视频语音",
            },
        ))
        if len(picked) >= total:
            break
    return picked, errors


# --------------------------------------------------------------------------
# 7. 中国人民银行官方数据（社融、金融统计数据、货币政策）
# --------------------------------------------------------------------------
PBC_STATS_URL = "https://www.pbc.gov.cn/diaochatongjisi/116219/116225/index.html"
PBC_NEWS_URL = "https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html"


def extract_og_image(page, base):
    import re as _re
    for pat in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image'):
        m = _re.search(pat, page, _re.I)
        if m:
            return urllib.parse.urljoin(base, htmllib.unescape(m.group(1)))
    return ""


def _pbc_article_text(url, limit=900):
    """抓央行正文，去掉导航和按钮文字"""
    page = http_get_text(url, headers={"Referer": "https://www.pbc.gov.cn/"})
    return _pbc_text_from_html(page, limit)


def _pbc_text_from_html(page, limit=900):
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", page)
    text = htmllib.unescape(re.sub(r"<[^>]+>", "\n", body))
    for marker in ("关闭窗口", "打印本页"):
        idx = text.find(marker)
        if idx > 0:
            text = text[idx + len(marker):]
            break
    text = "\n".join(line.strip() for line in text.split("\n") if line.strip())
    for tail in ("法律声明", "©", "网站地图", "打印本页"):
        idx = text.find(tail, 30)
        if idx > 0:
            text = text[:idx]
    return text[:limit]


def _fetch_pbc_listing(list_url, keywords, category, source_label, with_body=True, limit=12):
    page = http_get_text(list_url, headers={"Referer": "https://www.pbc.gov.cn/"})
    rows = re.findall(
        r'<a href="([^"]+)"[^>]*?\stitle="([^"]+)"[^>]*>[^<]*</a></font>\s*<span class="hui12">\s*([\d\-]+)\s*</span>',
        page)
    items = []
    for href, title, date in rows[:limit]:
        if keywords and not any(word in title for word in keywords):
            continue
        if "统计表" in title:      # 只有附件表格，正文没有可读内容
            continue
        link = urllib.parse.urljoin(list_url, href)
        summary = "中国人民银行官方发布：%s。" % title
        image = ""
        if with_body:
            try:
                page_html = http_get_text(link, headers={"Referer": "https://www.pbc.gov.cn/"})
                body = _pbc_text_from_html(page_html)
                if len(body) > 40:
                    summary = body
                image = extract_og_image(page_html, link)
            except Exception:
                pass
        items.append(make_item(
            kind="auto",
            category=category,
            title=title,
            url=link,
            source=source_label,
            summary=summary,
            published=date,
            status="官方发布 · 已核对原文",
            verified=True,
            metrics=extract_metrics(summary, 4),
            dedupe_key=link,
            extra={"image": image} if image else None,
        ))
    return items


def fetch_pbc_statistics():
    """央行调查统计司：月度金融统计数据报告、社会融资规模统计表"""
    return _fetch_pbc_listing(
        PBC_STATS_URL,
        ("金融统计数据报告", "社会融资规模", "金融数据"),
        "金融数据",
        "中国人民银行 · 调查统计司",
        with_body=True)


def fetch_pbc_news():
    """央行新闻发布：货币政策执行报告、LPR、降准降息等"""
    return _fetch_pbc_listing(
        PBC_NEWS_URL,
        ("货币政策执行报告", "LPR", "贷款市场报价利率", "降准", "降息", "逆回购",
         "金融统计数据报告", "社会融资", "金融支持", "结构性货币政策"),
        "宏观数据",
        "中国人民银行 · 新闻发布",
        with_body=True,
        limit=20)


# --------------------------------------------------------------------------
# 8. 东方财富标题搜索（只匹配标题，避免关键词被拆开后的噪音）
# --------------------------------------------------------------------------
def fetch_eastmoney_title_search(keyword, page_size=6):
    param = {
        "uid": "", "keyword": keyword,
        "type": ["cmsArticleWebOld"], "client": "web", "clientType": "web",
        "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "title", "sort": "time",
            "pageIndex": 1, "pageSize": page_size}},
    }
    url = ("https://search-api-web.eastmoney.com/search/jsonp?cb=x&param="
           + urllib.parse.quote(json.dumps(param, ensure_ascii=False)))
    data = http_get_json(url, headers={"Referer": "https://so.eastmoney.com/"})
    rows = ((data.get("result") or {}).get("cmsArticleWebOld")) or []
    items = []
    for row in rows:
        title = clean(row.get("title") or "")
        summary = clean(row.get("content") or "")
        code = row.get("code") or ""
        if not title or not code:
            continue
        link = "https://finance.eastmoney.com/a/%s.html" % code
        items.append(make_item(
            kind="auto",
            category=guess_category(title + " " + summary, "东方财富"),
            title=title,
            url=link,
            source="东方财富 · 标题搜索（%s）" % keyword,
            summary=summary,
            published=(row.get("date") or "")[:16],
            status="自动抓取 · 待核验",
            metrics=extract_metrics(title + " " + summary),
            dedupe_key=link,
        ))
    return items


# --------------------------------------------------------------------------
# 9. 微信公众号（经搜狗微信检索）
#   公众号没有公开接口，这里只取标题、公众号名、时间和摘要；
#   原文链接给出搜狗检索页，点开即可在微信里阅读。
# --------------------------------------------------------------------------
def fetch_wechat_search(keyword, max_items=5, max_age_days=10):
    url = ("https://weixin.sogou.com/weixin?type=2&query="
           + urllib.parse.quote(keyword))
    page = http_get_text(url, headers={
        "Referer": "https://weixin.sogou.com/",
        "Accept": "text/html,application/xhtml+xml",
    })
    if "用户您好，您的访问过于频繁" in page or "验证码" in page:
        raise RuntimeError("搜狗微信要求验证，稍后重试")
    blocks = re.split(r'<li id="sogou_vr_', page)[1:]
    cutoff = time.time() - max_age_days * 86400
    items = []
    for block in blocks:
        m_title = re.search(r'<h3>\s*<a[^>]*>(.*?)</a>', block, re.S)
        m_sum = re.search(r'<p class="txt-info"[^>]*>(.*?)</p>', block, re.S)
        m_acct = re.search(r'<span class="all-time-y2">(.*?)</span>', block)
        m_img = re.search(r'<div class="img-box">.*?<img[^>]+src="([^"]+)"', block, re.S)
        m_ts = re.search(r"timeConvert\('(\d+)'\)", block)
        if not (m_title and m_acct and m_ts):
            continue
        title = clean(m_title.group(1))
        if not title or len(title) < 4:
            continue
        ts = int(m_ts.group(1))
        if ts < cutoff:
            continue
        account = clean(m_acct.group(1))
        summary = clean(m_sum.group(1)) if m_sum else ""
        image = ""
        if m_img:
            proxy = htmllib.unescape(m_img.group(1))
            if proxy.startswith("//"):
                proxy = "https:" + proxy
            params = urllib.parse.parse_qs(urllib.parse.urlparse(proxy).query)
            image = (params.get("url") or [""])[0]     # 搜狗缩略图里带着原图地址
        items.append(make_item(
            kind="auto",
            category=guess_category(title + " " + summary, account),
            title=title,
            url="https://weixin.sogou.com/weixin?type=2&query=" + urllib.parse.quote(title),
            source="微信公众号 · %s" % account,
            summary=summary,
            published=fmt_ts(ts),
            status="公众号转载 · 点开可在微信中阅读全文",
            verified=False,
            metrics=extract_metrics(title + " " + summary),
            dedupe_key="wx-%s-%s" % (account, norm_title(title)),
            extra={"image": image} if image else None,
        ))
        if len(items) >= max_items:
            break
    return items


# --------------------------------------------------------------------------
# 10. 微信公众号 · 按账号轮询
#   搜狗微信对同一 IP 有频次限制，一次只查几个账号，按分钟轮换。
# --------------------------------------------------------------------------
def fetch_wechat_accounts(accounts, slots=3, max_age_days=14):
    if not accounts:
        return []
    now = time.localtime()
    start = (now.tm_hour * 60 + now.tm_min) // 5 * slots
    picked = [accounts[(start + i) % len(accounts)] for i in range(min(slots, len(accounts)))]

    items, errors = [], []
    for idx, account in enumerate(picked):
        try:
            if idx:
                time.sleep(12)     # 搜狗对同 IP 有频控，间隔拉长一点
            items.extend(fetch_wechat_search(account, 4, max_age_days=max_age_days))
        except Exception as exc:
            msg = str(exc)
            if "验证" in msg or "频繁" in msg:
                time.sleep(30)     # 被要求验证时等一会儿再试一次
                try:
                    items.extend(fetch_wechat_search(account, 4, max_age_days=max_age_days))
                    continue
                except Exception as exc2:
                    exc = exc2
            errors.append("微信·%s：%s" % (account, exc))
    return items, errors, picked


# --------------------------------------------------------------------------
# 11. 抖音热榜（不需要登录的公开接口）
#   只有话题词，没有单个博主的视频；用财经关键词过滤，保留市场相关的条目。
# --------------------------------------------------------------------------
DOUYIN_FINANCE_WORDS = ("股", "黄金", "金价", "油价", "原油", "央行", "降息", "降准",
                        "汇率", "人民币", "房价", "楼市", "地产", "基金", "债",
                        "保险", "银行", "茅台", "比亚迪", "宁德", "腾讯", "阿里",
                        "京东", "英伟达", "美联储", "关税", "GDP", "CPI", "PPI",
                        "PMI", "社融", "消费", "外贸", "出口", "进口", "新能源",
                        "芯片", "半导体", "就业", "物价", "通胀", "市值", "财报",
                        "业绩", "上市", "市值", "贬值", "升值")


def fetch_douyin_hot():
    url = ("https://www.douyin.com/aweme/v1/web/hot/search/list/"
           "?device_platform=webapp&aid=6383&channel=channel_pc_web&detail_list=1")
    data = http_get_json(url, headers={"Referer": "https://www.douyin.com/"})
    words = ((data.get("data") or {}).get("word_list")) or []
    items = []
    for row in words:
        word = (row.get("word") or "").strip()
        if not word or not any(w in word for w in DOUYIN_FINANCE_WORDS):
            continue
        hot = row.get("hot_value") or 0
        rank = row.get("position")
        items.append(make_item(
            kind="auto",
            category="社媒热议",
            title=word,
            url="https://www.douyin.com/search/%s" % urllib.parse.quote(word),
            source="抖音 · 热榜",
            summary="抖音热榜%s，热度 %s，相关视频 %s 条。点击查看话题下大家在讨论什么。"
                    % (("第 %s 位" % rank) if rank else "",
                       ("%.1f 万" % (hot / 10000.0)) if hot else "未知",
                       row.get("video_count") or 0),
            published=now_cn().strftime("%Y-%m-%d %H:%M"),
            status="热榜话题 · 非深度内容",
            verified=False,
            dedupe_key="dy-hot-%s" % (row.get("sentence_id") or word),
        ))
    return items


# --------------------------------------------------------------------------
# 12. 网易财经 · 滚动新闻
# --------------------------------------------------------------------------
def fetch_sina_roll(num=20):
    url = ("https://feed.mix.sina.com.cn/api/roll/get"
           "?pageid=153&lid=2516&num=%d&page=1" % num)
    data = http_get_json(url, headers={"Referer": "https://money.sina.com.cn/"})
    rows = ((data.get("result") or {}).get("data")) or []
    items = []
    for row in rows:
        title = clean(row.get("title") or "")
        link = row.get("url") or ""
        if not title or not link.startswith("http"):
            continue
        summary = clean(row.get("summary") or row.get("intro") or "")
        items.append(make_item(
            kind="auto",
            category=guess_category(title + " " + summary, "网易财经"),
            title=title,
            url=link,
            source="网易财经 · %s" % (row.get("media_name") or "滚动新闻"),
            summary=summary,
            published=fmt_ts(row.get("ctime")),
            status="自动抓取 · 待核验",
            metrics=extract_metrics(title + " " + summary),
            dedupe_key=link,
        ))
    return items


# --------------------------------------------------------------------------
# 13. 36 氪 · 快讯（经 RSSHub 公共实例）
# --------------------------------------------------------------------------
def fetch_36kr_flash(limit=15):
    url = "https://rsshub.rssforever.com/36kr/newsflashes"
    raw = http_get_text(url, headers={"Referer": "https://rsshub.rssforever.com/"})
    import xml.etree.ElementTree as ET
    root = ET.fromstring(raw)
    items = []
    for node in root.iter("item"):
        title = clean(node.findtext("title") or "")
        link = (node.findtext("link") or "").strip()
        desc = clean(node.findtext("description") or "")
        pub = (node.findtext("pubDate") or "").strip()
        published = ""
        if pub:
            try:
                from email.utils import parsedate_to_datetime
                published = parsedate_to_datetime(pub).astimezone(CN).strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                published = pub[:16]
        if not title or not link.startswith("http"):
            continue
        items.append(make_item(
            kind="auto",
            category=guess_category(title + " " + desc, "36氪"),
            title=title,
            url=link,
            source="36氪 · 快讯",
            summary=desc,
            published=published,
            status="自动抓取 · 待核验",
            metrics=extract_metrics(title + " " + desc),
            dedupe_key=link,
        ))
        if len(items) >= limit:
            break
    return items


# --------------------------------------------------------------------------
# 14. 微博 热搜（公开接口，无需登录）
# --------------------------------------------------------------------------
def fetch_微博_hot():
    url = "https://weibo.com/ajax/side/hotSearch"
    data = http_get_json(url, headers={"Referer": "https://weibo.com/"})
    rows = ((data.get("data") or {}).get("realtime")) or []
    items = []
    for row in rows:
        word = (row.get("word") or row.get("note") or "").strip()
        if not word or not any(w in word for w in DOUYIN_FINANCE_WORDS):
            continue
        num = row.get("num") or 0
        items.append(make_item(
            kind="auto",
            category="社媒热议",
            title=word,
            url="https://s.weibo.com/微博?q=" + urllib.parse.quote("#%s#" % word),
            source="微博 · 热搜",
            summary="微博 热搜话题，讨论量约 %s。点开看大家在聊什么。" % (
                ("%.1f 万" % (num / 10000.0)) if num else "未知"),
            published=now_cn().strftime("%Y-%m-%d %H:%M"),
            status="热搜话题 · 非深度内容",
            verified=False,
            dedupe_key="wb-hot-%s" % word,
        ))
    return items


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------
def fetch_all(cfg, live_only=False):
    """按配置抓一轮，返回 (items, errors, stats)

    live_only=True 时只抓两个 7x24 快讯源，用于每 5 分钟的高频轮询；
    搜索、公众号、B 站这些接口经不起高频请求，只在整点的全量轮次里跑。
    """
    started = time.time()
    items, errors, stats = [], [], {}

    def run(label, fn, *args, **kwargs):
        try:
            got = fn(*args, **kwargs)
            stats[label] = len(got)
            items.extend(got)
        except Exception as exc:
            errors.append("%s：%s" % (label, exc))
            stats[label] = 0

    run("华尔街见闻7x24", fetch_wallstreetcn_lives, cfg.get("live_limit", 40))
    run("同花顺7x24", fetch_ths_lives, cfg.get("live_limit", 40))

    if live_only:
        stats["耗时秒"] = round(time.time() - started, 1)
        return items, errors, stats

    run("国家统计局", fetch_stats_releases)
    run("抖音热榜", fetch_douyin_hot)
    run("微博热搜", fetch_微博_hot)
    run("网易财经", fetch_sina_roll, 20)
    run("36氪快讯", fetch_36kr_flash)
    run("央行·统计数据", fetch_pbc_statistics)
    run("央行·新闻发布", fetch_pbc_news)

    for keyword in cfg.get("finance_keywords", []):
        run("东方财富标题·" + keyword, fetch_eastmoney_title_search, keyword, 6)
        run("华尔街见闻·" + keyword, fetch_wallstreetcn_search, keyword, 5)

    for keyword in cfg.get("tracked_companies", []):
        run("东方财富·" + keyword, fetch_eastmoney_search, keyword, 6)
        run("华尔街见闻·" + keyword, fetch_wallstreetcn_search, keyword, 6)

    for keyword in cfg.get("macro_keywords", []):
        run("东方财富·" + keyword, fetch_eastmoney_search, keyword, 5)

    # 固定关注的号（图表/数据型），每轮都查
    for idx, query in enumerate(cfg.get("wechat_pinned", [])):
        try:
            if idx:
                time.sleep(3)
            got = fetch_wechat_search(query, 5, max_age_days=20)
            stats["微信·" + query] = len(got)
            items.extend(got)
        except Exception as exc:
            errors.append("微信·%s：%s" % (query, exc))

    accounts = cfg.get("wechat_accounts") or []
    if accounts:
        try:
            wx_items, wx_errors, picked = fetch_wechat_accounts(
                accounts, int(cfg.get("wechat_query_slots", 3)))
            stats["微信公众号"] = len(wx_items)
            errors.extend(wx_errors)
        except Exception as exc:
            errors.append("微信公众号：%s" % exc)
            stats["微信公众号"] = 0
    else:
        for idx, query in enumerate(cfg.get("wechat_queries", [])):
            try:
                if idx:
                    time.sleep(2.5)
                got = fetch_wechat_search(query, 5)
                stats["微信·" + query] = len(got)
                items.extend(got)
            except Exception as exc:
                errors.append("微信·%s：%s" % (query, exc))
                stats["微信·" + query] = 0

    try:
        videos, video_errors = fetch_bilibili_videos(
            cfg.get("video_keywords", []),
            total=int(cfg.get("video_total", 20)),
            max_per_author=1)
        stats["B站视频"] = len(videos)
        items.extend(videos)
        errors.extend(video_errors)
    except Exception as exc:
        errors.append("B站视频：%s" % exc)
        stats["B站视频"] = 0

    stats["耗时秒"] = round(time.time() - started, 1)
    return items, errors, stats
