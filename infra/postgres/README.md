# 自管 Postgres Machine(ADR-013 Tier B)

獨立於 App 的 Fly app(`shopfloor-cmms-db`;原 `cmms-db` 全球被占,2026-06-29 改名),只走私網(`*.internal`),持久 volume。

## 部署步驟(初次)

```bash
# 1. 建 volume(新加坡,3GB 起步;用量 <100MB,足夠)
flyctl volumes create pgdata -c infra/postgres/fly.pg.toml -r sin -s 3

# 2. 設密碼(值不入 git,見 ../secrets-manifest.md)
flyctl secrets set -c infra/postgres/fly.pg.toml POSTGRES_PASSWORD=<強密碼>

# 3. 部署
flyctl deploy -c infra/postgres/fly.pg.toml
```

## App 端連線

App 的 `CMMS_DATABASE_URL` secret 指向私網位址:

```
postgresql+asyncpg://cmms:<password>@shopfloor-cmms-db.internal:5432/cmms
```

## 升級到 Tier C(Managed PG)— 可逆

`pg_dump` → `pg_restore` 進 Fly Managed PG、改 `CMMS_DATABASE_URL`、刪本 app。
應用程式碼零變更(都是標準 Postgres URL)。判準見 ADR-013。

## Backup

見 `../backup/`:`pg_dump | gzip` → Cloudflare R2,每日 cron;每月驗一次 restore。
**沒驗過的 backup 等於沒 backup。**
