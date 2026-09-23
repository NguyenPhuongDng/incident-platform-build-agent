#!/usr/bin/env bash
# Khởi động MCP server + backend. Dừng bằng Ctrl+C.
set -uo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

if [ ! -f .env ]; then
  echo "Chưa có .env — đã copy từ .env.example, nhớ điền QWEN_API_KEY."
  cp .env.example .env
fi

MCP_PORT="${MCP_PORT:-8101}"
APP_HOST="${APP_HOST:-127.0.0.1}"
APP_PORT="${APP_PORT:-8000}"

MCP_PID=""
API_PID=""
CLEANING=0

# Dọn dẹp phải: (1) không tự gọi lại mình, (2) chỉ giết đúng tiến trình con của
# script này. Dùng `kill 0` sẽ bắn TERM vào cả process group — kể cả chính script —
# khiến trap TERM gọi lại cleanup, đệ quy tới khi bash tràn stack và segfault.
cleanup() {
  [ "$CLEANING" = "1" ] && return
  CLEANING=1
  trap - EXIT INT TERM
  echo
  echo "Đang dừng..."
  for pid in "$API_PID" "$MCP_PID"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
  done
  for pid in "$API_PID" "$MCP_PID"; do
    [ -n "$pid" ] && wait "$pid" 2>/dev/null
  done
  echo "Đã dừng."
}
trap cleanup EXIT INT TERM

port_busy() {
  "$PY" - "$1" <<'PYCHK'
import socket, sys
s = socket.socket()
s.settimeout(0.5)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PYCHK
}

if port_busy "$APP_PORT"; then
  echo "Cổng $APP_PORT đang bị chiếm — có một backend khác đang chạy."
  echo "Dừng nó trước, hoặc chạy lại với: APP_PORT=8002 ./run.sh"
  exit 1
fi

if port_busy "$MCP_PORT"; then
  echo "[1/2] MCP server đã chạy sẵn ở cổng $MCP_PORT — dùng lại."
else
  echo "[1/2] MCP server ky_thuat (cổng $MCP_PORT)..."
  "$PY" mcp_servers/ky_thuat_server.py &
  MCP_PID=$!
  sleep 2
fi

echo "[2/2] Backend FastAPI — mở http://$APP_HOST:$APP_PORT"
"$PY" -m uvicorn backend.app.main:app --host "$APP_HOST" --port "$APP_PORT" &
API_PID=$!

# Kết thúc ngay khi một trong hai tiến trình chết, thay vì treo mãi.
wait -n "$API_PID" ${MCP_PID:+"$MCP_PID"}
