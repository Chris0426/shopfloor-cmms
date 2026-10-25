# Synthetic instance — replaying a `tendline.seed.v1.1` ledger

A **synthetic instance** is a fresh CMMS database populated not from legacy exports but from a
synthetic event ledger produced by an external generator. The ledger describes a fictional fleet
(assets, spare parts) and the life of its work orders (open → status changes → notes → parts →
fields → close). `cmms seed-events` replays that ledger **through the existing domain services** —
the same governed write path the web UI, the MCP tools and the CLI use — up to a cut-off time.
The resulting instance shares the generator's `world_id`, so a downstream consumer reading this
CMMS and the generator's own materialised view are looking at the same world.

Nothing in the replay writes business tables directly. Assets go through
`AssetService.create_asset`, parts through `InventoryService.create_item` + `adjust_on_hand`,
work orders through `WorkOrderService` (`open_work_order`, the state machine, `add_note`,
`issue_part_to_work_order`, `set_seed_field`, `set_confirmed_reason`). Every row carries the usual
audit columns, with a synthetic `human:synth.*` actor. The only table the replay owns is
`seed_world` (fingerprint + progress).

## 1. Generating a ledger

The generator is external (tendline-board); this repo only consumes its output. Run its emitter
in seed-only mode, e.g. `python wsynth_emit.py --seed-only …`, and point `cmms seed-events` at
the resulting `seed/` directory. A ledger directory contains:

- `seed-manifest.json` — schema `tendline.seed.manifest.v1.1`: `world_id`, `generator_version`,
  `fleet_digest`, `horizon_start`, `exposed_until`, and `days[]` (`day`, `file`, `n_events`,
  `sha256` over the full file bytes).
- `seed-YYYY-MM-DD.jsonl` — one event per line (schema `tendline.seed.v1.1`), one file per
  Asia/Taipei calendar day.

Ledgers are generated data. Do not commit them to this repo.

## 2. Usage

```bash
cmms seed-events SEED_DIR [--until ISO] [--expect-world ID] [--area-map JSON] [--dry-run]
```

| Option | Meaning |
|---|---|
| `SEED_DIR` | Directory holding `seed-manifest.json` and the day files. |
| `--until` | Cut-off. ISO-8601; a **naive** value is Asia/Taipei, `Z` or an offset is honoured; omitted = now. Only events with `at <= until` are applied. |
| `--expect-world` | Reject the ledger unless the manifest's `world_id` matches. |
| `--area-map` | Override the area → line map (JSON object). See §3. |
| `--default-asset-type` | Asset type for `asset.define` events whose `station_cls` is null (default `UNCLASSIFIED`; also env `CMMS_SEED_DEFAULT_ASSET_TYPE`). An empty string disables the fallback. See §5.1. |
| `--dry-run` | Parse, validate and pre-check every event **without touching the database**, then print the summary with `events_applied` = the count that *would* apply. A dry run cannot see a previous run's progress, so it always assumes a fresh instance (resume from seq 1). |

Output and exit codes:

- **0** — success. Exactly one compact JSON line on stdout (keys sorted):
  `schema`, `world_id`, `generator_version`, `fleet_digest`, `horizon_start`, `applied_until`
  (UTC `Z`), `events_applied`, `events_resumed_from_seq`, `skipped_kinds`, `skipped_features`,
  `line_errors`, `errors_by_code`, `warnings`, `folded` (`{"seat_folded_to_owner": n}`, see §5.2).
- **2** — the ledger was rejected as a whole. One JSON line on stderr:
  `{"rejected": "<code>", "detail": "..."}`. Codes: `manifest` (missing or malformed),
  `schema_mismatch`, `world_mismatch`, `manifest_mismatch` (a day file's sha256, line count or
  presence disagrees with the manifest), `test_world` (the manifest marks a test-only world),
  `state_mismatch`, `until_regressed`, `bad_area_map`, `bad_until`.

Per-line problems never abort the run: the line is dropped (or applied with the problem noted),
counted in `errors_by_code`, and logged at WARNING on stderr as `{code, seq, line, detail}`.

`events_applied` counts events that were dispatched to a domain operation and succeeded.
Skipped kinds and skipped features are not "applied"; an event can be applied *and* carry a line
error (for example `fsa_after_open`: the work order is opened without the timestamp).

## 3. Area → line map

The ledger's `asset.define.area` is a generator-side key. It maps onto this CMMS's controlled
`line` vocabulary (`CANONICAL_LINES` in `src/cmms/domain/asset/transform.py`). Default:

```json
{"za": "Zone A", "zb": "Zone B", "zc": "Zone C", "zd": "Zone D"}
```

Older ledgers that use different area keys can still be replayed by passing a full map for their
keys via `--area-map` or `CMMS_SEED_AREA_MAP`.

Override with `--area-map '{"key": "Zone A", ...}'` or the environment variable
`CMMS_SEED_AREA_MAP` (same JSON; the option wins). Every value must be a canonical line, or the
run is rejected with `bad_area_map`. An area key that is not in the map is a line error
`unknown_area`: the asset is **not** created (a raw generator key is never written as a line),
and every later event on that asset becomes an `unknown_asset` error.

