#!/bin/bash
# ==============================================================================
# Stop Web Server Service
# ==============================================================================

echo "🛑 正在终止 Codex Referral 服务进程..."
PID=$(pgrep -f "web_server.py" || true)

if [ -n "$PID" ]; then
    kill -TERM $PID 2>/dev/null || true
    sleep 1
    kill -9 $PID 2>/dev/null || true
    echo "✅ 服务已停止 (PID: $PID)"
else
    echo "ℹ️ 未发现正在运行的 web_server 进程"
fi
