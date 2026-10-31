# Hermes gateway contract (multi-product)

Audience: teams that call the gateway (cmms web, Board). Source of truth is the code
(`app/gateway.py`, `app/ledger.py`, `app/llm_anthropic.py`); this file mirrors it.
Scope: `HERMES_BACKEND=anthropic`. The codex backend serves `cmms` only (`target=board` -> 503
`mcp target not supported by the codex backend: board`).

## 1. Endpoints and auth

| Endpoint | Purpose |
|---|---|
| `POST /chat` | Chat turn; the gateway acts as MCP client of the selected target. |
| `POST /mrq-description` | cmms only. Zero-tool text generation (Jira MRQ description). Body: `work_orders`, `jira_locale`, `truncated`. Returns `{"description": str}`. Always charged to the `cmms` daily bucket. |
| `GET /health` | Liveness, backend, MCP target config, budget state. No auth. |

Auth: header `X-Hermes-Secret` on `/chat` and `/mrq-description`, compared in constant time
(`secrets.compare_digest`) with `HERMES_GATEWAY_SECRET`. Secret unset on the server -> 503
(fail-closed); missing or wrong header -> 401.

## 2. `POST /chat` request (JSON)

| Field | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `message` | string | yes | - | The user's question. |
| `scoped_token` | string | yes | - | Short-lived per-user token. Sent as `Authorization: Bearer <token>` to the target's MCP. Never logged. |
| `history` | `[{role, content}]` | no | `[]` | Prior turns. `role` is `user` or `assistant` (anything else is rendered as assistant). |
| `locale` | string | no | null | Preferred reply language (e.g. `zh-TW`, `vi`, `en`). Also selects the refusal string. |
| `jira_locale` | string | no | null | cmms only: output language for Jira MRQ content. Ignored for `board`. |
| `client_id` | string | no | `"unknown"` | Per-source limit key (e.g. end-user IP or a stable channel key). HMAC-SHA256 with the gateway secret, 24-hex prefix stored; the raw value is never stored. Omitting it puts all callers in one shared bucket. |
| `tier` | string | no | null | `"deep"` selects the deep model. Messages containing deep-analysis markers also select it. |
| `target` | `"cmms"` \| `"board"` | no | `"cmms"` | Product to serve. Any other value -> 422. |
| `turn_id` | string | no | null | `board` only. Sanitised and forwarded to the board MCP as header `X-Board-Turn`: trimmed, <= 128 chars, ASCII printable (0x20-0x7E) only; otherwise the header is silently omitted. cmms never sends it. |
| `layout` | `"narrow"` \| `"wide"` | no | null | `board` only; cmms ignores it. Any other value -> 422. Rendered as a trailing system line `Reply layout: <value>` after the cached persona. |

The request never carries an MCP URL. URLs come from server env only
(`CMMS_MCP_URL`, `BOARD_MCP_URL`); a caller can choose a `target`, nothing more.

## 3. `POST /chat` response

200: `{"reply": "<text>"}`.

If the model refuses (`stop_reason=refusal`) the reply is a fixed per-locale string, still 200 and
still charged. Example (`en`, also the fallback for unknown locales): `I can't help with that request.`
Locales starting `zh` and `vi` get their own fixed strings.

## 4. Errors

Body is `{"detail": "<string>"}` for all errors below.

| Status | `detail` | When |
|---|---|---|
| 401 | `unauthorized` | Header missing or wrong. |
| 422 | FastAPI validation body | Bad JSON shape, missing `message`/`scoped_token`, `target` not in {cmms, board}. |
| 429 | `daily question limit reached` | Per-source daily question cap hit. Header `Retry-After: <seconds to next UTC midnight>`. |
| 503 | `gateway secret not configured` | Server secret unset. |
| 503 | `mcp target not configured: board` | `BOARD_MCP_URL` unset or empty. |
| 503 | `persona not available: board` | Persona file missing, unreadable, empty or not UTF-8. |
| 503 | `monthly budget exhausted` | Shared monthly pool used up (checked first). |
| 503 | `daily budget exhausted` | This target's daily bucket used up. |
| 503 | `ledger unavailable` | Ledger cannot be read (fail-closed: no accounting, no spend). |
| 503 | `agent backend not configured: <reason>` | `ANTHROPIC_API_KEY` unset, credentials rejected / permission denied (`anthropic credentials rejected`), or model/endpoint not found. |
| 502 | `agent backend error: timeout` | Exceeded `HERMES_LLM_TIMEOUT`. |
| 502 | `agent backend error: tool server unavailable` | MCP connection/protocol failure (e.g. target MCP unreachable). |
| 502 | `agent backend error: <reason>` | Anthropic failure: `anthropic rate limited` (adds `Retry-After` if upstream sent one), `anthropic rejected the request`, `anthropic error <status>`, `anthropic timeout`, `anthropic unreachable`, `tool loop limit reached` (more than 8 tool rounds), `anthropic produced an empty reply`, `anthropic produced no message`. |

Gate order in `/chat` (anthropic backend):

1. Secret check (401 / 503).
2. Target MCP URL configured (503 `mcp target not configured`).
3. Board only: persona loads (503 `persona not available`).
4. Monthly + this target's daily budget (503; 503 `ledger unavailable`).
5. Per-source question cap, check only (429).
6. Anthropic client built (503 `agent backend not configured`), then the LLM call (502 / 503 above).

A question is counted against the per-source cap only after the LLM returned an answer or a
refusal. Rejections in steps 1-5 and failures in step 6 (502 / 503 / timeout) do not count, so a
visitor retrying through an outage keeps their daily questions. Failed LLM calls that already
consumed tokens are still recorded in the ledger as `error` and still count against the budget.

