# CMMS Domain Model — 02. Work Orders

> Extraction Spec, page 2. Sources: eMaint X4 UI exploration + the `work_orders.csv` export (a decade of history, tens of thousands of rows) + cross-module validation.
> Status: **v2.0 — the #4a read slice and the #4b-1 write engine are both live and fully verified end-to-end against real PostgreSQL** (migration 0007, `alembic check` clean, 0 FK orphans).
> **#4b-1**: canonical state machine (7 states + `work_order_status_history`), governed `open` / `start` / `hold` / `resume` / `complete` / `close` / `void` / `cancel` ops, the **downtime calculation engine** (`ProductionCalendar`: production hours only; test runs not counted, waiting-for-parts counted), **part issue linked to stock deduction** (`work_order_part` + `stock_transaction`, ADR-005), and **`miscreated=T` rows dropped entirely** (a small fraction of the corpus, never imported). Verified against real PostgreSQL: historical downtime estimated, a live lifecycle computing downtime exactly, part issue moving `on_hand` with idempotency.
> **Rulings**: `miscreated` = mis-created, drop (W1); downtime = stoppage during production hours only (W6); part issue deducts stock; IN_PROGRESS / ON_HOLD intermediate states are real; future work-order timestamps are captured by the system (never typed in by hand). The **production-schedule source** (which hours of the day count as production) is configuration, not a hard-coded constant, and is to be detailed later.
> **#4b-2, live**: **ADR-016 two-phase** propose/confirm + `pending_proposal` (MCP `propose_open/close_work_order` + `confirm_work_order_proposal`; confirm rejects anonymous callers and agents), and **ADR-017 on-box Profile B** (`onbox.py` JWS verification + the three columns `origin_station` / `idempotency_key` / `evidence_ref` + `open_reactive_work_order_onbox` / `cancel_reactive_report_onbox` + the `/work-orders/on-box/*` API). Verified against real PostgreSQL: migration 0008, `alembic check` clean, propose/confirm audit trail, on-box JWS open / idempotent / unknown-EID-reject / bad-signature-reject / soft-cancel.
> **★ Still outstanding**: W4 `work_type` reclassification (`ON HALT` / `DELAY`), W5 close-form field collection, MES up/down coupling (ADR-011).
> **★ Live (migration 0012)**: the work log `work_order_note` (append-only, per-entry timestamp, verbatim) — this supplies the missing narrative for long-downtime work orders that get **updated repeatedly, at several points in time** (previously there was only the initial `brief_description`, the closing `action_taken` and `work_order_status_history` — **no narrative log**). Manual entries and natural-language entries dictated to the assistant land in the same table (`source_actor` distinguishes human from agent). Jira MRQ mapping = a synthesized title/summary + one comment per note (see ADR-020 decision 7). Details in §1.6 / §8.

---

## 0. Where this module sits

A work order records every intervention on a piece of equipment. Three origins:

1. **PM work orders** — generated automatically when a Scheduled Activity reaches its `pmnextdate` (`wo_type=PM`).
2. **Repair work orders** — raised by line staff when equipment fails (`wo_type=REACTIVE`).
3. **Other work orders** — engineering experiments, process-parameter changes, product changeovers, part requests, etc., raised by engineers (`CORRECTIVE / PROACTIVE / CONVERSION / PART REQUEST`).

PM and REACTIVE together dominate the corpus almost entirely; the other types are a long tail.

Every work order must be closed on completion, at which point the system settles downtime. **This is the core write target of the MES pipeline**: MES detects a yield or equipment anomaly → a REACTIVE work order is opened automatically.

Data shape: a decade of history. **WO volume is highly skewed** — the median maintained asset has only a handful of work orders over its whole life, while a small number of problem machines account for a disproportionate share of the corpus. Anything that assumes a uniform distribution (pagination defaults, per-asset UI, downtime aggregates) must be designed for that skew.

---

## 1. Entity: WorkOrder

**[CSV]** = present in the `work_orders.csv` export; **[UI]** = only visible on the eMaint form, not exported (needs extraction); **[DROP]** = denormalized copy, to be removed and derived from the related entity instead.

### 1.1 Identity and relationships

| Target field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `work_order_no` | [CSV] `wo` | integer | ✅ PK | Global sequence number, monotonically increasing (**not** auto-increment — the legacy numbers are imported as-is) |
| `asset_id` | [CSV] `compid` | string | ✅ FK | →`Asset.asset_id`; ✅ 0 orphans |
| `comp_desc` | [CSV] | — | [DROP] | Denormalized asset name; derive from Asset |
| `assetsubtp` | [CSV] | — | [DROP] | Denormalized subtype; derive from Asset |
| `pm_source_id` | [UI] | string | ⬜ FK | →`ScheduledActivity.pmid`; PM work orders only. **Reverse relation**: `scheduled_activity.lastpmno → work_order_no` verified at 98% hit rate |

### 1.2 Classification and status

| Target field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `work_type` | [CSV] `wo_type` | enum | ✅ | 8 values: `REACTIVE` `PM` `ON HALT` `PART REQUEST` `CONVERSION` `PROACTIVE` `CORRECTIVE` `DELAY` (the last two are vanishingly rare). **⚠ The taxonomy is muddled** (see §5.4) |
| `status` | [CSV] `workstatus` | enum | ✅ | `H` = History (closed) / `O` = Open. Almost everything in the export is closed. **⚠ The export has only 2 states; the real lifecycle is richer** (see §3) |
| `is_voided` | [CSV] `miscreated` | bool | ✅ | Appears to be a "mis-created / voided" flag; set on a small fraction of rows, and overwhelmingly on REACTIVE ones (consistent with an operator mis-reporting a fault). **⚠ TODO confirm semantics** |
| `priority` | [UI] | enum | ⬜ | eMaint normally has a priority field; not in the export |

### 1.3 Work content