## 4. Actors

Each ledger role maps to a synthetic account, created on first use through `IdentityService`
with a random password that is never printed or stored (nobody logs in as these accounts):

| Ledger role | Account | CMMS role |
|---|---|---|
| `role:operator` | `synth.operator` | operator |
| `role:technician` | `synth.technician` | engineer |
| `role:engineer` | `synth.engineer` | engineer |
| `role:planner` | `synth.planner` | admin |
| `role:system` | `synth.system` | admin |

Audit columns read `human:synth.<role>`. The domain services enforce their normal RBAC against
these accounts: if a service refuses an action for that role, the event becomes a line error
`forbidden` and the replay continues — the role is never escalated. The accounts and the
controlled vocabularies the replay needs (work types, statuses, hold reasons, stock transaction
kinds, the mapped lines) are seeded idempotently with provenance `human:cli`.

## 5. Event mapping

| Kind | Domain operation |
|---|---|
| `asset.define` | `create_asset` (`display_name` → description, `station_cls` → asset type, upserted with label = code — or `UNCLASSIFIED` when null, §5.1 — `area` → line); `parent_asset_id` → `link_containment` once the batch of defines is complete. Site `PLANT-1`, no department. |
| `inventory.item` | `create_item`, then `adjust_on_hand` to **20** (an `ADJUST` ledger entry at the event's time). The event's `asset_id` is the sentinel `EID-00000` and is never looked up. |
| `wo.open` | `open_work_order(work_order_no=…, at=…, failure_started_at=…)`. A seat-level `asset_id` is folded to its owner machine first (§5.2). `failure_started_at` must be strictly before `at`, otherwise line error `fsa_after_open` and the work order opens without it. |
| `wo.status` | By `to_status`: `IN_PROGRESS` → `start_work` (from OPEN) or `resume_work`; `ON_HOLD` → `hold_work(hold_reason)`; `COMPLETED` → `complete_work`; `CLOSED` → `close_work_order`; `VOIDED` → `void_work_order`; `CANCELLED` → `cancel_reactive_report` from OPEN, otherwise `void_work_order` plus line error `cancel_mapped_to_void`. All with `at` = the event time, so `status_history.changed_at` equals the ledger. |
| `wo.note` | `add_note(entry_type, body, occurred_at=at)`. |
| `wo.part` | `issue_part_to_work_order(…, at=at)`. |
| `wo.field` | `diagnosis` / `action_taken` / `production_resumed_at` / `scheduled_date` / `downtime_estimated` → `set_seed_field` (whitelisted setter); `confirmed_reason_code` → `set_confirmed_reason`. `production_resumed_at` must be after the first `IN_PROGRESS` (else `opened_at`), else line error `pra_not_after_start`; on a PM work order it is `pra_non_reactive`. |

Notes and part issues carry the idempotency key `seed:<world_id>:<seq>`.

### 5.1 `station_cls: null` → `UNCLASSIFIED`

Some assets in the ledger have no station class: jigs, fixtures, benches. The Board treats these
as "not measurable" machines, meaning they exist and get work orders but have no MES station to
measure. Following the Board's ruling, they are created with `asset_type = UNCLASSIFIED`. That
lookup is upserted with the label "Unclassified (no station class in source)", so the code says
plainly that the source had no class; it does not invent a real type. They are applied (not
errors) and counted under `skipped_features` as `asset.define.station_cls:null`.
`--default-asset-type CODE` (or `CMMS_SEED_DEFAULT_ASSET_TYPE`) changes the code. An empty string
disables the fallback: the asset is then not created, which is a line error
`station_cls_missing`, and its events become `unknown_asset`.

### 5.2 Seat-level work orders fold to the owner machine

Seats (nest positions) are not assets here. A work-order `asset_id` matching
`^(EID-\d{5})-N\d+$` (for example `EID-12345-N3`) is folded to the owner machine id (`EID-12345`)
on `wo.open` **and** on every later `wo.*` event for that work order. This is the same rule as the
Board's `normalize_wo`, which folds seat ids to the parent EID when it reads this CMMS, so both
sides attribute the work order to the same machine. `brief_description` is not changed.
`wo_asset_mismatch` is checked against the folded id. Each folded `wo.open` that is applied
counts once in `folded.seat_folded_to_owner`; follow-up events are then normal. If the owner
machine is not in master, the open is the usual `unknown_asset` error.

The ledger's `is_downtime` flag is **not** applied — the CMMS computes downtime itself from the
status history. When the ledger's flag disagrees with `segment_is_downtime` for that segment, it
is counted under `warnings.is_downtime_mismatch` (not a line error).

## 6. What is skipped, and why

| Skipped | Counted under | Why |
|---|---|---|
| `asset.schedule_state`, `pm.schedule` (reserved), unknown kinds | `skipped_kinds` | No equivalent concept here (production scheduling is not this system's job); unknown kinds are the contract's forward-compatibility rule. |
| `wo.scope` events, non-empty `wo.open.scope` | `skipped_features` (`wo.scope`, `wo.open.scope`) | The CMMS has no sub-unit scope on a work order. |
| `asset.define.roles` | `skipped_features` (`asset.define.roles:<role>`) | Co-tagging / aliasing has no equivalent; containment is taken from `parent_asset_id`. |
| `asset.define.seats` | `skipped_features` (`asset.define.seats`) | Seats (nest positions) are not assets here; seat-level work orders fold to the owner machine (§5.2). |
| `asset.define` with `station_cls: null` | `skipped_features` (`asset.define.station_cls:null`) | Applied with asset type `UNCLASSIFIED` (§5.1); only a line error `station_cls_missing` when the fallback is disabled. |
| `wo.field assigned_vendor` | `skipped_features` (`wo.field.assigned_vendor`) | No governed setter exists. |
| `stations`, `in_service_from`, `sub_units`, `pm_source_id` | not counted | Not represented in the CMMS. |

## 7. Resume semantics

The first run inserts a `seed_world` row keyed by `world_id` holding the manifest fingerprint
(`schema`, `world_id`, `generator_version`, `fleet_digest`, `horizon_start`) and the progress
(`last_seq`, `applied_until`). A later run against the same world:

- with an **identical** fingerprint resumes from `last_seq + 1` (events already consumed are not
  replayed; the pre-checker re-derives its in-memory state from them);
- with a **different** fingerprint is rejected (`state_mismatch`) — a regenerated world is a
  different world; use a fresh database;
- with `--until` **earlier** than the stored `applied_until` is rejected (`until_regressed`).

Re-running with the same cut-off is a no-op. Advancing the cut-off ("show me one more day")
applies only the new events.

Each event is its own transaction, because every domain operation commits its own `write()`
block. The progress update for an event is staged on the same session, so the event's commit
carries it: an interruption leaves the database and `last_seq` consistent. An event the domain
rejects is rolled back, recorded as a line error, and still counts as consumed.

## 8. Pages that depend on the wall clock

A synthetic instance replayed to a past cut-off still runs against today's clock. Most pages only
render stored timestamps and look the same whenever you open them. The exceptions:

- **`/app/pm`** (and `/app/pm/calendar`, and the PM **backfill** page `/app/pm/{pm_id}/backfill`)
  compute due / overdue against *today in Asia/Taipei*. PM schedules in a world replayed to a
  past date will read as overdue.
- **`/app/proposals`** and **`/admin/proposals`** show proposal expiry relative to *now*
  (`/admin/proposals` also sweeps stale proposals to `EXPIRED` when viewed).

Everything else renders stored timestamps. Downtime is computed from the status history and
locked at close (`downtime_minutes`), so it does not drift. There is no "N minutes ago" style
relative-time text anywhere in the UI.

## 9. Run it with docker compose

`docker-compose.seed.yml` is an override on `docker-compose.yml` that turns the stack into a
**parity instance**: the `seed` service skips the demo generator and replays a ledger instead
(`scripts/seed_demo.sh` switches to ledger mode when `SEED_DIR` is set). Differences from the
demo stack: `CMMS_DEMO_MODE=0` (no banner, no one-click accounts on the login page), the app is
published on `127.0.0.1:8001` only (no `edge`), the image is tagged `shopfloor-cmms:seed`, and
there is **no reset timer** — never run `scripts/demo_reset.sh` against it.

Write a `.env` in the checkout (git-ignored; compose reads it by itself):

```dotenv
COMPOSE_PROJECT_NAME=cmms-a18
COMPOSE_FILE=docker-compose.yml:docker-compose.seed.yml
SEED_DIR=/abs/path/to/ledger/seed
SEED_UNTIL=2026-10-01T19:36:00
SEED_EXPECT_WORLD=<world_id from seed-manifest.json>
CMMS_PUBLIC_BASE_URL=http://127.0.0.1:8001
```

`COMPOSE_PROJECT_NAME` is **required** when the demo stack runs on the same host: without it
the project name defaults to the directory name and containers or the `pgdata` volume can
collide. `SEED_DIR` is the host directory, mounted read-only at `/seed`. Add
`CMMS_SEED_AREA_MAP='{"key":"Zone A", ...}'` (single-quoted, so the JSON survives `.env`
parsing) only for a ledger with non-default area keys; check it with
`docker compose config | grep CMMS_SEED_AREA_MAP`.

```bash
docker compose build
docker compose up -d --wait db app
docker compose run --rm --no-deps seed     # last line = the seed-events JSON summary; exit code is the CLI's
```

The seed step runs migrations, creates the usual accounts (`admin`, `jordan.lee`,
`operator.a`; see `docs/DEMO.md`) and then `cmms seed-events /seed --until … --expect-world …`.
To advance the cut-off later, raise `SEED_UNTIL` in `.env` (it may only move forward, see §7)
and run `docker compose run --rm --no-deps seed` again: only the new events are applied, and
the existing accounts are left alone. Tear the instance down, database included, with
`docker compose down -v`.
