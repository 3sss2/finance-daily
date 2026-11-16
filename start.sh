#!/usr/bin/env bash
# 启动财经日报本地服务（优先用 systemd，未安装时退回后台进程）
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
if systemctl --user list-unit-files 2>/dev/null | grep -q '^finance-daily.service'; then
  systemctl --user start finance-daily.service
  systemctl --user --no-pager status finance-daily.service | head -4
else
  cd "$DIR"
  mkdir -p logs
  setsid nohup python3 -I server.py >> logs/server.log 2>&1 &
  echo $! > logs/server.pid
  echo "已启动：http://127.0.0.1:8775"
fi