## 5. `GET /health`

```json
{
  "status": "ok",
  "backend": "anthropic",
  "mcp_targets": {
    "cmms":  {"configured": true,  "url": "http://app:8080/mcp"},
    "board": {"configured": true,  "url": "http://host.docker.internal:18050/mcp"}
  },
  "budget": {
    "monthly": {"spent_usd": 1.2, "limit_usd": 10.0, "state": "ok"},
    "daily": {
      "cmms":  {"spent_usd": 0.41, "limit_usd": 0.5, "state": "warning"},
      "board": {"spent_usd": 0.0,  "limit_usd": 0.5, "state": "ok"}
    }
  },
  "questions": {
    "day": "2026-10-31",
    "total": 7,
    "by_target": {
      "cmms":  {"questions": 7, "clients": 3},
      "board": {"questions": 0, "clients": 0}
    }
  }
}
```

- `mcp_targets.*.configured` is true when the env var is set and non-empty; no network probe.
  `configured`/`url` for cmms reflect `CMMS_MCP_URL` only (the code default
  `https://cmms.example.com/mcp` is used for calls but is not shown here when the env is unset).
- `state` is `ok`, `warning` (spent >= 80% of limit) or `exhausted` (spent >= limit, or limit <= 0).
- `budget` and `questions` appear only when `backend` is `anthropic`; if the ledger cannot be
  opened each is the string `"unavailable"`.
- `questions` is today's (UTC) per-source question count summed per target, plus the number of
  distinct sources; it never exposes a source key or hash.
- `url` is exposed unauthenticated: keep `/health` on the private network.

## 6. Limits and budgets

| Env | Default | Meaning |
|---|---|---|
| `HERMES_MONTHLY_BUDGET_USD` | `10.0` | Monthly (UTC) pool, shared by all targets. |
| `HERMES_DAILY_BUDGET_USD` | `0.5` | Daily (UTC) cap applied to each target separately. |
| `HERMES_DAILY_BUDGET_USD_CMMS` / `_BOARD` | unset | Per-target override of the above. |
| `HERMES_DAILY_QUESTIONS_PER_CLIENT` | `10` | cmms: questions per source per UTC day. |
| `HERMES_DAILY_QUESTIONS_PER_CLIENT_BOARD` | `3` | board: same, separate counter. `0` = unlimited. |
| `HERMES_LLM_TIMEOUT` | `120` | Whole-request timeout in seconds (-> 502). |
| `HERMES_MAX_CONCURRENCY` | `2` | Concurrent LLM requests (extra requests wait). |
| `BOARD_MCP_URL` | unset | Board MCP endpoint. No default; unset -> 503. |
| `HERMES_PERSONA_BOARD_FILE` | `personas/board.md` next to `app/` | Board persona file. |

Question bucket key is `<target>:<hmac(client_id)>`, per UTC day. The count is taken before the
LLM call and not refunded on failure.

## 7. Models

| Use | Env | Default |
|---|---|---|
| Default chat | `HERMES_MODEL_CHAT` | `claude-haiku-5-5` |
| `tier="deep"` | `HERMES_MODEL_DEEP` | `claude-sonnet-5-5` |
| `/mrq-description` | `HERMES_MODEL_TEXT` | `claude-haiku-5-5` |

Prices in USD per million tokens (`PRICES` in `app/ledger.py`, checked 2026-10-29):

| Model | input | output | cache write | cache read |
|---|---|---|---|---|
| `claude-haiku-5-5` | 0.10 | 0.50 | 0.125 | 0.01 |
| `claude-sonnet-5-5` | 2.00 | 10.00 | 2.50 | 0.20 |
| `claude-opus-5-5` | 4.00 | 20.00 | 5.00 | 0.20 |

Haiku prompts above 100K tokens are billed at 0.50 / 2.50 / 0.625 / 0.05. Unlisted models are
estimated at the most expensive listed price.

## 8. Networking for the board target (proposed, pending the Board team's decision)

- The gateway runs in Docker on a bridge network. A container cannot reach ports bound to the
  host's `127.0.0.1`; `host.docker.internal:host-gateway` resolves to the docker0 address
  (`172.17.0.1` on the demo host). An SSH reverse tunnel bound to `127.0.0.1` is therefore
  unreachable from the container.
- Recommended: the Oracle host initiates the tunnel to the Board host with
  `-L 172.17.0.1:18050:127.0.0.1:8050` (client-side bind, needs no sshd `GatewayPorts`) and
  `-R 127.0.0.1:8797:127.0.0.1:8797`. `8797` on the Oracle side is the gateway's host-loopback
  publish port (compose `127.0.0.1:8797:8080`, container port 8080); `8797` on the Board host is
  what the Board client dials; `18050` is the Board MCP as seen from the Oracle host (docker0 bind).
- hermes gets `extra_hosts: ["host.docker.internal:host-gateway"]` and
  `BOARD_MCP_URL=http://host.docker.internal:18050/mcp`
  (commented lines are in `docker-compose.public.yml`).
- Not recommended: `network_mode: host` (hermes would have to bind 0.0.0.0 for the app container
  to reach it).

## 9. Compatibility notes

- cmms clients that omit `target` are unaffected (`target` defaults to `cmms`).
- 2026-10-30: `/health` `budget.daily` changed from a flat object to per-target
  (`budget.daily.cmms`, `budget.daily.board`); `mcp_targets` was added.
- The question-bucket key gained the `cmms:` prefix, so per-source counters restart once on the
  UTC day of deploy.
- 2026-10-31: a question is counted only after an answer or refusal (backend errors no longer
  consume the daily cap); `/health` gained `questions`.
