# Secrets manifest

**Keys only. Values never enter git** (ADR-013). This file exists so that "what secrets does this
system need, and what happens if one is missing" is answerable without reading the code.

Every setting is read by `src/cmms/config.py` (pydantic-settings, prefix `CMMS_`). In production
they are set once with `flyctl secrets set` and never edited in a dashboard.

## The rule that matters

**Every secret fails closed.** A missing secret never degrades into an insecure default — it
degrades into a refusal, or into an honestly-labelled in-memory stub for local development. There
is no code path where a missing key silently turns a protected surface into an open one.

## App (`shopfloor-cmms`)

| Key | Purpose | Missing ⇒ |
|---|---|---|
| `CMMS_DATABASE_URL` | PostgreSQL DSN (asyncpg), private network | app will not start |
| `CMMS_READ_API_TOKEN` | Static bearer for the service-to-service JSON read API | production: protected reads return **503**; local/CI: open |
| `CMMS_CREDENTIAL_MASTER_KEY` | Fernet key for the per-user credential vault (envelope encryption; the master key is never stored in the DB) | vault refuses to store or retrieve. No plaintext fallback |
| `CMMS_R2_ENDPOINT` / `_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY` | S3-compatible object storage for photos | falls back to an in-memory backend (local/CI) |
| `CMMS_SMTP_HOST` / `_PORT` / `_USERNAME` / `_PASSWORD` / `CMMS_NOTIFY_FROM` / `CMMS_RFQ_FROM` / `CMMS_RFQ_REPLY_TO` | Outbound email (RFQs, work-order notifications) | notifications queue as `pending` and are never sent. They are not dropped |
| `CMMS_TELEGRAM_BOT_TOKEN` / `CMMS_TELEGRAM_BOT_USERNAME` | Notification + assistant bot | Telegram channel reports itself unconfigured |
| `CMMS_TELEGRAM_WEBHOOK_SECRET` | Shared secret in the webhook header | webhook returns **503** and accepts nothing |
| `HERMES_GATEWAY_SECRET` | Shared secret between the app and the agent gateway (also set on the gateway app) | assistant reports itself unconfigured |
| `CMMS_JIRA_BASE_URL` / `CMMS_JIRA_PROJECT_KEY` | Jira instance and the *only* project agents may write to | Jira forwarding disabled |
| `CMMS_ONBOX_JWKS_JSON` | Public keys (RFC 7517 JWKS) of the edge tooling permitted to submit signed writes (ADR-017) | on-box write API returns **503** |

Non-secret runtime config (`CMMS_APP_ENV`, `CMMS_HERMES_GATEWAY_URL`, TTLs) lives in `fly.toml`
under `[env]`, in git, on purpose.

## Agent gateway (`shopfloor-cmms-hermes`)

| Key | Purpose |
|---|---|
| `HERMES_GATEWAY_SECRET` | Same value as on the app. The gateway is private-network-only; this is defence in depth, not the only control |
| model provider credentials | Persisted on a volume, not in the image. See `infra/hermes/README.md` |

## Backup (`shopfloor-cmms-backup`)

| Key | Purpose |
|---|---|
| `PGHOST` / `PGUSER` / `PGPASSWORD` / `PGDATABASE` | Source database for `pg_dump` |
| `S3_ENDPOINT` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `S3_BUCKET` | Offsite object storage for the dumps |
| `BACKUP_MIN_BYTES` | Integrity guard — a dump smaller than this is treated as a failed backup, not uploaded |
| `HEALTHCHECK_URL` | Dead-man's-switch ping. If the nightly job stops running, this is what notices |

## Rotation

Rotating any key is `flyctl secrets set` + a rolling restart. Two have extra consequences worth
stating out loud:

- **`CMMS_CREDENTIAL_MASTER_KEY`** — rotating it without re-encrypting invalidates every stored
  user credential. Users are prompted to re-enter their PAT. This is the correct failure mode, and
  it is why the vault surfaces a distinct "key invalid" error rather than a generic decrypt failure.
- **`CMMS_READ_API_TOKEN`** — downstream consumers must be handed the new value. It is a static
  bearer precisely so that rotation is a deliberate, coordinated act.
