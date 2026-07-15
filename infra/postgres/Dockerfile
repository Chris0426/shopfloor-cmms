# 自管 Postgres Machine(ADR-013 Tier B)。獨立於 App Machine,持久 volume,
# 僅暴露於 Fly 私網(*.internal)。用官方映像,不加業務邏輯。
#
# 升級到 Tier C(Fly Managed PG)時:pg_dump -> pg_restore 進 Managed、改
# CMMS_DATABASE_URL、刪本 Machine 即可,應用程式碼零變更(都是標準 Postgres URL)。

FROM postgres:17-bookworm

# 時區與 locale 可在此設定;資料目錄掛 Fly volume(見 fly.pg.toml 的 [mounts])
ENV PGDATA=/var/lib/postgresql/data/pgdata
