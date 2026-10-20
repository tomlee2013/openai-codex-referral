#!/bin/bash
# ==============================================================================
# Start Web Server & API Service
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$ROOT_DIR"

PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"
PYTHON_BIN="python3"

if [ -f "$ROOT_DIR/venv/bin/python3" ]; then
    PYTHON_BIN="$ROOT_DIR/venv/bin/python3"
fi

# 检查是否已安装官方 codex 引擎
if [ ! -f "$ROOT_DIR/codex" ]; then
    echo "ℹ️ 未检测到 codex 二进制，尝试自动拉取..."
    bash "$ROOT_DIR/scripts/setup_codex.sh" || echo "⚠️ 自动安装 codex 失败，激活时将回退至备用上报模式。"
fi

mkdir -p "$ROOT_DIR/logs"

echo "🚀 启动 Codex Referral 系统 (Host: $HOST, Port: $PORT)..."
nohup "$PYTHON_BIN" web_server.py --host "$HOST" --port "$PORT" > "$ROOT_DIR/logs/web_server.log" 2>&1 &
SERVER_PID=$!

echo "✅ 服务已在后台启动！PID: $SERVER_PID"
echo "📄 实时日志输出: tail -f $ROOT_DIR/logs/web_server.log"
