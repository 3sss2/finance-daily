# 财经日报 · 每小时自动更新

**公开网址（任何电脑都能打开）**：<https://3sss2.github.io/finance-daily/>

本地网址（只在这台设备上可访问）：<http://127.0.0.1:8775>

同一份页面、同一套数据，两种形态：

| | 公开站（GitHub Pages） | 本地服务（8775） |
| --- | --- | --- |
| 谁在抓数据 | GitHub Actions 每小时跑一次 | 这台设备上的 systemd 服务 |
| 访问范围 | 任何人、任何电脑 | 仅本机（桌面图标「财经日报」） |
| 能否手动添加文章 | 不能（静态站） | 可以 |
| 更新频率 | 每小时第 7 分钟 | 每小时一轮 |

## 公开站怎么更新

`.github/workflows/update.yml` 每小时（UTC 第 7 分钟）在 GitHub 的机器上：

1. 还原上一轮的数据缓存（用于去重、保留最近 10 天）
2. 跑 `build_static.py`，抓取并生成 `public/index.html` + `public/data.json`
3. 打包部署到 GitHub Pages

抓取失败时不会用空内容覆盖线上版本，页面上会提示「本轮数据源不可达，沿用上次内容」。

也可以在仓库的 Actions 页手动点 **Run workflow** 立刻更新一次。

如果 GitHub 的定时任务因仓库 60 天无活动被自动停用，去 Actions 页重新启用即可；
本地服务不受影响，可以随时回来手动跑一次。

## 数据来源

| 来源 | 内容 |
| --- | --- |
| 华尔街见闻 7x24 | 市场快讯 |
| 同花顺财经 7x24 | 市场快讯 |
| 东方财富资讯搜索 | 跟踪公司、宏观关键词相关报道 |
| 华尔街见闻文章搜索 | 跟踪公司的深度文章 |
| 国家统计局「数据发布」 | 官方宏观稿件 |
| B 站视频搜索 | 财经视频，可在页面内播放 |

每轮自动去重（链接 + 标题指纹），按分类归档，只保留最近 10 天、最多 500 条自动内容；
原始整理稿（14 条，见 `seed.json`）和本地手动添加的文章一直保留。

## 页面怎么用

- **精选**：默认视图，隐藏零碎的「市场快讯」，只看宏观、财报、公司动态和视频
- 顶部筛选可切到全部/单一分类；搜索框按标题、摘要、来源过滤
- 「只看已整理」只显示人工整理过、可核实的条目
- 「导出 JSON」下载当前全部内容
- 链接 `?f=视频财经` 可以直接带筛选打开（`精选 / 宏观数据 / 公司财报 / 公司动态 / 视频财经 / 市场快讯`）

## 本地服务管理

```bash
cd /home/ubuntu/.wegent/workspace/916580380855103/finance-daily

./start.sh                      # 启动
./stop.sh                       # 停止
systemctl --user status finance-daily     # 看状态
systemctl --user restart finance-daily    # 改完配置后重启
tail -f logs/server.log         # 看抓取日志
```

## 改配置

编辑 `config.json` 后，本地 `systemctl --user restart finance-daily`；公开站下个整点自动生效。

- `refresh_interval_seconds`：本地更新间隔，默认 3600
- `tracked_companies`：要跟踪的公司名
- `macro_keywords`：宏观关键词
- `video_keywords`：B 站视频搜索词
- `retention_days` / `max_auto_items`：自动内容的保留天数和条数上限
- `port`：本地端口

## 文件结构

```
finance-daily/
├── server.py          # 本地 HTTP 服务 + 每小时调度（仅标准库）
├── build_static.py    # 生成 public/ 静态站（GitHub Actions 调用）
├── fetchers.py        # 各数据源抓取与清洗
├── seed.json          # 原始 14 条人工整理稿
├── config.json        # 关键词、间隔、端口
├── web/index.html     # 前端页面（自动识别本地服务或静态数据源）
├── public/            # 静态站产物：index.html + data.json
├── .github/workflows/update.yml   # 每小时抓取并部署
├── data/              # 本地运行状态（不进仓库）
└── logs/server.log
```

## 说明

- 自动抓取的条目标注「待核验」，标题和摘要来自原媒体，未做事实核验；
  指标数字由程序从正文抽取，单季/累计、同比/环比的口径以原文为准。
- 媒体转述不等于公司公告；页面内容不构成投资建议。
- 抓取全部走公开页面/公开接口；某一路失败会在页面顶部提示是哪一路，其余照常更新。
