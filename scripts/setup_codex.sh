#!/bin/bash
# ==============================================================================
# Setup Official Codex Linux Binary Engine (Headless app-server)
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET_BIN="$ROOT_DIR/codex"

CODEX_VERSION="rust-v0.160.0"
DOWNLOAD_URL="https://github.com/openai/codex/releases/download/${CODEX_VERSION}/codex-x86_64-unknown-linux-musl.tar.gz"

echo "================================================================="
echo "📥 正在配置官方 Codex Linux 独立运行时引擎 (${CODEX_VERSION})..."
echo "================================================================="

if [ -f "$TARGET_BIN" ] && [ -x "$TARGET_BIN" ]; then
    echo "✅ 官方 Codex 二进制已存在且具备执行权限: $TARGET_BIN"
    exit 0
fi

echo "正在从官方发布源拉取运行时包..."
echo "下载源: $DOWNLOAD_URL"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

curl -fsSL "$DOWNLOAD_URL" | tar -xz -C "$TMP_DIR"

if [ -f "$TMP_DIR/codex-x86_64-unknown-linux-musl" ]; then
    mv "$TMP_DIR/codex-x86_64-unknown-linux-musl" "$TARGET_BIN"
elif [ -f "$TMP_DIR/codex" ]; then
    mv "$TMP_DIR/codex" "$TARGET_BIN"
else
    FOUND=$(find "$TMP_DIR" -type f -name "*codex*" | head -n 1)
    if [ -n "$FOUND" ]; then
        mv "$FOUND" "$TARGET_BIN"
    else
        echo "❌ 未在压缩包中识别到 codex 可执行文件！"
        exit 1
    fi
fi

chmod +x "$TARGET_BIN"
echo "🎉 官方 Codex 原生引擎配置就绪: $TARGET_BIN"
"$TARGET_BIN" --version || true
