# 财经日报 · 本地服务

打开网址就能看，不用再下载 HTML。后台每小时自动抓一轮新内容。

- **网址**：<http://127.0.0.1:8775>
- **桌面图标**：桌面上的「财经日报」，双击用 Chrome 应用窗口打开
- **服务名**：`finance-daily`（systemd 用户服务，已设开机自启并开启 linger）

## 它每小时做什么

后台线程按 `config.json` 里的 `refresh_interval_seconds`（默认 3600 秒）跑一轮采集：

| 来源 | 内容 |
| --- | --- |
| 华尔街见闻 7x24 | 市场快讯 |
| 同花顺财经 7x24 | 市场快讯 |
| 东方财富资讯搜索 | 跟踪公司、宏观关键词相关报道 |
| 华尔街见闻文章搜索 | 跟踪公司的深度文章 |
| 国家统计局「数据发布」 | 官方宏观稿件 |
| B 站视频搜索 | 财经视频，可直接在页面内播放 |

每轮抓取会自动去重（链接 + 标题指纹），按分类归档，只保留最近 10 天、最多 500 条自动内容；
原始整理稿（14 条）和手动添加的文章一直保留。

## 页面怎么用

- **精选**：默认视图，隐藏零碎的「市场快讯」，只看宏观、财报、公司动态和视频
- 顶部筛选可切到全部/单一分类；搜索框按标题、摘要、来源过滤
- 「只看已整理」只显示人工整理过、可核实的条目
- 「立即更新」立刻跑一轮，约 20 秒完成；页面每 30 秒检查一次版本号，有新数据会自动刷出来
- 「＋ 添加文章」写进 `data/manual.json`，不会丢
- 「导出 JSON」下载当前全部内容
- 右上角显示上次/下次更新时间和倒计时

## 管理命令

```bash
cd /home/ubuntu/.wegent/workspace/916580380855103/finance-daily

./start.sh                      # 启动
./stop.sh                       # 停止
systemctl --user status finance-daily     # 看状态
systemctl --user restart finance-daily    # 改完配置后重启
tail -f logs/server.log         # 看抓取日志
```

## 改配置

编辑 `config.json` 后 `systemctl --user restart finance-daily`：

- `refresh_interval_seconds`：更新间隔，默认 3600（改成 1800 就是半小时一轮）
- `tracked_companies`：要跟踪的公司名，会拿去搜资讯
- `macro_keywords`：宏观关键词
- `video_keywords`：B 站视频搜索词
- `retention_days` / `max_auto_items`：自动内容的保留天数和条数上限
- `port`：端口（改了要同步改 systemd 单元里的启动端口环境变量，或用 `FINANCE_PORT`）

## 文件结构

```
finance-daily/
├── server.py          # HTTP 服务 + 每小时调度 + 去重合并（仅标准库）
├── fetchers.py        # 各数据源抓取与清洗
├── config.json        # 关键词、间隔、端口
├── web/index.html     # 前端页面
├── data/
│   ├── seed.json      # 原来那 14 条整理稿
│   ├── auto.json      # 自动抓取内容（每轮覆盖更新）
│   ├── manual.json    # 手动添加
│   └── meta.json      # 更新状态/版本号
└── logs/server.log
```

## 说明

- 自动抓取的条目标注「待核验」，标题和摘要来自原媒体，未做事实核验；
  指标数字由程序从正文抽取，单季/累计、同比/环比的口径以原文为准。
- 媒体转述不等于公司公告；页面内容不构成投资建议。
- 所有接口都是公开页面/公开 API，抓取失败时会在页面顶部提示是哪一路源，
  其余内容照常更新，不会整页挂掉。
