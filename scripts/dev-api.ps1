# 本地起 API 服务：数据面**认真实 API Key**（而不是内置的 dev-key）。
#
# 为什么把这两个开关固定在脚本里：它们平时是命令行前缀传的
# （`AUTH_ENABLED=true RATE_LIMIT_ENABLED=true uv run uvicorn ...`），一旦哪次忘了，
# 服务会**静默回落**到 .env 里的 `AUTH_ENABLED=false`——彼时数据面只认那把固定的
# `DEV_API_KEY`，调试台里选中的每一把真实 Key 都会被判 20010「无效或已停用」，
# 而界面上看起来像是 Key 本身坏了（这个坑真踩过一次）。
#
# 参数：
#   -Port     端口，默认 8000
#   -NoReload 关掉 --reload（跑压测或调 Celery 时用）
param(
    [int]$Port = 8000,
    [switch]$NoReload
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

$env:AUTH_ENABLED = 'true'
$env:RATE_LIMIT_ENABLED = 'true'

$reloadArgs = if ($NoReload) { @() } else { @('--reload') }

Write-Host "AUTH_ENABLED=$env:AUTH_ENABLED  RATE_LIMIT_ENABLED=$env:RATE_LIMIT_ENABLED  port=$Port" -ForegroundColor Green
uv run uvicorn app.main:app --host 127.0.0.1 --port $Port @reloadArgs
