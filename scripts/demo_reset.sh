#!/usr/bin/env bash
# Reset the public demo site to a clean, freshly seeded state.
#
#   1. docker compose down --remove-orphans      then `docker volume rm <project>_pgdata`
#                                                (wipes ONLY the database volume; the gateway's
#                                                budget/quota ledger `hermes_data` must survive
#                                                resets or the daily/monthly caps mean nothing)
#   1b. docker compose build                      (no-op when nothing changed; picks up a git pull)
#   2. docker compose up -d --wait <every service except seed>
#                                                (db -> migrations -> app [-> edge], wait for healthy)
#   3. docker compose run --rm --no-deps seed    (one-shot seed; its exit code is ours)
#
# Everything a visitor changed is gone afterwards: the seed service regenerates the
# SYNTHETIC data and the demo accounts from scratch. The site is unavailable for roughly
# 1-2 minutes while the stack comes back up.
#
# Meant to run from a host-side timer (infra/demo/cmms-demo-reset.timer, or cron). The
# timer interval must match CMMS_DEMO_RESET_MINUTES, which is what the banner tells
# visitors (default: 30).
#
# Works with or without the public override (docker-compose.public.yml, which adds `edge`):
# compose itself reads COMPOSE_FILE from the repo's .env, so no script flag is needed.
#
# Env:
#   COMPOSE_BIN   compose command to use  (default: "docker compose")
#   DOCKER_BIN    docker CLI for `volume rm`  (default: "docker")
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 刻意不加引號展開,讓 "docker compose" 這種兩段式指令可用。
COMPOSE="${COMPOSE_BIN:-docker compose}"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] demo-reset: $*"; }

log "resetting demo stack in $ROOT"
# 不用 `down -v`:那會連 hermes_data(gateway 的預算/配額帳本)一起清掉,每 30 分鐘歸零
# 等於沒有預算上限。只刪資料庫 volume;專案名從 compose config 第一行 `name:` 取。
# shellcheck disable=SC2086
$COMPOSE down --remove-orphans
# shellcheck disable=SC2086
PROJECT="$($COMPOSE config | sed -n 's/^name: //p' | head -n1)"
[ -n "$PROJECT" ] || { log "cannot determine compose project name"; exit 1; }
${DOCKER_BIN:-docker} volume rm -f "${PROJECT}_pgdata" >/dev/null
# `up` 不會因原始碼變了就重建映像;明確 build 一次,layer cache 命中時只花幾秒,
# 但 git pull 後的程式碼與依賴變更一定會進到下一輪(2026-10-25 首次部署實測教訓)。
# shellcheck disable=SC2086
$COMPOSE build
# 長駐服務先起並等 healthy;seed 是一次性容器,分開跑才拿得到它的 exit code
# (`up -d --wait` 把「跑完就退出」的 seed 容器視為失敗,見 compose v2 行為)。
# 服務清單從 compose config 推導,有沒有疊 public override(多一個 edge)都適用。
# 用命令替換而非 process substitution:config 失敗時 set -e + pipefail 會直接中止;
# 清單為空時也中止,否則 `up` 不帶服務名會連 seed 一起起。
# shellcheck disable=SC2086
SERVICES="$($COMPOSE config --services | grep -vx seed)"
[ -n "$SERVICES" ] || { log "no services found in compose config"; exit 1; }
# 服務名不含空白,刻意不加引號展開成多個參數。
# shellcheck disable=SC2086
$COMPOSE up -d --wait $SERVICES
# shellcheck disable=SC2086
$COMPOSE run --rm --no-deps seed
log "done"
