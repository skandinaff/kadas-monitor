#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/dashboard.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "Dashboard is not running (no PID file)."
  exit 0
fi

pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "$pid" ]]; then
  rm -f "$PID_FILE"
  echo "Removed empty PID file."
  exit 0
fi

if kill -0 "$pid" 2>/dev/null; then
  kill "$pid"
  for _ in {1..20}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 0.1
  done
fi

if kill -0 "$pid" 2>/dev/null; then
  echo "PID $pid did not stop yet; try again in a moment."
  exit 1
fi

rm -f "$PID_FILE"
echo "Dashboard stopped."