| Target field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `brief_description` | [CSV] `brief_desc` | text | ⬜ (98%) | **⚠ HTML-entity-encoded CJK** — the export writes CJK as numeric character references (`&#NNNNN;…`), so ingestion must call `html.unescape()` or the database ends up holding entity soup. On PM work orders this is the task name (English); on REACTIVE it is the line operator's fault description (Chinese) |
| `diagnosis` | [CSV] `diag` | text | ⬜ (14%) | Fault cause / diagnosis; **also HTML-entity-encoded**. Mostly on REACTIVE (27% populated); nearly empty on the other types |
| `action_taken` | [UI] | text | ⬜ | What was actually done — not exported, needs extraction |
| `confirmed_reason_code` | [UI] | string (FK→`equipment_failure_code.code`) | ⬜ | **The human-confirmed root cause (D6; migration 0027, live)**. **Single axis: efc** (the 107-code vocabulary, see doc 08) — it **never mints a canonical code and never touches the mfc axis**. **Only meaningful on REACTIVE** (a fault found during a PM should get its own repair work order); `null` = not confirmed (**≠ no fault**). Optionally set when finishing (`finish` / `close`), or afterwards via `set_confirmed_reason` (fill in / correct / clear); **once CLOSED, only an admin may correct it** (terminal-state freeze, same as notes and brief). Retired codes (`is_active=false`) can no longer be chosen as a new root cause; historical references are unaffected. The outbound contract `contract_wo_detail.v1` exposes this field additively (no schema version bump). |
| `external_ref` | [CSV] `comments` | string | ⬜ (5%) | Values of the form `MRQ-<n>` or a bare number — apparently an external maintenance-request number. **⚠ TODO confirm semantics** |

> **★ `brief_description` = the initial fault as reported at open time (essentially never edited).** Progress / diagnosis / waiting-for-parts updates over the life of the work order are **appended to `work_order_note` (§1.6)** — they never overwrite this field and are never squashed into a single blob. The closing summary goes in `action_taken` (which may be summarized from the log).

### 1.4 Timeline and hours

| Target field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `opened_date` | [CSV] `date_wo` | date | ✅ | Date the WO was opened (the export spans about a decade) |
| `scheduled_date` | [CSV] `sch_date` | date | ⬜ (47%) | Planned execution date; **only meaningful on PM work orders** (47% populated ≈ the PM share) |
| `work_start_time` | [CSV] `time` | time | ⬜ | **⚠ 12-hour clock with no AM/PM** — unreliable (see §5.2) |
| `work_complete_time` | [CSV] `time_cmpl` | time | ⬜ | **⚠ Same 12-hour ambiguity** |
| `closed_date` | [CSV] `editdate` | date | ⬜ | Close date. ⚠ This is *not* "last modified" — it is the **close sign-off date** (only populated when status=H) |
| `closed_time` | [CSV] `edittime` | time | ⬜ | Close time (24-hour, reliable) |
| `closed_by` | [CSV] `edituser` | string→FK | ⬜ | Who closed it — a legacy eMaint login name, from a small set of accounts. Loaded as text; the FK→user is deferred |
| `downtime` | [UI] | duration | ⬜ | **Equipment downtime — the system settles this value, but it is not in the export and must be extracted** (see §5.2) |
| `labor_hours` | [UI] | decimal | ⬜ | Actual labour hours; not exported |
| `cost` | [UI] | decimal | ⬜ | Repair cost; not exported |

### 1.5 Assignment

| Target field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `assigned_vendor` | [CSV] `assignto` (parsed) | enum | ⬜ (92%) | The maintenance contractor, as a code in the `vendor` lookup: `CMA` / `CMB` (two maintenance contractors, one of them historical — its legacy rows must still resolve, so the lookup row is kept rather than deleted) and `SF` (in-house shopfloor) |
| `assigned_person` | [CSV] `assignto` (parsed) | string→FK | ⬜ | String format `VENDOR (Person Name)`; must be split into vendor + person. **Not a clean FK — a person mapping is required** |
| `opened_by` | [UI] | string→FK | ⬜ | Who opened it (line staff for REACTIVE, the system for PM); not exported |

> **★ Multiple assignees `work_order_assignee` (0031)** — work-order assignees moved to the cross table `work_order_assignee(work_order_no, person_name, position)` (composite PK; `position` orders the list). `work_order.assigned_person` is **retained** as the denormalized "first assignee" (for the downstream `assigned_person` contract, 21k rows of history, and display compatibility) and is maintained by the domain layer (`_replace_assignees` keeps them in sync). Service: `get_assignees` / `assignees_map` / `set_assignees` (whole-set replace, superseding the single-value `set_assignee`, which is kept as a delegating shim). The "My work orders" filter matches **any** assignee in `work_order_assignee` (EXISTS).
>
> **★ Assignee derivation order (0031)** — when a REACTIVE work order is opened (`_open_impl`) with no explicit assignee, assignees are derived from the **asset owner list** (`asset_owner`, all of them — see `01-assets.md`) → written to the cross table, with `assigned_person` = the first. `open_work_order` accepts `assignees: list[str]` (which takes precedence over the single-value `assigned_person`). The domain layer **does not hard-reject an empty list** (on-box Profile B auto-open must keep working, and an asset with no owner must still be able to raise a work order); the human-facing required check lives in the web report form (empty + asset has no owner → friendly error, form redisplayed). For PM generation's precedence order, see `03-scheduled-activity.md` §1.5.
>
> **★ Downstream read contract (0031, additive)** — `contract_wo_detail.v1` gains `assignees: list[str]` (all assignees, in `position` order; the first == `assigned_person`). The schema_id stays at **v1** (purely additive — consumers must not reject on an allow-list); `/work-orders/active-in` is unchanged in shape.

### 1.6 The work log (`work_order_note`) ★ live 2026-07-01 (migration 0012; service `add_note` / `list_notes`; web detail timeline + "add an update")

A work order is a **timeline**, not a handful of static fields. On long-downtime cases (repaired over several days, days spent waiting for parts) engineers update it **repeatedly, at different points in time**; those updates must be kept **verbatim** (each with its own timestamp and author) and must never be overwritten into a single `brief_description`. Hence an append-only work log:

