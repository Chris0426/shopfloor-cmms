# CMMS Domain Model — 08. Failure Vocabulary (C2 shared controlled vocabulary)

> Deliverable: the failure_vocab slice (C2). Sources: two seed CSVs — the `mfc` axis (material fail flags plus a few triage categories) and the `efc` axis (equipment failure codes). Both are supplied as files by **a sibling analytics project** (referred to below as *the analytics consumer*), which mined them on the MES side. **The seed files themselves are plant data and are not part of this repository**; the shapes below, plus the golden fixtures under `tests/fixtures/`, are what the code is written against.
> Status: **v1.0 — slice live (migration 0023)**. The CMMS owns the **single authoritative lookup** for the controlled vocabulary; the analytics consumer only supplies seeds.
> **This slice does not touch MES (guardrail #7)** — it is a pure vocabulary lookup: no B2MML, no DMZ FTP, no direct SQL.

---

## 0. Module scope

`failure_vocab` is the **shared controlled vocabulary** layer: it centralises the vocabulary of "failure reasons" in the CMMS (the maintenance system of record) as the single authority, for use in future reason-code rollups, work-order closing-reason classification, and mapping inside a downstream analytics fusion layer. The analytics consumer mines the vocabulary from the MES side and supplies the seed; the CMMS owns the lookup and governs retirement.

### 0.1 Two axes — **never merged into one table**

| Axis | Table | Question it answers | Seed | Natural key |
|---|---|---|---|---|
| **mfc** | `mes_failmode` | Why was **material rejected** (product / yield) | the material fail-flag seed | **(station, label)** composite |
| **efc** | `equipment_failure_code` | Why did the **machine fail** (equipment) | the equipment failure-code seed | `code` |

These are **different questions** (product yield vs equipment health) and the semantics are not interchangeable, hence **two tables**. Even where the two axes carry a similarly named concept at the same station, the meaning differs: "this unit of material was rejected here" is not "this machine faulted here". Non-merging is enforced at the design level.

---

## 1. mfc (`mes_failmode`) — the product / yield axis

### 1.1 Columns

| Target column | Type | Required | Notes |
|---|---|---|---|
| `id` | bigint | ✅ PK | synthetic auto-increment key |
| `station` | text | ✅ | station key (`sta1` … `sta6`); first element of the natural key |
| `label` | text | ✅ | short label (second element of the adapter's FAIL_FLAGS tuple); second element of the natural key |
| `signal_id` | text | ⬜ | `mes.failmode.<lowercased label>` (generated upstream); empty for triage rows. **Stored at face value, never recomputed** |
| `entry_kind` | text | ✅ | `fail_flag` (a clean failure flag) \| `triage_category` (a playbook-level triage bucket) |
| `seg_class` | text | ⬜ | the production segment class the flag is scored under, as supplied in the seed |
| `mes_variable` | text | ⬜ | the flag variable name as persisted upstream (`mfc*`) |
| `material_class` | text | ⬜ | the yield unit the flag is scored against (e.g. `assyModule`) |
| `semantic_zh` | text | ⬜ | plain-language meaning (the CMMS may rewrite / consolidate these) |
| `dominant_in_chronic` | text | ⬜ | "dominant in chronic failures" marker; **stored verbatim** (`y+<number>` / `n` / `TODO(calibrate)` / `raw90d:<n>`) and never interpreted |
| `source_adapter` | text | ⬜ | the adapter file it came from |
| `notes` | text | ⬜ | free notes |
| `is_active` | bool | ✅ (default true) | retirement flag; the loader **never** flips it (see §4) |
| `created_at/by`, `updated_at/by`, `source_actor`, `proposed_by`, `confirmed_by` | — | — | `AuditMixin` (ADR-005/016) |

### 1.2 ★ `signal_id` collides across stations (landmine)

`signal_id` is generated deterministically by lowercasing `label`, so **the same label at different stations produces the same `signal_id`**: generic labels (a sensor failure, a short, a bad contact) recur at several stations alike — different segments, different stations, potentially subtly different meanings. Therefore:

- **The natural key must be `(station, label)`**. It **must never** be `signal_id` alone — that would fold the same-named failure at three stations into one row and destroy per-station failure statistics.
- Triage (playbook-level) rows have **no** `signal_id` at all (left empty).
- The `signal_id` column is nullable, carries no unique constraint, and exists purely to preserve the source value.

### 1.3 `entry_kind`

- `fail_flag`: `signal_id` is non-empty → a clean failure flag (the vast majority of rows).
- `triage_category`: `signal_id` is empty and `label` starts with `triage_` → a playbook-level triage bucket, so the CMMS can build "reason categories".
- `signal_id` empty but `label` is not a triage label → **the parser raises** (an unexpected shape; fail honestly, do not guess — guardrail #8).
- **Rows with an empty `label` are documentation rows for zero-flag stations** (some stations carry no per-fail-mode flag at all; the seed lists them honestly, but they are not vocabulary) → the loader **skips** them and counts them in `skipped_doc_rows`.

### 1.4 What the loader reports

The load result separates the three populations it saw: `fail_flag` entries, `triage_category` entries, and `skipped_doc_rows`. The vast majority are fail flags; triage buckets and documentation rows are a handful each. The counts are echoed by the CLI so that a load can be reconciled against the seed the analytics consumer handed over — an honest count, never an estimate (see the failure-pattern catalogue in the execution playbook).

---

## 2. efc (`equipment_failure_code`) — the equipment axis

### 2.1 Columns

| Target column | Type | Required | Notes |
|---|---|---|---|
| `id` | bigint | ✅ PK | synthetic auto-increment key |
| `code` | text | ✅ UNIQUE | the efc variable name (= the failure-code value carried by the MES equipment-failure event feed); the natural key |
| `descr` | text | ⬜ | the human-readable description that ships with the code |
| `station_hint` | text | ⬜ | station **inferred from the code prefix** (**not authoritative**); the literal `TODO` in the seed becomes **None** (see §2.3) |
| `recency_status` | text | ⬜ | e.g. `source_alive_2026-07` (whether the source was still live at survey time) |
| `is_active` | bool | ✅ (default true) | retirement flag; the loader **never** flips it |
| `AuditMixin` (7 columns) | — | — | ADR-005/016 |

The codes themselves are vendor-defined alarm identifiers of the form `efc<Prefix>_<AlarmName>`, where the prefix is a station or sub-assembly abbreviation. Structurally illustrative examples only — this document deliberately does **not** reproduce an equipment vendor's alarm dictionary, and the strings below are invented to show the shape:

```
efcSTA4_AirPressureLow          -- pneumatic supply below setpoint at STA4
efcSTA2_DoorOpen                -- service door interlock opened
efcSTA6_AlignmentFailed         -- the station could not align the part
efcSTA3_TempOutOfRange          -- process temperature outside the allowed band
```

### 2.2 Constant-column drift guard (not stored)

Every seed row carries four **constant** extra columns: a dimension-class tag, the upstream source table, the source column, and an axis marker (`axis=equipment`). These four are **not stored** (identical on every row, zero information), but the transform **validates them row by row** against the expected values and raises on mismatch — a drift guard: if the analytics consumer later changes the source table or shifts a column, the load explodes immediately instead of silently ingesting wrong data.

### 2.3 `station_hint` caveat (prefix inference, not authoritative)

`station_hint` is inferred from the `code` prefix and is **not authoritative**. Most prefixes are corroborated by an adapter or a station profile; a few are inferred but have no station built out, and one is a sentinel (a generic equipment-PLC dump that belongs to no station). **A few sub-assembly (`*SA`) prefix families are unresolved**: their descriptions are all generic PLC conditions (comms / recipe / limits) and cannot be attributed to an exact station. The seed marks these with the literal `TODO`; the CMMS stores **None** on load (`station_hint IS NULL`) and the admin UI renders "—", honestly expressing "station unknown" rather than inventing a binding.

### 2.4 Emit dependency (blocked)

efc is a **pure vocabulary layer**. It creates **no** object that references the analytics consumer's emit schema — that schema is still a draft. efc *consumption* (mapping codes onto work orders / the analytics fusion layer) is blocked until the consumer freezes its equipment-failure emit contract at v1.

---

## 3. External contract (reads / admin display)

- **`/admin/vocab`**: one read-only table per axis (mfc shows station / label / signal_id / entry_kind / semantic_zh; efc shows code / descr / station_hint). The efc section is annotated with "station_hint is inferred from the prefix and is not authoritative".
- The read service `FailureVocabService.list_mes_failmodes(station=)` / `list_equipment_failure_codes(station_hint=)` serves internal callers, the admin page, and the read API (§3.1).

### 3.1 Read API — `GET /vocab/failure`

The downstream analytics consumer confirmed it consumes both C2 axes (to map them onto work orders in its fusion layer), so a read-only HTTP JSON endpoint was opened (additive).

| Item | Value |
|---|---|
| **Endpoint** | `GET /vocab/failure` (a thin client — it only calls the two `FailureVocabService` list methods) |
| **schema_id** | **`contract_failure_vocab.v1`** |
| **golden fixture** | `tests/fixtures/contract_failure_vocab.v1.json` (shape drift → the contract test fails → notify the consumer; a breaking change bumps to v2 rather than overwriting in place) |
| **authN** | Protected by the existing **static bearer** middleware (`/vocab` is not on the exemption list in `api/auth.py`, so the middleware covers it automatically; missing/wrong token → 401; unset in production → 503) |
| **Parameters** | **None** (v1 dumps both axes in full — a few hundred rows in total; query parameters can be added additively later) |
| **Response envelope** | `{ "mes_failmodes": [...], "equipment_failure_codes": [...] }` — **the two axes are listed separately and never merged** |

**Exposed fields** (all other internal columns are deliberately withheld: `id`, the audit columns, `source_adapter`, `notes`; they can be added additively later):

- mfc (`MesFailmodeRead`): `station` / `label` / `signal_id` / `entry_kind` / `seg_class` / `mes_variable` / `material_class` / `semantic_zh` / `dominant_in_chronic` / `is_active`.
- efc (`EquipmentFailureCodeRead`): `code` / `descr` / `station_hint` / `recency_status` / `is_active`.

**Semantics of retired rows**: rows with `is_active=false` are **still exposed** — a downstream consumer resolving an old code referenced by a historical work order or signal needs them, and the flag honestly says "this code is retired, do not use it for new records" instead of making the lookup miss. Ordering is deterministic (mfc by `(station, label)`, efc by `code`).

- **Impact on the downstream analytics consumer**: this endpoint is an entirely new read surface and changes no existing shape; the consumer pulls `contract_failure_vocab.v1` when it starts mapping.

---

## 4. Additive-only + `is_active` policy

- **The loader is an idempotent upsert** (`on_conflict_do_update` on the natural key): re-runnable, and on conflict it only updates the **content columns**.
- **`is_active` is excluded by the loader**: inserts rely on the server default `true`, and on conflict the column is **not in the update set** — so once an admin retires a code (`is_active=false`), re-running the loader **must not resurrect it**. This is the concrete implementation of additive-only: seeds may add rows and update content, but retirement is an administrative governance decision, exposed by a governed setter in the follow-up D6 slice.
- Audit (guardrail #3): the `AuditMixin` columns are populated by the service; the loader runs as `Actor.human("migration")`.

---

## 5. Target schema (migration 0023)

```sql
CREATE TABLE mes_failmode (
    id                  bigint GENERATED ... PRIMARY KEY,
    station             text NOT NULL,
    label               text NOT NULL,
    signal_id           text,                 -- empty for triage rows; collides across stations, never a unique key
    entry_kind          text NOT NULL,        -- fail_flag / triage_category
    seg_class           text, mes_variable text, material_class text,
    semantic_zh         text, dominant_in_chronic text, source_adapter text, notes text,
    is_active           boolean NOT NULL DEFAULT true,
    -- AuditMixin ...
);
CREATE UNIQUE INDEX uq_mes_failmode_station_label ON mes_failmode (station, label);
CREATE INDEX        ix_mes_failmode_station        ON mes_failmode (station);

CREATE TABLE equipment_failure_code (
    id              bigint GENERATED ... PRIMARY KEY,
    code            text NOT NULL,
    descr           text, station_hint text, recency_status text,
    is_active       boolean NOT NULL DEFAULT true,
    -- AuditMixin ...
);
CREATE UNIQUE INDEX uq_equipment_failure_code_code ON equipment_failure_code (code);
```

---

## 6. Loading (operator steps)

Migration 0023 creates the tables only and seeds **no data**; the seed goes through the CLI (same pattern as the other large loads — relationships / part issues / media — which run on-box in production):

```
cmms load-mes-failmodes <seed.csv>   # prints: fail_flag / triage_category / skipped_doc_rows counts
cmms load-efc-codes     <seed.csv>   # prints: codes upserted
```

Prerequisites: none (an independent vocabulary; it does not depend on asset or inventory). Idempotent and re-runnable.

---

## 7. Follow-ups (non-blocking)

1. **D6 governed add/update + retirement**: an admin surface for add/update plus an `is_active` setter (propose → confirm, or direct admin write). This slice ships only the loader upsert plus the read-only display.
2. **Canonical `reason_code` rollup**: rolling mfc entries (which are fine-grained, per-signal detail) up into a single "reason category" — **deferred to D6 Phase 2**. The other half, `close_work_order(confirmed_reason_code=)`, **is live (migration 0027)**: `work_order.confirmed_reason_code` FKs to `equipment_failure_code.code` (efc axis only, optional, REACTIVE work orders only, admin-only correction once CLOSED, `null` = "not confirmed" ≠ "no fault"); `finish_work_order` / `close_work_order` / `set_confirmed_reason` can all write it, and `contract_wo_detail.v1` exposes it additively. What is **not** done is the rollup into canonical categories and the wiring of the mfc axis (still Phase 2). See `02-work-orders.md` §1.3.
3. **efc emit consumption**: blocked until the analytics consumer freezes its equipment-failure emit contract at v1.

---

## 8. efc code × work-order cross-check seed

**Purpose**: on the equipment axis, **event volume ≠ failure**. A high-volume alarm code (a band-limit warning, say) can fire again and again while the machine keeps running and nobody ever raises a work order — a nuisance code. A low-volume code that lines up with breakdowns every time is the opposite. Nothing in the vocabulary itself tells you which is which; **the maintenance record does**. The division of labour: **the CMMS produces the objective seed** (as the maintenance system of record it is the most authoritative objective anchor), the analytics consumer trains a classifier on it, and the vocabulary owner supplies the codes plus on-site judgement. This slice takes an event CSV, cross-checks every code against the CMMS's REACTIVE work-order activity windows, and emits a JSON seed in governed-vocabulary shape with per-code provenance, hardened by a golden fixture.

**Input** (delivered as a file; guardrail #7 — the CMMS never reads the MES event store directly): an event CSV with the header `efc_code,eid,event_timestamp` (utf-8-sig; EIDs already resolved on the other side; typically the highest-volume codes over a recent window). Timestamps are naive local wall-clock. The file is plant data and is **not** shipped in this repository; the golden fixture defines the output shape.

**Method**: an event is *matched* if its timestamp (local naive → UTC) falls inside the active window `[opened_at − buffer, (closed_at or as_of) + buffer]` of **any** REACTIVE work order for that EID whose status ∉ {CANCELLED, VOIDED}. The buffer is day-scale (default 1 day, adjustable via the CLI), because historical work orders only have day-level (plus a default time) granularity and faults are commonly reported before the work order is opened or persist after it is closed → **the ratio is indicative, not minute-accurate** (honestly recorded in `granularity_note`). Unknown EIDs (not in the asset master) are excluded from the ratio denominator and counted in `n_events_unknown_eid`; a known EID with zero work orders counts as unmatched (that **is** the signal). For OPEN work orders the window end is `as_of`.

**★ Privacy red line (consumer requirement)**: the cross-check uses **only the "existence + time window" fields of a work order** (`work_order_no` / `asset_id` / `work_type` / `status` / `opened_at` / `closed_at`) and **never reads and never emits any personnel field** (`assigned_person` / `opened_by` / `closed_by` / `source_actor`, …). The DB reader's SELECT projects only the fields listed above; the seed output contains no personnel surface at all.

| Item | Value |
|---|---|
| **schema_id** | **`efc_workorder_crosscheck_seed.v1`** |
| **golden fixture** | `tests/fixtures/efc_workorder_crosscheck_seed.v1.json` (shape drift → contract test fails; a breaking change bumps to v2 rather than overwriting in place) |
| **Shape** | `{schema_id, generated_at, method{events_file, buffer_days, as_of, n_rows_skipped, wo_scope, granularity_note, verdict_rules}, codes[]}`; `codes` sorted by `n_events_total_checked` DESC |
| **Per code** | `code / n_events_total_checked / n_events_matched / n_events_unknown_eid / ratio (4 dp) / verdict_hint / evidence{eids_seen, event_period, matched_work_orders (capped at 20, with a truncation flag)}` |
| **verdict_hint** | `real_fault` (matched ≥ 3 and ratio ≥ 0.30) / `nuisance` (total ≥ 100 and ratio ≤ 0.02) / `insufficient_data`. **A hint from transparent thresholds, not an authority** — the final verdict is curated in the C2 lookup (the vocabulary-authority split is unchanged) |

**Output** (an offline transform plus a thin CLI; read-only; no migration, no API, no MCP):

```
cmms efc-workorder-crosscheck <events.csv> --out efc_crosscheck_seed.json
# optional: --buffer-days N (default 1), --as-of <ISO> (for reproducibility; defaults to now, UTC)
```

The DB reader writes nothing (respecting the single-write-path guardrail). Code: `src/cmms/domain/failure_vocab/crosscheck.py` (pure functions `read_efc_events` / `crosscheck` / `build_seed` plus the thin async `fetch_wo_windows` / `generate_seed`).
