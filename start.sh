#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-8088}"
HOST="${2:-0.0.0.0}"
PID_FILE="$DIR/dashboard.pid"
LOG_FILE="$DIR/dashboard.log"

if [[ -f "$PID_FILE" ]]; then
  existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${existing_pid}" ]] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "Dashboard already running (PID $existing_pid)"
    ip="$(hostname -I | awk '{print $1}')"
    echo "URL: http://${ip}:${PORT}"
    exit 0
  fi
fi

nohup python3 "$DIR/server.py" --host "$HOST" --port "$PORT" >"$LOG_FILE" 2>&1 &
pid="$!"
echo "$pid" >"$PID_FILE"
sleep 0.3

if ! kill -0 "$pid" 2>/dev/null; then
  echo "Dashboard failed to start. Check $LOG_FILE"
  exit 1
fi

ip="$(hostname -I | awk '{print $1}')"
echo "Dashboard started (PID $pid)"
echo "URL: http://${ip}:${PORT}"
echo "Log: $LOG_FILE"
