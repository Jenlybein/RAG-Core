#!/usr/bin/env bash
# 本地起 API 服务：数据面**认真实 API Key**（而不是内置的 dev-key）。
#
# 为什么把这两个开关固定在脚本里：它们平时是命令行前缀传的
# （`AUTH_ENABLED=true RATE_LIMIT_ENABLED=true uv run uvicorn ...`），一旦哪次忘了，
# 服务会**静默回落**到 .env 里的 `AUTH_ENABLED=false`——彼时数据面只认那把固定的
# `DEV_API_KEY`，调试台里选中的每一把真实 Key 都会被判 20010「无效或已停用」，
# 而界面上看起来像是 Key 本身坏了（这个坑真踩过一次）。
#
# 用法：
#   ./scripts/dev-api.sh              # 8000 端口 + --reload
#   PORT=9000 ./scripts/dev-api.sh    # 换端口
#   NO_RELOAD=1 ./scripts/dev-api.sh  # 关掉 --reload
set -euo pipefail

cd "$(dirname "$0")/.."

export AUTH_ENABLED=true
export RATE_LIMIT_ENABLED=true

PORT="${PORT:-8000}"
RELOAD_ARGS=()
if [[ -z "${NO_RELOAD:-}" ]]; then
  RELOAD_ARGS=(--reload)
fi

echo "AUTH_ENABLED=${AUTH_ENABLED}  RATE_LIMIT_ENABLED=${RATE_LIMIT_ENABLED}  port=${PORT}"
exec uv run uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" "${RELOAD_ARGS[@]}"
