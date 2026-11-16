#!/usr/bin/env bash
# 兜底触发器：GitHub 自带的 cron 经常延迟（新仓库尤其明显），
# 这里每 5 分钟检查一次，若最近 6 分钟没有跑过就手动触发一次。
set -uo pipefail
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
REPO="3sss2/finance-daily"
DIR="/home/ubuntu/.wegent/workspace/916580380855103/finance-daily"
LOG="$DIR/logs/nudge.log"
mkdir -p "$DIR/logs"

last="$(gh run list --repo "$REPO" --limit 1 --json createdAt -q '.[0].createdAt' 2>/dev/null)"
if [ -n "$last" ]; then
  last_ts="$(date -d "$last" +%s 2>/dev/null || echo 0)"
  now_ts="$(date +%s)"
  if [ $((now_ts - last_ts)) -lt 360 ]; then
    exit 0
  fi
fi

if gh workflow run update.yml --repo "$REPO" -f mode=live >/dev/null 2>&1; then
  echo "$(date '+%F %T') 触发更新（上一轮 ${last:-未知}）" >> "$LOG"
else
  echo "$(date '+%F %T') 触发失败" >> "$LOG"
fi