| Target field | Type | Required | Notes |
|---|---|---|---|
| `id` | bigint | ✅ PK | |
| `work_order_no` | bigint | ✅ FK | →`work_order` |
| `entry_type` | enum | ✅ | Controlled: `progress` / `diagnosis` / `hold` (transition to waiting + reason) / `resume` / `part` (part-issue note) / `note` (general) / `report` (the report at open time) / **`ai_candidate`** (produced by an AI agent, see below) |
| `body` | text | ✅ | The text of that update (**verbatim**; `html.unescape` applied) |
| `author` | text | ✅ | `human:<id>` or `agent:<name>` (i.e. `source_actor`) |
| `occurred_at` | timestamptz | ✅ | **When the update happened** (not `created_at`) |
| `status_history_id` | bigint | ⬜ FK | If the note accompanied a state transition, links to `work_order_status_history` |

**Four fields, four jobs — do not conflate them**: ① `brief_description` = the initial symptom (at open time) ② **`work_order_note` = the running narrative log (this section; appended over the life of the WO)** ③ `work_order_status_history` = state transitions (the source of downtime) ④ `action_taken` = the closing summary (may be summarized from the log; the raw notes remain the system of record).

**Write path**: manual entry (the engineer's "add an update" on the WO detail page) and Hermes natural-language input **both append a row to this table** (single write path, guardrail #1), with `source_actor` recording human vs agent → giving us "whether typed by hand or dictated in natural language, the entries stay verbatim, one per point in time". This is a low-risk additive operation and does not go through gated write.

> **★ 2026-07-04 (migration 0021): the `ai_candidate` note type.** Repair/diagnosis **suggestions** produced by AI agents (the Hermes gateway and similar) land in this table with `entry_type='ai_candidate'` and `source_actor=agent:<name>`; the UI timeline renders an "AI candidate (unconfirmed)" badge (indigo, `notetype.ai_candidate` i18n). **A candidate is never fed back as confirmed** — it is a narrative the agent proposed, and its mere existence must not make it a reviewed fact (governance; see the ADR-027 agent constitution). **Supporting evidence** uses a "standard prefix line" convention inside the note body (v1): `evidence: <ref>` — a plain-text convention; v1 adds **no column and does no parsing**.

> **★ 2026-07-03 revision: the log is correctable.** If something (text or a photo) was entered wrongly it must be fixable — same as an editable Jira comment. `WorkOrderService.update_note` updates `body` in place, **restricted to the author (an admin may correct on their behalf)**; `updated_at` / `updated_by` (AuditMixin) *are* the "edited" audit marker, and the UI shows "(edited)"; photos can be added or soft-deleted (`soft_delete_attachment`, the R2 object is retained). **No full revision history is kept** (a cost/benefit ruling — who changed what and when is already auditable). The sync anchors are unchanged: `id` / `idempotency_key` do not move, so the gateway can later align by *updating* the existing comment rather than adding a new one. "Append-only" therefore narrows to: **each entry is kept distinct — never merged, never overwritten into one field** — rather than "not one character may change".
> **★ 2026-07-04 hardening (code review)**: ① **Terminal-state freeze** — once a work order is CLOSED / CANCELLED / VOIDED, **even the author may no longer edit** its notes (they are the evidence behind an already-settled downtime figure, which downstream consumers read); only an admin may correct them. ② Admin identity is **resolved in the domain layer** (`user_account.role`); the caller-asserted `as_admin` flag is abolished. ③ Corrections carry an ownership guard (the note must belong to that work order).
> **★ 2026-07-05 revision: the log is deletable.** A wholly mistaken entry must be removable. `WorkOrderService.delete_note` performs a **soft delete** (migration 0022: `work_order_note += deleted_at / deleted_by`; guardrail #4 — who deleted what and when stays auditable); `list_notes` excludes deleted rows and the operation is idempotent (already-deleted = no-op). Permissions mirror `update_note`: **author or admin only**, **admin only once the WO is terminal** (terminal-state freeze), plus the ownership guard. Photo attachments are **not touched** (the "R2 keeps everything" policy); they simply disappear from the timeline along with the note.

**Jira MRQ mapping (see ADR-020 decision 7)**: the MRQ title and summary are synthesized by Hermes from **everything currently on the work order** (editable / regenerable); the MRQ **comments** are a **1:1 verbatim** mapping of every note in this table (timestamps and authors preserved; nothing rewritten or merged) → **the MRQ reads exactly like the cmms work order**, and both sides are append-only (one more note on the WO → one more comment appended to the existing MRQ, ADR-020 `link_type=appended`).

---

## 2. Operations (CRUD + actions)

Almost every work-order operation in a CMMS is a **state transition**, not free-field editing: open, assign, start work, put on hold, complete, close, void. Plus: auto-generate from a PM, open directly from an Asset, issue parts (Part Request), print, settle downtime.

> Modernization notes: each state transition is a **governed domain operation** (`open_work_order`, `close_work_order`, `void_work_order`…), never a naked UPDATE. `downtime` is computed and written by the domain service on `close`; it is not freely editable. Voiding and bulk deletion are high risk and are not exposed to agents.

> **★ 2026-07-05: UI lifecycle simplification — three domain convenience methods (the canonical state machine is unchanged).** The ruling: you only raise a repair when the machine is already broken, so downtime starts at open time and a "Start" button is meaningless; complete-then-close is one layer too many; switching to a waiting state must be one click. The UI collapses to "a row of state chips + a single Finish button", and the domain gains three **composite convenience methods** (each merely chains existing canonical transitions **inside one transaction**; `status_history` faithfully records every step and the **outbound contract shape does not change**):
> - **`resume_or_start`** (the "In progress" chip): OPEN→start / ON_HOLD·COMPLETED→resume; already IN_PROGRESS = idempotent no-op.
> - **`set_hold`** (the waiting chips): one click from any active state — OPEN implicitly starts first; ON_HOLD with a different reason = resume+hold as two atomic transitions (the intermediate state is instantaneous, zero-length, so downtime is unaffected); `note_body` produces a hold note linked to the final transition. UI: "Other" requires a sentence of explanation (enforced in the route); chips for reasons that do not count as downtime (TEST_RUN / WAITING_MACHINE_TIME) are labelled "not counted as downtime".
> - **`finish_work_order`** (the "Finish" button): `action_taken` **required** + `labor_hours` optional (help text: actual labour hours, not downtime duration) → COMPLETED→CLOSED in one transaction (it can start from OPEN / IN_PROGRESS / ON_HOLD / COMPLETED; OPEN implicitly starts), reusing every guard in complete/close (bad labour hours validated before the transaction, PM close writes back `next_due_date`, downtime settled and locked).
>
> The separate `start_work` / `hold_work` / `resume_work` / `complete_work` / `close_work_order` ops are **all retained** (the MCP/API surface does not change). **Cancellation is narrowed on the engineer's surface**: the detail page keeps only "Cancel work order" (OPEN only, reason required); the "Request void" button is removed (the void propose/confirm domain ops and the admin surface are fully retained — voiding is the admin's tool for correcting a wrongly-raised WO). The "Mine" list defaults to **active** work orders (OPEN / IN_PROGRESS / ON_HOLD), with separate groups for "Finished" (COMPLETED / CLOSED) and "Cancelled" (CANCELLED / VOIDED).

---

## 3. Workflow / state machine ★ the heart of this module ★

**The export distinguishes only `O` (open) and `H` (closed)**, but the population patterns of the other fields let us reconstruct the real lifecycle:

- On open: `opened_date` + `work_start_time` populated; `work_complete_time` and all `closed_*` empty.
- On close: `work_complete_time` + `closed_date` / `closed_time` / `closed_by` filled in, downtime settled.

The proposed **modernized target state machine** (richer than the legacy O/H; to be corrected against the UI extraction in §7):

```
              ┌──────────────────────────────────┐
              ▼                                  │
  DRAFT ──▶ OPEN ──▶ IN_PROGRESS ──▶ COMPLETED ──▶ CLOSED
              │           │  ▲
              │           ▼  │
              │        ON_HOLD
              │
              └──────────────▶ VOIDED  (mis-created / voided)
```

- Legacy `workstatus=O` corresponds to somewhere between `OPEN` and `COMPLETED`; `H` corresponds to `CLOSED`.
- In the legacy `wo_type`, **`ON HALT` and `DELAY` look like a *status/reason* misfiled as a *work-order type***. The new model should move them out of `work_type` into a status (`ON_HOLD`) or a hold reason. To be verified against the UI (§7).
- Transition rules (first cut): nothing may transition out of `CLOSED`; `VOIDED` is a terminal side exit; `downtime` is locked on entry to `CLOSED`.
- **Transition-carried log entries (★ 2026-07-01)**: on entry to `ON_HOLD` (waiting for parts / vendor) or on resume/progress, the domain service can simultaneously append a `work_order_note` (§1.6) recording the reason or progress, cross-referenced with `work_order_status_history` → the timeline interleaves "state change + narrative" by `occurred_at`.

### 3.1 ★ MES state coupling (architectural requirement, pending a pipeline ADR) ★

The WorkOrder state machine **is not one system's private state**. MES has absolute control over the physical machine (a machine must first raise a production request to MES and receive a response before it can run), and MES holds a field controlling each machine's up/down production status. Therefore:

- The WorkOrder state and the MES up/down field form **one distributed state that must be kept consistent**.
- Machine enters repair/maintenance (WO `OPEN`~`IN_PROGRESS`) ↔ MES should be `down`; WO `CLOSED` ↔ MES may return to `up`.
- **Directionality is the question the ADR must decide**: is MES the source of truth (CMMS follows), does CMMS drive MES, or is it a two-way negotiation?
- The coupling applies **equally to PM work orders** (planned downtime), not only REACTIVE ones.

At the pipeline-ADR stage we will need from the MES side: the exact table/column controlling up/down, its value domain, who may write it, whether the MES MCP can read/write it, the transition semantics (is it a real-time interlock?), and the mapping from equipment id to `Asset.EID`. **None of this is needed for the current deliverable.**

### 3.2 ★ Two-phase externally-confirmed open/close (ADR-016) ★

A downstream analytics consumer's follow-up loop has an agent **propose** opening/closing a work order, but **the human confirmation happens in an external chat channel**, not in the cmms UI — the proposer and the confirmer are different identities. So beyond "dry-run + confirm within one session", `open_work_order` / `close_work_order` also support an asynchronous two-phase flow:

- **propose** (an agent identity) → the domain service does not execute immediately; it persists a pending proposal (intent + dry-run diff + idempotency key + expiry) → returns a pending token.
- **confirm** (the confirmer's identity is authenticated by the channel) → after validation (valid / not expired / not already used / authorized) it executes through the single write path.
- Constraints: a token is **not** an authorization (authorization comes from the confirmer's identity; **anonymous is rejected**); **every confirm requires a currently active admin** (opening/closing work orders are no longer exempt — now that `/mcp` is on the public internet, a holder of a valid scoped token must not be able to self-confirm; enforced in the domain layer, aligned with ADR-016/027, the agent constitution). The confirmer's identity is **bound to the `/mcp` token** (a self-declared `confirmer` parameter is not accepted). **Agent authority is not widened** (high-risk operations such as void or Key Change remain outside what an agent may even propose); idempotency follows ADR-006.
- The audit trail records both `proposed_by` and `confirmed_by`; the final `source_actor` is the confirmer (see the §8 schema / ADR-005).

### 3.3 ★ Open / close notifications (Slice B; migration 0030 + Slice D watch lists, 0032) ★

Work-order **open** (REACTIVE reports + PM generation) and **close** notify the relevant people (downtime is time-critical). Mid-life note updates do **not** notify.

- **Recipient resolution**: the `notify_recipient` vocabulary (maintained by an admin at `/admin/notify`, **deliberately not bound to `user_account`** — a line supervisor with no login still needs to be notified). The recipients of a given event on a given work order are every row that is `is_active` **and** matches on at least one of three arms:
  - **Broadcast**: the row's `notify_on_open` / `notify_on_close` flag is set (engineering team / line-supervisor group).
  - **Direct**: `assignee_name` exactly equals **any** assignee of that work order (0031: matched against all of `work_order_assignee`, not just the denormalized first one; this is the asset owner's personal notification).
  - **Watch** (Slice D; migration 0032): `notify_watch` has a row whose `assignee_name` exactly equals any assignee of that work order (`EXISTS(notify_watch WHERE recipient_id = notify_recipient.id AND assignee_name IN <work-order assignees>)`). One person can watch several assignees (a supervisor covering a line, a colleague covering a shift); a watch fires unconditionally **on both open AND close**.
  - **Exactly one message per person per event**: all three arms can hit the same person at once; one row per person in `notify_recipient` plus the outbox unique key `(work_order_no, event, channel, recipient_id)` guarantee de-duplication (direct + watch + multiple owners all collapse to a single row).
- **enqueue**: `_open_impl` (covering web reports / MCP confirm / on-box / PM on-demand + the scheduler) and `_transition` (target state CLOSED) call `enqueue_work_order_notifications` **inside the same transaction**, queueing one `notification_outbox` row per recipient × per non-empty channel (email / telegram) — via the single write path. Zero recipients → zero rows (no noise).
- **Channels**: email (reusing `EmailSender` / SMTP) + Telegram (a bot; a single group chat_id; `TelegramSender`).
- **Outbox and idempotency**: the unique key `(work_order_no, event, channel, recipient_id)` → `on_conflict_do_nothing`. **Corollary**: reopening and re-closing the same work order does **not** re-send (the `(wo, closed, …)` row already exists) — accepted as reasonable semantics. Flush (a web background task / the CLI `notify-flush-outbox` / the tail of `pm-generate-due`) processes rows independently and records failures honestly (`status` / `attempts` ≤ 5 / `last_error`); **an unconfigured channel → the row is skipped entirely (left pending, burning no attempts)** and is sent once the secret is configured.
- **Body**: a fixed Traditional Chinese template (`notify/render.py`; deliberately not localized — the audience is asset owners, the engineering team and line supervisors), purely informational with no nagging language; times are rendered in Taipei time; a report shows the **account that opened it** (the shared iPad account on the floor, not a named person). **Zero impact on the outbound JSON read contracts** (this is a purely additive notification surface, not consumed downstream).

---

## 4. Relationships to other entities

| Relationship | Target | Cardinality | Join key | Validation |
|---|---|---|---|---|
| Asset | Asset | N : 1 | `compid` | ✅ 0 orphans |
| PM source | ScheduledActivity | N : 1 | `pmid` / `lastpmno` | ✅ `sa.lastpmno→wo` 98% hit |
| Assigned person | Contact / Person | N : 1 | `assignto` (parsed) | ⚠ String, not an FK |
| Parts issued | Inventory | N : M | (PART REQUEST work orders) | Usage detail still to be extracted (§7) |
| Closed by | User | N : 1 | `edituser` | 15 accounts |

---

## 5. Data-quality issues (migration / ingestion must handle these)

1. ✅ **HTML-entity encoding** — `brief_desc` / `diag` are restored via `unescape_text()` (`html.unescape`). Verified against real PostgreSQL: the DB holds true CJK, not `&#` entities.
2. 🔶 **★ 12-hour ambiguity + downtime settlement (#4b-1, live) ★** — `time` / `time_cmpl` have no AM/PM (`parse_time` is best-effort). The **downtime engine**: history is estimated once from open (`opened_date` + the unreliable `work_start_time`) to close (`closed_date` + the reliable `closed_time`) and flagged `downtime_estimated=True`; future work orders are computed exactly from the down segments of `work_order_status_history` (the system captures `timestamptz` automatically, `downtime_estimated=False`). Both count **production hours only** (`ProductionCalendar`, which excludes the plant's non-production window — that window is configuration, and the authoritative production-schedule source is still to be wired in). Down determination: `OPEN` / `IN_PROGRESS` = down; `ON_HOLD` depends on `hold_reason` (`TEST_RUN` / `WAITING_MACHINE_TIME` = machine still running, not counted; `WAITING_PARTS` / `WAITING_VENDOR` / other = counted); after `COMPLETED`, not counted. **★ 2026-07-03: delay explanations are auditable** — hold/resume can carry explanatory text → in the same transaction a `hold` / `resume` note is written and linked to that transition via `status_history_id`; and `WAITING_MACHINE_TIME` was added (waiting for a slot to pull the machine down — it is still producing; migration 0019 seeds it into prod). A downstream consumer watching downtime therefore sees, right on the timeline, a plausible explanation: waiting for parts / waiting for the vendor / waiting for a machine slot / observing a test run.
3. ✅ **`editdate` / `edittime` / `edituser` renamed** — now `closed_date` / `closed_time` / `closed_by`; the real audit columns `updated_at` / `updated_by` come from AuditMixin.
4. ⏳ **Muddled `work_type` taxonomy** — **handling in this slice**: load all 8 values as-is into the `work_type` lookup (including `ON HALT` / `DELAY`); reclassification (moving them out of `work_type` into status/reason) is deferred to #4b (W4; guardrail #8 — do not guess semantics).
5. ✅ **Denormalization** — `comp_desc` and `assetsubtp` are not created (the transform does not read them).
6. ✅ **`assignto` parsing** — split into `assigned_vendor` (the vendor lookup: `CMA` / `CMB` / `SF`) + `assigned_person` (text). Empirically the overwhelming majority of populated rows match the `VENDOR (Person)` format; a small remainder are empty, and a couple are a bare name with no vendor (→ person-only). The parser must tolerate all three rather than assume the happy path. The person FK is deferred to the Contacts slice.
7. ✅ **Empty `brief_desc`** — `clean()` treats the empty string and `nan` as NULL (a couple of percent of rows).
8. ✅ **★ Historical part-issue backfill (`part_issues.csv`; `part_issue_backfill`) ★** — `backfill_part_issue` reconstructs `work_order_part` + `stock_transaction(ISSUE)`. Key points: ① `occurred_at` = `date_wo` (the source is DATE-precision only → Taipei 00:00); ② `stock_transaction.reason` = `descrip` after `html.unescape` (provenance; `&rdquo;` → ”); ③ **`on_hand` is NOT touched** (the eMaint on-hand snapshot already reflects these historical deductions; deducting again would double-count) — this is the **inverse** of the ADR-005 semantics (a ledger entry with no `on_hand` movement), with the **side effect** that `sum(qty_delta) ≠ on_hand`, so **never reconstruct `on_hand` from the ledger**; ④ the backfill is allowed to attach to work orders in the **CLOSED terminal state** (whereas the governed `issue_part_to_work_order` blocks terminal states); ⑤ FK attribution (★ ADR-024, from migration 0015) has two axes: **if the WO does not exist but the row's `compid` is a valid asset → rescue by attaching to the asset** (`charge_target_asset_id`, `INSERTED_ASSET`, no `work_order_part` row); if the WO does not exist and `compid` is invalid or missing → `MISSING_WORK_ORDER` (unrescuable); **an item not in the inventory master is always judged `MISSING_ITEM` first** (following ADR-018: never mint phantom ids) → all of these are logged and skipped without aborting the run; ⑥ denormalized columns (`unitcost` / `extcost` / `comp_desc` / `assetsubtp` / `vpartno` / `category`) are dropped; ⑦ duplicate `(wo, item)` rows each become their own record via an occurrence-based `idempotency_key` (`partissue:v1:<wo>:<item>:<n>`), so re-runs are idempotent. The CSV encoding is **cp1252**.

---

## 6. MES pipeline anchors ★ this module's core contribution to the pipeline ★

Work Order is the pipeline's **write end**. Key design points:

| Mechanism | Notes |
|---|---|
| **Auto-open** | MES detects a yield anomaly / equipment fault signal → a `REACTIVE` work order is opened automatically through the CMMS domain service |
| **Idempotency** | MES-triggered opens must carry an **idempotency key** (= the MES event id); agent / pipeline retries must not open duplicates |
| **Downtime reconciliation** | The CMMS `downtime` and the stoppage windows in the MES equipment-state log should cross-reconcile, jointly supporting OEE / availability |
| **Timeline alignment** | `opened_date` / `closed_date` ↔ the MES production-interruption windows |
| **Join key** | `asset_id` (EID) — maps to the MES equipment id (see 01-Assets §6) |
| **Governance** | The write tools are domain operations such as `open_work_order` and `close_work_order`, **never raw SQL/UPDATE**; agent writes must record `source_actor` and a reason |

> Candidate first pipeline write use case: **an MES equipment-anomaly event → automatically open and assign a REACTIVE work order** (reads via the existing MES MCP, writes via the new CMMS domain service).

---

## 7. Next: extraction outstanding / the #4b write slice

> The read slice (#4a) created empty columns, loaded as-is, or deferred. The items below still need UI extraction or belong to the #4b write slice.

- [x] **downtime (#4b-1 ✅; ★ 2026-07-12 made work_type-aware)** — computed by `ProductionCalendar` (history estimated, future computed from `status_history`; production hours only). **The semantics are now contractual**: the decision collapses into the pure function `domain/work_order/downtime.py::segment_is_downtime` (a single source of meaning) — ON_HOLD follows `hold_reason.is_downtime` (None / not found → True); **PM + OPEN → False** (a machine with a PM due keeps running; the clock only starts when an engineer moves it to IN_PROGRESS — this is the only work_type-dependent behaviour); everything else follows the `wo_status.is_downtime` lookup. Both the engine (`_recompute_downtime`) and the read contract go through this one function; **already-closed work orders are not recomputed retroactively**. On the read side, every segment of `status_history` in `/work-orders/{no}/detail` and `/active-in` exposes **`is_downtime` (an additive computed field, not an ORM column; schema stays v1)**; a downstream consumer takes the true segments and fuses them with shift schedules / MES data to compute true downtime.
- [x] **State machine (#4b-1 ✅)** — the canonical 7 states + `work_order_status_history` are live; `ON HALT` / `DELAY` still sit in `work_type`, reclassification (W4) awaits the UI extraction / #4b-2.
- [ ] **Close-form fields** — `action_taken` / `labor_hours` / `cost` / `priority` / `opened_by` exist as empty [UI] columns; having `close` collect them is deferred to #4b-2 + UI extraction (W5)
- [x] **Work-order ↔ part link (#4b-1 ✅ + historical backfill ✅)** — `work_order_part` + `issue_part_to_work_order` (which moves `stock_transaction` and deducts `on_hand`) are live; history is reconstructed by `part_issue_backfill` (`part_issues.csv`, `backfill_part_issue`): one `work_order_part` + one `stock_transaction(ISSUE)` per row, **`on_hand` untouched**, occurrence-based `idempotency_key` making re-runs safe (see §5.8; W7).
- [x] **`miscreated` (W1 ✅)** — confirmed to mean mis-created → the whole row is dropped and never imported.
- [ ] **Semantics of `comments` (`MRQ-<n>`)** — loaded as `external_ref` (text); semantics still to be confirmed (W2)
- [ ] **PM source link** — `pm_source_id` exists as an empty column (the FK→pm_schedule is deferred); the reverse link `pm_schedule.last_work_order_no` already exists

---

## 8. Target schema (Postgres) — ★ slice #4a (the read entity) is live (migration 0004)

The tables as actually built. The differences from the draft reflect "the read slice only loads history; the write machinery is deferred to #4b" and "do not build tables around unconfirmed semantics" (guardrail #8):

```
work_order(                                       -- migrations/versions/20260621_0004_work_orders.py
  work_order_no       bigint PRIMARY KEY,         -- wo (legacy sequence, imported as-is; not auto-increment)
  asset_id            text   NOT NULL REFERENCES asset(asset_id),  -- 0 orphans; indexed ix_work_order_asset_id
  work_type           text   NOT NULL REFERENCES work_type(code),  -- 8 values as-is (W4 reclassification deferred to #4b)
  status              text   NOT NULL REFERENCES wo_status(code),   -- changed: only O/H here (the rich state machine lands in #4b)
  brief_description   text,                       -- html.unescape applied
  diagnosis           text,                       -- html.unescape applied
  external_ref        text,                       -- comments (MRQ-<n> / bare number, W2)
  opened_date         date   NOT NULL,            -- date_wo
  scheduled_date      date,                       -- sch_date (PM only)
  work_start_time     time,                       -- changed: time (unreliable 12h, §5.2; loaded at face value)
  work_complete_time  time,                       -- changed: time (time_cmpl; same)
  closed_date         date,                       -- editdate (close sign-off date)
  closed_time         time,                       -- edittime (24h, reliable)
  closed_by           text,                       -- changed: edituser as text (FK→app_user deferred)
  assigned_vendor     text   REFERENCES vendor(code),   -- CMA/CMB/SF (reusing the 0003 vendor lookup)
  assigned_person     text,                       -- changed: text (FK→person deferred to the Contacts slice)
  -- [UI], empty for now (pending extraction / filled by #4b writes):
  priority text, action_taken text, downtime_minutes integer, labor_hours numeric(6,2),
  cost numeric(12,2), opened_by text, pm_source_id text,   -- pm_source_id FK→pm_schedule deferred
  -- Audit (AuditMixin / ADR-005/016; proposed_by/confirmed_by are ready, populated by the #4b two-phase flow)
  created_at timestamptz NOT NULL DEFAULT now(), created_by text,
  updated_at timestamptz, updated_by text,
  source_actor text, proposed_by text, confirmed_by text
  -- ★ Not built (W1): is_voided / miscreated — ruled out of the import; may be added once semantics are confirmed
  -- ★ Deferred to #4b: work_started_at/completed_at/closed_at (synthesized timestamptz, needs the real UI dates),
  --                    idempotency_key (write de-duplication, ADR-006)
)

-- ★ Deferred to #4b: the pending intent for two-phase external confirmation (ADR-016; only needed by the write slice)
pending_proposal(
  pending_token   text PRIMARY KEY,
  operation       text NOT NULL,                  -- 'open_work_order' / 'close_work_order' / ...
  params          jsonb NOT NULL,
  dry_run_diff    jsonb,
  proposed_by     text NOT NULL,                  -- 'agent:<name>'
  idempotency_key text UNIQUE,
  status          text NOT NULL,                  -- PENDING / CONFIRMED / REJECTED / EXPIRED
  expires_at      timestamptz NOT NULL,
  confirmed_by    text,                           -- 'human:<id>', filled on confirm; anonymous rejected
  created_at timestamptz, resolved_at timestamptz
)

-- Built in 0004:
work_type(code, label)        -- the 8 values as-is (W4 reclassification deferred to #4b-2)
vendor(code, label)           -- CMA / CMB / SF (from 0003; 0004 adds SF). A contractor that no longer works
                              --   here keeps its lookup row, so its legacy work orders still resolve.

-- ★ Built by #4b-1 (migration 0007):
wo_status(code, label, rank, is_terminal, is_downtime)   -- the canonical 7 states (OPEN/IN_PROGRESS/ON_HOLD/COMPLETED/CLOSED/CANCELLED/VOIDED)
wo_hold_reason(code, label, is_downtime)  -- TEST_RUN(F)/WAITING_PARTS(T)/WAITING_VENDOR(T)/WAITING_MACHINE_TIME(F; 0019)/OTHER(T)
work_order += opened_at, closed_at (timestamptz), downtime_estimated (bool), hold_reason (FK)
work_order_status_history(id, work_order_no, from_status, to_status, hold_reason, changed_at, +audit)  -- transitions = the source of downtime
  -- ★ 2026-07-12: the read contracts (detail / active-in) additionally expose is_downtime per segment as a computed
  --   field (not a DB column, zero migration); decided by downtime.py::segment_is_downtime (work_type-aware:
  --   PM+OPEN = False, everything else follows the two lookups).
work_order_part(id, work_order_no, item_code, quantity, deleted_at, deleted_by, +audit)   -- parts issued to a WO (governed issue + the part_issue_backfill history; (wo,item) is NOT unique — several rows = several issues; ★ a direct-to-asset issue [charge_target=asset] creates no row here, ADR-024)
  -- ★ 0022 — part issues can be re-quantified or cancelled: `update_part_issue_quantity` (located by part_id;
  --   the delta moves stock — an increase books another ISSUE and deducts [rejected if short], a decrease books a
  --   RETURN back into stock; the summary row's quantity is updated in place) /
  --   `cancel_part_issue` (RETURN of the full quantity + a **soft delete** of this row via deleted_at/deleted_by;
  --   get_parts excludes it). The ledger (stock_transaction) is append-only — old entries are never rewritten and
  --   compensating entries leave a trail; terminal-state work orders reject changes. ★ **Backfilled historical
  --   issues (source_actor='human:data-migration' [= BACKFILL_ACTOR]) always reject re-quantification and
  --   cancellation** — the backfill booked its ledger entries with adjust_on_hand=False and **never deducted
  --   on_hand**, so a RETURN would inflate stock out of thin air. The guard keys on the explicit marker (no longer
  --   inferring from terminal state: legacy data has OPEN work orders carrying backfilled parts, so a terminal-state
  --   guard would not reliably catch them). The UI hides those buttons.
  -- ★ Direct-to-asset issues (which have no summary row) get the same capability: InventoryService
  --   `update_asset_issue_quantity` (= cancel the old entry + rebook at the new quantity, one transaction) /
  --   `cancel_asset_issue`; cancellation uses the deterministic idempotency key assetissuecancel:v1:<txn_id>
  --   (one per ledger entry, so repeated cancel/re-quantify is always safe); `cancelled_asset_issue_ids` lets the UI
  --   hide the buttons on already-cancelled rows; backfilled direct issues (rescued-to-asset) carry the same
  --   BACKFILL_ACTOR marker and are likewise blocked (`_assert_not_backfill_issue`).
stock_txn_kind(code, label)                              -- ISSUE/RETURN/ADJUST/RECEIVE

-- ★ Issue attribution (ADR-024, migration 0015): stock_transaction gains charge_target_asset_id
stock_transaction(txn_id, item_code, work_order_no, charge_target_asset_id, qty_delta, kind, reason, occurred_at, idempotency_key, +audit)  -- the stock ledger (ADR-005)
  -- charge_target_asset_id: FK→asset; a direct (non-WO) issue is charged to the asset; a WO issue leaves it NULL (the asset is resolved through the WO)
  -- CHECK ck_stock_transaction_issue_charge: kind<>'ISSUE' OR num_nonnulls(work_order_no, charge_target_asset_id)=1
  --   → an ISSUE has exactly one charge target (work order xor asset); no orphan issues, no double attribution;
  --     non-ISSUE kinds (RECEIVE/ADJUST/RETURN) are unconstrained

-- ★ Built by #4b-2 (migration 0008):
pending_proposal(pending_token, operation, params jsonb, dry_run_diff jsonb, proposed_by,
  idempotency_key(128) unique, status, expires_at, confirmed_by, created_at, resolved_at)  -- ADR-016 two-phase
work_order += origin_station, idempotency_key(128, unique), evidence_ref(160)   -- ADR-017 on-box

-- ★ Deferred:
wo_priority(code, label, rank)              -- the priority column is empty and has no lookup; pending UI extraction (W5)

-- ★ Live (migration 0012; ADR-020 note↔comment mapping):
work_order_note(                                  -- the append-only work log (§1.6)
  id bigint PRIMARY KEY,
  work_order_no bigint NOT NULL REFERENCES work_order(work_order_no),
  entry_type text NOT NULL REFERENCES wo_note_type(code),   -- progress/diagnosis/hold/resume/part/note/report/ai_candidate
  body text NOT NULL,                             -- verbatim (html.unescape applied)
  author text NOT NULL,                           -- human:<id> / agent:<name>
  occurred_at timestamptz NOT NULL,               -- when the update happened (≠ created_at)
  status_history_id bigint REFERENCES work_order_status_history(id),  -- linked when produced by a state transition
  idempotency_key text UNIQUE,                    -- de-duplicates note↔jira comment sync (ADR-006/020)
  deleted_at timestamptz, deleted_by text,        -- ★ 0022 soft delete (list_notes excludes)
  created_at timestamptz NOT NULL DEFAULT now(), created_by text, source_actor text
)
wo_note_type(code, label)                         -- progress/diagnosis/hold/resume/part/note/report/ai_candidate (0021)

-- ★ Live (migration 0017; ADR-020 decision 3): work order ↔ external knowledge base link (first target: Jira MRQ)
work_order_external_link(
  id             bigserial PRIMARY KEY,
  work_order_no  bigint NOT NULL REFERENCES work_order(work_order_no),
  system         text NOT NULL,                   -- controlled: jira (allow-list shape guard, decision 8)
  external_key   text NOT NULL,                   -- MRQ-<n> (guard regex ^MRQ-\d+$)
  link_type      text NOT NULL,                   -- referenced (legacy backfill) / forwarded / appended
  title          text,
  forward_idem_key text,                           -- ★ 0025: the anti-duplication anchor for batch forwards (the same key never opens a second MRQ)
  + audit,                                        -- source_actor=agent:<name>, created_by=human:<id> (dual)
  UNIQUE (work_order_no, system, external_key, link_type)   -- idempotency
)

-- ★ Live (migration 0025; ADR-020 decision 1 revised 2026-07-06): the note→MRQ comment auto-sync outbox.
--   cmms now **calls the Jira REST API directly** (HttpJiraForwarder), using the per-user PAT of whoever created
--   the link (the ADR-022 vault).
jira_outbox(
  id             bigserial PRIMARY KEY,
  note_id        bigint NOT NULL REFERENCES work_order_note(id),
  work_order_no  bigint NOT NULL REFERENCES work_order(work_order_no),
  external_key   text NOT NULL,                    -- MRQ-<n>
  on_behalf_user text NOT NULL,                    -- the PAT owner = the link creator (the flush writes to Jira as them)
  status         text NOT NULL DEFAULT 'pending',  -- pending / sent / failed
  attempts       int  NOT NULL DEFAULT 0,
  last_error     text,
  sent_comment_id text,
  attachments_uploaded boolean NOT NULL DEFAULT false, -- ★ 0026: photos go up first; the comment is only sent once this flag is set (retry de-duplication)
  + audit,
  UNIQUE (note_id, external_key)                   -- idempotency: one note → at most one comment on a given MRQ
)
```

**Auto-sync semantics (ADR-020 decision 1, revised)**: once a work order has a `forwarded` / `appended` MRQ link,
every subsequent `work_order_note` (a manual update / Hermes natural language / a transition note) enqueues a
`jira_outbox` row **in the same transaction** as `add_note`; after a web update a **background flush** sends it
immediately, and the CLI `jira-flush-outbox` retries as a backstop. The comment template is
`[WO <no> · <Taipei time> · <author>]\n<note body>`, and the body is **reproduced faithfully, never translated**
(auto-sync involves no LLM; the original stays in the system of record — under ADR-023 only the summary/description
honour `jira_output_locale`). **v1 syncs additions only**: note corrections (§1.6 "edited") and soft deletes (0022) are
**not** written back to the MRQ (a soft-deleted note is marked `note-deleted` at flush time and not sent). If the
integration is unconfigured (`CMMS_JIRA_*` / the master key / the PAT is missing), the row is honestly marked
`config-missing` / `pat-missing` — it never fakes success.

**Photo sync (migration 0026)**: the photos attached to a note (`attachment(owner_type='work_order_note',
owner_id=str(note.id))`, not soft-deleted) are uploaded to the MRQ one by one **before** the comment is sent.
The flow: download the bytes from storage → `HttpJiraForwarder.upload_attachment`
(`POST /rest/api/2/issue/{key}/attachments`, multipart + `X-Atlassian-Token: no-check`) → only when all succeed is
`attachments_uploaded=true` recorded → finally the comment is sent, its body carrying one Jira-wiki embed line per
photo (`!<filename>!`) after the original text, plus a trailing `(photos: N)`. **Filename convention (collision
avoidance)**: the filename sent to Jira is `wo<no>-note<id>-<original filename>` (several notes on the same MRQ may
carry identically-named camera files such as `IMG_0001.jpg`; the prefix stops them overwriting each other).
**Retry trade-off (v1)**: if one photo fails mid-upload the row goes `failed` and the flag is **not** set, so a retry
re-uploads the ones that already succeeded — Jira may end up with duplicate attachments. We chose "rather duplicate
than lose". An attachment that cannot be downloaded (object missing from R2) marks the row `failed` honestly — it
never fakes success. The initial batch forward and the subsequent auto-flush share the same code path; the dry-run
preview reports a per-WO `photo_count` and a `total_photos`.
