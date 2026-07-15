#!/bin/sh
# Hermes gateway 啟動封裝(以 root 進、以 hermes 出)。
#
# Fly volume(/data)首次掛載為 root 擁有 → 非 root 的 hermes 無法在 CODEX_HOME 寫入
# (codex login 的 auth.json / config.toml / 快取)。故:root 先確保目錄存在並把整個
# /data 的擁有權交給 hermes,再用 gosu 降權跑實際服務(uvicorn = CMD)。
#
# 這也修復 operator 以 root 跑 `codex login` 後 auth.json 屬 root 的情況 —— 下次容器
# 重啟時 entrypoint 會把 /data 一併 chown 回 hermes(見 部署 runbook)。
set -e

CODEX_HOME="${CODEX_HOME:-/data/codex}"
mkdir -p "$CODEX_HOME"
chown -R hermes:hermes /data

exec gosu hermes "$@"
