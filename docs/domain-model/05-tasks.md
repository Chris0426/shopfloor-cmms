# CMMS Domain Model — 05. Tasks (Maintenance Task Templates)

> Deliverable 1 (Extraction Spec), page 5. Sources: eMaint X4 UI exploration + the `tasks.csv` export + cross-module validation.
> Status: **v1.0 — the Task slice (#2) is live and verified against a real PostgreSQL instance (`alembic check` clean)**.
> Clarifications closed: T2 (`task_no` is opaque), T3 (`is_active` semantics + deferred flagging), S5 (`standard_hours` belongs to ScheduledActivity).
> **Child entities TaskStep + TaskPart (maintenance checklist steps + per-step parts) are live (migration 0016)** — source `data/raw/task_steps_parts.csv`. §8 is the final schema.
> **Update**: Task is no longer just imported reference data — **admins can create/edit it online** (`create_task`: owner-chosen `task_no`, shape `^[A-Za-z0-9_-]{2,20}$`; full CRUD over description / active flag / steps / parts). **"Delete" of a step or part is a soft delete** (migration 0020 `deleted_at` / `deleted_by`; guardrail #4 — the imported eMaint steps are the reconciliation basis for historical PM work, so reads filter them out but the rows are retained for audit; re-attaching the same `(step, item)` revives the existing row).

---

## 0. Module scope

A Task is a **maintenance task template** — it defines "what maintenance is to be performed" for a class of equipment. A ScheduledActivity references a Task and binds it to a specific machine. Opening a Task in eMaint shows the **checklist steps** it defines.

Data shape: a few hundred task definitions, of which **a substantial minority are never referenced by any ScheduledActivity** (idle templates). The import must therefore not assume "a task exists ⇒ a task is in use".

---

## 1. Entity: Task

The Task entity itself is very small — two columns from `tasks.csv` plus a system-level active flag. **Task carries no equipment-type column**; it is a machine-agnostic maintenance template.

| Target column | Source | Type | Required | Notes |
|---|---|---|---|---|
| `task_no` | [CSV] `task_no` | string | ✅ PK | A short code (4–10 chars) that *looks* structured: the prefix roughly abbreviates the machine family and generation, and the suffix roughly abbreviates the maintenance action or the shift it runs on (`DS` / `NS` / `SS` = Day / Night / Swing Shift). **T2 resolved: the regularity is only a habit — an informal abbreviation of `description`, with `description` as the decoder. It is not a grammar, it is not enforced anywhere, and it does not hold across the whole set. Therefore `task_no` is carried as an immutable opaque PK and is never parsed or decomposed into structured columns** (guardrail #8: do not build a parser on top of a naming convention) |
| `description` | [CSV] `task_desc` | string | ✅ | Task name; English, and unique across the whole set. This is the semantic carrier (the human-readable expansion of `task_no`) — everything that wants meaning reads this, not the code |
| `is_active` | system flag | bool | ✅ (default true) | **T3 resolved: false = retired** (the owning equipment was decommissioned, or the task was verified as unnecessary). This slice loads everything as true; flagging "never referenced by a ScheduledActivity" as false requires a join to SA and was **deferred to the SA slice (#3)**, where the relationship is live and the flag can be maintained empirically |

> **`standard_hours` does not belong on Task.** S5 resolved: it represents "labour hours required to perform this task" and belongs to **ScheduledActivity** (per EID + `task_no`). It is modelled and populated by the SA slice (#3); Task has no such data and we do not pre-create a default.

> **The relationship between Task and equipment sub-type is *derived*, not a column.** Task hangs off no asset and no sub-type. Only when a ScheduledActivity binds a Task to an Asset does the relationship "this task belongs to that asset's sub-type" emerge. The observation that **almost every in-use task in fact maps to exactly one sub-type** is a **data-level regularity** (in practice a task is habitually used on one machine family), but it **must not be modelled as an FK on Task** — it is a transitive result of Task → ScheduledActivity → Asset → sub-type, and the handful of counterexamples are enough to break any model that hard-codes it.

---

## 2. Child entity: TaskStep (maintenance checklist)

TaskStep is a 1:N child of Task: the checklist of steps a task defines. Structured checklist steps are high-value in the modernised system — they become an **LLM-readable, agent-verifiable checklist**, letting an agent know what a PM work order actually requires. That is a concrete anchor for LLM governance.

The final schema is in §8 (`task_step` + `task_part`, migration 0016).

---

## 3. Operations

CRUD; maintain the checklist. Task is a reference master; change frequency is low.

---

## 4. Relationships

| Relationship | Target | Cardinality | Join key | Verification |
|---|---|---|---|---|
| Referenced by schedules | ScheduledActivity | 1 : N | `task_no` | ✅ 0 orphans; only a subset of tasks is in use |
| Checklist steps | TaskStep | 1 : N | `task_no` | ✅ live (migration 0016) |
| Step parts | TaskPart | 1 : N | `task_step_id` | ✅ live (migration 0016) |

> Task has **no direct relationship** to Asset or equipment sub-type. The link is transitive: `Task ← ScheduledActivity → Asset → sub-type`. Answering "which machine families use this task?" requires a join through ScheduledActivity; it is not a Task column.

---

## 5. Data quality findings

1. ✅ **`task_no` encoding** (T2) — the prefix/suffix have structure, but they are an informal abbreviation of `description` (which is the decoder), not a parseable grammar. **Decision: no structured columns, no parser** (guardrail #8). `task_no` is an immutable opaque PK; semantics live in `description`.
2. ✅ **Idle tasks** (T3) — idle = retired (equipment decommissioned / task verified as unnecessary). **Decision: load all of them (`is_active` defaults to true), delete nothing**; flagging idle rows `is_active=false` requires a ScheduledActivity join and was **deferred to the SA slice (#3)**. Rationale: the flag can only be set correctly — and kept correct — with the SA relationship in place; computing it inside the Task slice would bake in a static snapshot that goes stale.

---

## 6. MES pipeline anchor

Task is reference data and plays a minor role in the MES ↔ CMMS work-order pipeline. The one forward-looking hook: **structured TaskSteps** let the pipeline or an agent verify that checklist steps were completed when a PM work order is opened or closed automatically.

---

## 7. Follow-ups

- [x] ~~TaskStep checklist~~ — delivered (migration 0016)
- [x] ~~`task_no` encoding rules~~ — T2: opaque PK, not parsed
- [x] ~~Does `standard_hours` exist at Task level~~ — S5: belongs to ScheduledActivity, not modelled on Task
- [x] ~~`is_active` flag for idle tasks~~ — T3: default true; false-flagging deferred to the SA slice

---

## 8. Normalized target schema (PostgreSQL)

```
task(                                   -- live (migrations/versions/0002_tasks.py)
  task_no        text PRIMARY KEY,       -- opaque (T2); immutable
  description    text NOT NULL,
  is_active      boolean NOT NULL DEFAULT true,   -- T3: false = retired; false-flagging deferred to the SA slice
  -- standard_hours is NOT here: it belongs to ScheduledActivity (S5)
  -- audit columns (AuditMixin / ADR-005/016):
  created_at timestamptz NOT NULL DEFAULT now(), created_by text,
  updated_at timestamptz, updated_by text,
  source_actor text, proposed_by text, confirmed_by text
)

-- live (migration 0016; source = data/raw/task_steps_parts.csv)
task_step(                              -- one CSV row = one step (rows whose task is absent from the master are skipped + counted)
  id             bigserial PRIMARY KEY, -- synthetic stable identity (NOT (task_no, proc_seq) — that pair can repeat)
  task_no        text NOT NULL REFERENCES task(task_no),
  proc_seq       integer,               -- original eMaint sequence number (nullable / NON-UNIQUE — several steps can share one seq); ordering + provenance only
  task_desc      text NOT NULL,         -- step instruction
  idempotency_key text UNIQUE,          -- taskstep:v1:<task_no>:<occurrence> (re-run idempotency)
  + audit
)
-- Sequence-number decision: do NOT renumber eMaint's 10/20/30 (preserve faithfully) and use a synthetic id for identity
--   → the sequence number is no longer identity; inserts/reordering are handled by the system (the gap convention no longer
--     carries meaning). The UI sorts by (proc_seq, id) and renders an enumerated 1..N.

task_part(                              -- step 1:N part (a step may consume several parts; normalized up-front)
  id             bigserial PRIMARY KEY, -- in the legacy data every step happens to have ≤1 part, but the model supports many
  task_step_id   bigint NOT NULL REFERENCES task_step(id),
  item_code      text NOT NULL REFERENCES inventory_item(item_code),  -- unmatched codes are skipped + counted by the loader (ADR-018)
  replace_qty    numeric(12,3),         -- replacement quantity; nullable (never counted during cataloguing → leave as-is, the owner fills it in later)
  + audit,
  UNIQUE (task_step_id, item_code)      -- one row per (step, item) (idempotent)
)
```
