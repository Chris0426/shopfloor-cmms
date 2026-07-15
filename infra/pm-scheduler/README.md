# PM 自動排程器 — 每日 Fly scheduled machine(ADR-021)

> AI 寫這份 IaC/runbook,**不執行**(ADR-009 / 護欄 #6)。operator 步驟見 本目錄 README。

## 設計:重用主 app 映像,不另立 app

排程器要跑的是 `cmms pm-generate-due`(ADR-021 unattended 批次生成到期 PM 工單;idempotent、單筆失敗隔離、`source_actor=scheduler`)。這支 CLI **早已在 `shopfloor-cmms` 主映像內**(2026-06-28 併入,早於 0013 部署)。

```
每日喚醒 ──► cmms pm-generate-due(直連 shopfloor-cmms-db.internal 私網,不跨海)
         ──► 對「到期 × 週期性 × 未 suppress」PM 各開一張 PM 工單(冪等:同週期未結案不重生)
         ──► (選用) PM_SCHEDULER_HEALTHCHECK_URL 心跳 ──► 沒 ping 到 → healthchecks 告警
         ──► machine 退出;Fly 依 --schedule daily 週期重啟
```

**為何不像 backup 另立 app**:`shopfloor-cmms-backup` 之所以獨立,是因為它需要 `pg_dump` 二進位(`postgres:17` 映像)。PM 排程器需要的是 **cmms Python 套件本身**,那**就是主映像**——所以最省的做法是**在 `shopfloor-cmms` app 上多跑一台 scheduled machine**,`--command` override 成 `/opt/venv/bin/cmms pm-generate-due`,重用主映像 + 同一顆 `CMMS_DATABASE_URL` secret。**不需新 app / 新 Dockerfile / 新 secret / redeploy。**

## 檔案

| 檔 | 作用 |
|---|---|
| `README.md` | 本檔:設計 + 為何重用主映像 |
| (無 Dockerfile) | 重用 `shopfloor-cmms` 主映像(不像 backup 需另建) |
| (無 fly.toml) | 不建新 app;scheduled machine 掛在既有 `shopfloor-cmms` 上 |

> **替代設計(隔離獨立 app,非預設)**:若日後要把排程器和 web app 生命週期解耦,可另立 `shopfloor-cmms-pm-scheduler` app + `fly.pm-scheduler.toml` 重用 repo 根 `Dockerfile`(build context 為 repo 根)+ 自己的 `CMMS_DATABASE_URL`。目前**不需要**——多一台 machine 成本近零、且共用映像保證排程器與 app 程式永遠同版。

## 部署 / 排程 / 驗證

完整 operator 步驟見 **本目錄 README**。一句話流程:
`flyctl image show`(取映像 ref)→ `flyctl machine run --schedule daily --command "/opt/venv/bin/cmms pm-generate-due"`(建排程 machine)→ `flyctl machine start`(立即跑一次驗證)→ `flyctl logs` 看 `due N: X generated…`。

## 心跳(選用,dead-man's-switch)

`cmms pm-generate-due --healthcheck-url <url>`(或環境變數 `PM_SCHEDULER_HEALTHCHECK_URL`)在成功跑完後 ping 一次(stdlib urllib,無新依賴);ping 失敗被吞、**絕不**讓排程本身算失敗。healthchecks.io check 設 period 1 day / grace 數小時:排程今天沒跑到 → 收告警。設定方式同 backup 的 `HEALTHCHECK_URL`。
