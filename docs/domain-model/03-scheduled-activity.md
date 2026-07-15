# CMMS Domain Model — 03. Scheduled Activity (Preventive Maintenance Schedules)

> Extraction Spec, page 3. Sources: eMaint X4 UI exploration + the `scheduled_activity.csv` export + cross-module validation.
> Status: **v1.0 — the read slice for the entity itself (#3, `pm_schedule`) is live and verified against real PostgreSQL** (migration 0003, `alembic check` clean, idle rows flagged via T3, `assignto` split into the vendor lookup + person).
> Rulings: split `assignto` into vendor (lookup) + person (text); all writes deferred (read-only slice); S1/S2/`consumables` get empty columns or are deferred.
> **PM generation / suppress / recalculation as governed writes, the `consumables` N:M table, and the Shadow/Fixed recalculation logic (S1/S4) still await later slices + UI extraction.**

---

## 0. Where this module sits

Scheduled Activity binds one **asset** to one **maintenance task**, adding a frequency, a next-due date, an owner, standard hours and consumables — i.e. "this recurring maintenance job on that machine". It is the middle of the PM chain:

```
Asset ──┐
        ├──▶ ScheduledActivity ──(auto-generates when due)──▶ WorkOrder (type=PM)
Task ───┘
```

Data shape: roughly a thousand PM definitions — a few per maintained machine, drawn from a smaller catalogue of reusable task templates (the same task recurs across a machine family).

---

## 1. Entity: ScheduledActivity (proposed new name `pm_schedule`)

**[CSV]** = exported in `scheduled_activity.csv`; **[UI]** = only on the eMaint form, needs extraction; **[DROP]** = denormalized copy, removed in the new system.

### 1.1 Identity and relationships

| Target field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `pm_id` | [CSV] `pmid` | string | ✅ PK | **Two namespaces**: the legacy eMaint ids are **opaque surrogate keys** (an underscore-prefixed alphanumeric blob with no parseable structure — carried verbatim, never decoded), while schedules created in the web UI are minted in our own namespace `PMW-XXXXXXXX` (by `create_pm_schedule`; the two can never collide). **Consumers must not assume the eMaint form** |
| `asset_id` | [CSV] `compid` | string | ✅ FK | →`Asset.asset_id`; ✅ 0 orphans |
| `task_id` | [CSV] `task_no` | string | ✅ FK | →`Task.task_no`; ✅ 0 orphans |
| — | (`asset_id`, `task_id`) | — | ✅ UNIQUE | **Natural unique key**: verified to have zero duplicates across the whole export — a given machine + task pair can only be defined once |
| `line_no` / `comp_desc` / `task_desc` | [CSV] | — | [DROP] | Denormalized; derive from Asset / Task |
| `pm_type` | [CSV] | — | [DROP] | Constant `PM`, carries no information |

### 1.2 Frequency

| Target field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `frequency_interval` | [CSV] `pmfreqx` | integer | ✅ | The interval value. **`0` = non-recurring (one-off / as-needed)** — a small handful of rows, in which case `pmfreq` is null |
| `frequency_unit` | [CSV] `pmfreq` | enum | conditional | `Months` (the great majority) / `Days` / `Weeks` (rare); null when `frequency_interval=0` |
| `calendar_freq_type` | [UI] | enum | ⬜ | **A key field**: `Shadow` (next date computed from the *actual completion date*) / `Fixed` (computed from a fixed calendar). The UI shows `Shadow`; not in the export |
| `skip_weekends_holidays` | [UI] | bool | ⬜ | The "skip weekends and public holidays" flag is **not among the 17 exported columns** — UI-only, needs extraction |

> Frequency examples: `12 Months` (annual), `6 Months`, `183 Days` (semi-annual), `60 Months` (five-yearly), `14 Days`.

### 1.3 Schedule timeline

| Target field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `next_due_date` | [CSV] `pmnextdate` | date | ⬜ (34%) | Next maintenance date; long-interval PMs push it years out. **A number of schedules are already overdue** at import time — the new system must not assume the legacy data is in a healthy state. Most of the nulls come from suppressed schedules (see §1.5) |
| `last_pm_date` | [CSV] `lastpmdate` | date | ⬜ (94%) | Last maintenance date |
| `last_work_order_no` | [CSV] `lastpmno` | bigint | ⬜ FK | →`WorkOrder.work_order_no`, the most recent PM work order; ~93% resolve, the rest apparently point at purged legacy work orders → **soft reference, no FK constraint** |
| `completion_window_days` | [CSV] `dayscmpl` | decimal | ✅ | How many days after generation the work order should be completed within; default `2.5` |

### 1.4 Hours and resources

| Target field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `standard_hours` | [CSV] `standard` | decimal | ⬜ | Standard hours; possibly inherited from the Task definition (**TODO confirm**) |
| `estimated_labor_hours` | [CSV] `estlabor` | decimal | ⬜ | Estimated hours for this schedule; it **differs** from `standard` on a meaningful minority of rows, so the two are not the same quantity and both columns are kept |
| `consumables` | [UI] | relation | ⬜ | The "consumables that may need replacing" — a ScheduledActivity↔Inventory relationship, not exported, needs extraction (see §4) |

### 1.5 Status and assignment

| Target field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `is_suppressed` | [CSV] `suppress` | bool | ✅ | The disable flag — a suppressed schedule generates no work orders. **More than half the legacy schedules are suppressed**, and most of those also have no `next_due_date`: the two facts are consistent, and together they mean "the due list must be derived, never assumed from row count" |
| `assigned_vendor` | [CSV] `assignto` (parsed) | enum | ⬜ (72%) | The maintenance contractor (the same `vendor` lookup as WorkOrder) |
| `assigned_person` | [CSV] `assignto` (parsed) | string→FK | ⬜ | Of the form `VENDOR (Person Name)`, must be parsed. **Since 0029 this degrades to an explicit override** — see below |
| `pm_group` | [UI] | string | ⬜ | The "PM Group" grouping field visible in the UI; not exported |

> **★ Assignee derivation + multiple owners (0029 → 0031)** — the source of truth for ownership is the **asset owner list `asset_owner`** (multi-owner since 0031; see `01-assets.md`). `pm.assigned_person` is the rare **per-PM override (single value, retained)**.
> - **Generation precedence** (`_generate_pm_impl`): an explicit override argument (single value) → `pm.assigned_person` (the per-PM override, single value) → **the asset owners (all of them)**. The generated work order is assigned to every effective owner (written to `work_order_assignee`).
> - **Reading the effective assignee**: the effective assignee is the per-PM override if set, otherwise **all** asset owners. Every PM list / calendar / detail view, and the prefill when raising a catch-up work order, annotate `effective_assignees` (a list) / `effective_assignee` (the same list joined into one string); the "Mine" filter matches `pm.assigned_person == X` OR (`pm.assigned_person` is empty AND X is in `asset_owner`); exports use `coalesce(pm.assigned_person, asset owners joined with "; ")`.
> - The single-value `asset.owner` column from 0029 was migrated into `asset_owner` and dropped in 0031.

---

## 2. Operations (CRUD + actions)

CRUD; `suppress` / `unsuppress`; **generating the PM work order** (auto-triggered when due + on demand — the core operation; both the on-demand path and the automatic scheduler are live, see §3.1); writing back the completion and recalculating `next_due_date`.

> Modernization notes: "generate the PM work order" is a schedule/data-driven domain operation, not a user typing an UPDATE. Toggling `is_suppressed` and bulk frequency changes are governed operations.

---

## 3. Workflow: the PM cycle

ScheduledActivity is not itself a rich state machine — it has one flag, `Active ⇄ Suppressed`. The real "process" is the recurring cycle it drives:

```
  next_due_date arrives
        │  (and is_suppressed = false)
        ▼
  auto-generate WorkOrder (type=PM)  ──▶  record last_work_order_no / last_pm_date
        │
        ▼
  PM work order completed and closed
        │
        ▼
  recalculate next_due_date
     ├─ calendar_freq_type=Shadow → from the actual completion date + interval
     └─ calendar_freq_type=Fixed  → from the fixed calendar + interval
        │
        └──────────────▶ (back to the top)
```

- `frequency_interval=0` (non-recurring) → never enters the cycle; it can only be generated manually, once.
- `is_suppressed=true` → the cycle is paused.

### 3.1 Live: time-based generation (on demand + automatic scheduler) + completion write-back (ADR-021)

Both ADR-021 execution modes are **live** (2026-06-28): ① **review + run on demand** ② the **automatic scheduler** (unattended). They share one generation core, `_generate_pm_impl`, both go through the `WorkOrderService` single write path (guardrail #1), and both are fully audited and idempotent. Neither goes through gated write (PM generation is schedule-deterministic, not discretionary — ADR-021).

- **`generate_pm_work_order(pm_id, actor)`** (on demand, `WorkOrderService`): an engineer hits "Run" on a due PM → a `WorkOrder(work_type=PM, status=OPEN)` is opened with `brief_description` taken from `Task.description`, and `pm_schedule.last_work_order_no = wo` and `pm_source_id = pm_id` are written back.
  - **Idempotent**: if this PM already has an unclosed PM work order for the current cycle (`last_work_order_no` points at it and it is not in a terminal state) → that work order is returned, nothing new is created; calling again after it closes creates a new one (the next cycle).
  - **Suppression is allowed**: on demand *is* an explicit human override; `is_suppressed` governs only the automatic scheduler and the due list — it **does not block the on-demand path**. The initiator is `Actor.human` (the engineer).
- **`generate_due_pm_work_orders(actor, as_of=today, limit=None)`** (the automatic scheduler, `WorkOrderService`; unattended): batch-generates work orders for PMs that are **due** (`next_due_date <= as_of`), not suppressed, and **recurring** (`frequency_interval>0` and `frequency_unit` non-null); it shares `_generate_pm_impl` (and therefore the same idempotency logic) with the on-demand path.
  - **One transaction per PM**: each is generated inside its own transaction so a single failure is isolated as an `error` and does not affect the rest (unattended resilience); it returns `list[PmGenerationResult]` (`pm_id` / `work_order_no` / `created` / `error`).
  - **Recurring only**: non-recurring / one-off PMs are excluded from the scheduler (they stay on demand) — otherwise closing them would not advance `next_due_date` and the scheduler would regenerate them on every run.
  - **`source_actor = scheduler`** (`Actor.scheduler()`, ADR-021 §4: honestly labelled as clock-driven, distinct from `mes-pipeline` / `human` / `agent`). The CLI entry point `cmms pm-generate-due [--as-of YYYY-MM-DD] [--limit N]` is what a Fly scheduled machine / cron calls daily.
  - **No lead window**: opening work orders ahead of time would require a non-downtime `PLANNED` state, which the current 7-state machine does not have (and `OPEN.is_downtime=true`); this waits for the `calendar_freq_type` [UI] extraction plus a state-machine extension (ADR-021 §3, guardrail #8).
- **Completion write-back** (`close_work_order` → `_advance_pm_schedule`, in the same transaction): closing a PM work order advances its schedule (identically for on-demand and scheduler-generated work orders).
  - **Recurring**: `base = next_due_date` (or the completion date if absent) → `add_interval(base, frequency_interval, frequency_unit)`; and `last_pm_date = the completion date` is recorded.
  - **Non-recurring** (`frequency_interval=0` or `frequency_unit` is None): only `last_pm_date` is recorded; `next_due_date` is **left alone** (it never enters the cycle).
- **Fixed vs Floating**: currently everything is **Fixed** (counted from the scheduled `next_due_date`). **Floating** (counted from the actual completion date, to prevent pile-up — the Shadow branch in the diagram above) awaits the `calendar_freq_type` [UI] extraction before it can be branched (S1/S4).
- **The due list**: `list_pm_schedules(due_on_or_before=…, is_suppressed=false)` returns PMs that are due and not suppressed, for selection and generation.
- **Next-due computation** is the pure function `add_interval` (`pm_schedule/transform.py`): Days/Weeks add directly; Months advance by calendar month (year carry + end-of-month clamp, e.g. Jan 31 + 1mo → Feb 28/29).

### 3.2 Pre-weekend generation (2026-07-05)

If the due date falls on a **Saturday or Sunday**, the **preceding Friday** becomes the "effective generation date" — i.e. from that Friday the scheduler and the due list already select and generate the PM (so the technician picks up the work order during the week, rather than nobody being there at the weekend and the job slipping). The pure function `effective_generation_date(due)` (`pm_schedule/transform.py`): Saturday → −1 day, Sunday → −2 days, weekdays unchanged.

- It **only affects when generation / due-ness is judged** — it does **not** change `next_due_date` itself, and it does **not** affect the Fixed advance chain (`_advance_pm_schedule` still counts from the scheduled `next_due_date`).
- `generate_due_pm_work_orders` selects in two stages: SQL widens to `next_due_date <= as_of + 2 days` (the maximum lead), then `effective_generation_date(due) <= as_of` filters precisely. The due list (the `pm_due` route) does the same.
- **On-demand generation** (`generate_pm_work_order`) adds no hard due-date gate of its own (it is an explicit human override); but the *visibility* condition of the web "raise catch-up work order" button uses the same effective due date (`pm_due_by`) plus "not yet generated this cycle" (the work order that `last_work_order_no` points at is not terminal).
- **Public holidays are out of scope for v1** (that needs a holiday table): a due date landing on a holiday is not pulled forward for now; `effective_generation_date` can be extended later (falls on a holiday → keep walking back to the previous working day).

---

## 4. Relationships to other entities

| Relationship | Target | Cardinality | Join key | Validation |
|---|---|---|---|---|
| Asset | Asset | N : 1 | `compid` | ✅ 0 orphans |
| Maintenance task | Task | N : 1 | `task_no` | ✅ 0 orphans |
| Generated work orders | WorkOrder (PM) | 1 : N | `lastpmno` / `pm_source_id` | ✅ ~93% hit (the rest point at purged legacy WOs) |
| Consumables | Inventory | N : M | (consumables) | UI-only, needs the §7 extraction |
| Owner | Contact / Person | N : 1 | `assignto` (parsed) | ⚠ String, not an FK |

---

## 5. Data-quality issues (migration must handle these)

1. ✅ **Denormalization** — `comp_desc`, `task_desc`, `line_no` and the constant `pm_type` are **not built at all** (the transform does not read them).
2. ✅ **`frequency_interval=0` semantics** — built as `integer NOT NULL DEFAULT 0` with `frequency_unit` nullable; `parse_interval` maps blank → 0. Empirically the rows with interval=0 are **exactly** the rows with a blank unit — the two encode the same fact ("non-recurring") and the model keeps them consistent rather than allowing a contradictory combination.
3. ✅ **`assignto` parsing** — split into `assigned_vendor` (the vendor lookup) + `assigned_person` (text). Empirically **every populated value** matches `VENDOR (Person)` with zero exceptions (the remainder are simply blank), so the parser can be strict and fail loudly rather than guessing. Mapping the person to a contact (FK→person) is deferred to the Contacts slice (#6).
4. ✅ **`last_work_order_no` is a soft reference** — built as `bigint` with **no FK constraint** (the WO table exists [0004], but a few percent of values point at purged legacy work orders → no FK; the same reasoning applies to `person`).
5. ⏳ **Many `next_due_date` nulls** — loaded as-is (blank → NULL). Analysing the anomaly "`is_suppressed=false` yet no date" is not a blocker for the read slice; it is handled by the PM-generation slice.

---

## 6. MES pipeline anchors

| Mechanism | Notes |
|---|---|
| **Planned downtime** | A PM work order generated by a ScheduledActivity puts the machine into planned downtime → it too must be synchronized with the MES up/down field (see the MES state coupling in 02-WorkOrders) |
| **Production-schedule coordination** | A PM's `next_due_date` can be fed forward to MES so that production is scheduled around the maintenance window |
| **★ Forward-looking opportunity: meter-based PM ★** | Today every schedule is calendar-based (Months/Days/Weeks). A modern system can add **usage-driven** triggers — PM driven by equipment running hours / counts reported back by MES, matching the `Meter` assets and Meter Readings in 01-Assets. This is a capability legacy eMaint lacks and one worth designing into the new system |

---

## 7. Next: extraction outstanding for this module

> Slice #3 created the empty columns / deferred the corresponding structures. Below is what still needs UI extraction or a later slice to finalize (the read path is not blocked by any of it).

- [ ] `calendar_freq_type` (Shadow / Fixed) — its value domain and next-date rules. Built as an empty text column for now; becomes a lookup once confirmed (S1, tied to S4)
- [ ] `skip_weekends_holidays` — built as a nullable empty column (S2)
- [ ] `consumables` — the structure and quantity columns of the ScheduledActivity↔Inventory consumable relation; the `pm_schedule_consumable` table is deferred (S3, or folded into the Inventory slice)
- [ ] `pm_group` — built as an empty text column
- [x] ~~Whether `standard_hours` is inherited from Task~~ — answered (S5): it belongs to the Scheduled Activity; both `standard_hours` and `estimated_labor_hours` columns exist (2026-06-21)
- [~] The exact `next_due_date` recalculation rules (base date, carry) — **Fixed is live** (completion write-back via `_advance_pm_schedule` + `add_interval`, see §3.1); **Floating** (counting from the actual completion date) awaits the `calendar_freq_type` [UI] value domain before it can be branched (S4, tied to S1)

---

## 8. Target schema (Postgres) — ★ slice #3 is live (migration 0003)

The tables as actually built. The differences from the draft reflect "do not build tables around unconfirmed semantics" (guardrail #8) and missing dependencies:

```
pm_schedule(                                            -- migrations/versions/20260621_0003_pm_schedule.py
  pm_id                  text PRIMARY KEY,              -- pmid
  asset_id               text NOT NULL REFERENCES asset(asset_id),
  task_id                text NOT NULL REFERENCES task(task_no),
  frequency_interval     integer NOT NULL DEFAULT 0,    -- 0 = non-recurring
  frequency_unit         text REFERENCES freq_unit(code),  -- Months/Weeks/Days; null when interval=0
  calendar_freq_type     text,                          -- ★ changed: plain text, no lookup (S1 value domain unconfirmed)
  skip_weekends_holidays boolean,                        -- ★ changed: nullable empty column ([UI], no data — do not assume a default)
  next_due_date          date,
  last_pm_date           date,
  last_work_order_no     bigint,                        -- soft ref → work_order (no FK; some point at purged legacy WOs)
  completion_window_days numeric(4,1),                   -- ★ changed: nullable (defensive; in practice always populated)
  standard_hours         numeric(6,2),                   -- standard (S5: hours belong to the SA)
  estimated_labor_hours  numeric(6,2),                   -- estlabor (differs from standard on a minority of rows, so both are kept)
  assigned_vendor        text REFERENCES vendor(code),   -- parsed from assignto
  assigned_person        text,                           -- ★ changed: text; FK→person deferred to the Contacts slice (#6)
  pm_group               text,                           -- [UI] empty column
  is_suppressed          boolean NOT NULL DEFAULT false,
  -- Audit (AuditMixin / ADR-005/016)
  created_at timestamptz NOT NULL DEFAULT now(), created_by text,
  updated_at timestamptz, updated_by text,
  source_actor text, proposed_by text, confirmed_by text,
  UNIQUE (asset_id, task_id)                             -- the natural unique key (zero duplicates in the export)
)

freq_unit(code, label)                   -- Months / Weeks / Days (seeded from the data)
vendor(code, label)                      -- the maintenance-contractor codes (seeded from the data; shared with the WorkOrder slice)
-- calendar_freq_type lookup: deferred (built once the S1 value domain is confirmed)
-- pm_schedule_consumable(pm_id, item, qty): deferred (no consumables data, §7)
```

> The denormalized columns (`line_no` / `comp_desc` / `task_desc` / `pm_type`) are all dropped per §5.1 and never created.
