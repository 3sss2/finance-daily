#!/usr/bin/env bash
# 停止财经日报本地服务
DIR="$(cd "$(dirname "$0")" && pwd)"
if systemctl --user list-unit-files 2>/dev/null | grep -q '^finance-daily.service'; then
  systemctl --user stop finance-daily.service
  echo "已停止"
else
  [ -f "$DIR/logs/server.pid" ] && kill "$(cat "$DIR/logs/server.pid")" 2>/dev/null
  echo "已停止"
fi
