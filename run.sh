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
MCP_AN_NINH_PORT="${MCP_AN_NINH_PORT:-8102}"
MCP_VE_SINH_PORT="${MCP_VE_SINH_PORT:-8103}"
APP_HOST="${APP_HOST:-127.0.0.1}"
APP_PORT="${APP_PORT:-8000}"

# Mỗi bên tham gia là một MCP server riêng: "khóa|cổng|tệp". Thêm nhà thầu mới
# (thang máy, cây xanh...) = thêm một dòng ở đây + vài dòng trong catalog.yaml.
MCP_SERVERS=(
  "ky_thuat|$MCP_PORT|mcp_servers/ky_thuat_server.py"
  "an_ninh|$MCP_AN_NINH_PORT|mcp_servers/an_ninh_server.py"
  "ve_sinh|$MCP_VE_SINH_PORT|mcp_servers/ve_sinh_server.py"
)

MCP_PIDS=()
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
  for pid in "$API_PID" ${MCP_PIDS+"${MCP_PIDS[@]}"}; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
  done
  for pid in "$API_PID" ${MCP_PIDS+"${MCP_PIDS[@]}"}; do
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

STARTED=0
for entry in "${MCP_SERVERS[@]}"; do
  IFS="|" read -r name port file <<<"$entry"
  if port_busy "$port"; then
    echo "[1/2] MCP '$name' đã chạy sẵn ở cổng $port — dùng lại."
  else
    echo "[1/2] MCP '$name' (cổng $port)..."
    MCP_PORT="$port" MCP_AN_NINH_PORT="$port" MCP_VE_SINH_PORT="$port" "$PY" "$file" &
    MCP_PIDS+=($!)
    STARTED=1
  fi
done
[ "$STARTED" = "1" ] && sleep 2

echo "[2/2] Backend FastAPI — mở http://$APP_HOST:$APP_PORT"
"$PY" -m uvicorn backend.app.main:app --host "$APP_HOST" --port "$APP_PORT" &
API_PID=$!

# Kết thúc ngay khi một trong hai tiến trình chết, thay vì treo mãi.
wait -n "$API_PID" ${MCP_PIDS+"${MCP_PIDS[@]}"}
