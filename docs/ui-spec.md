# UI MVP Specification — The CMMS Engineer Console

> Status: **implemented** — the five MVP screens (report a fault / work order queue / detail timeline with writes and state operations / spare parts / PM) plus the responsive three-column desktop layout and the agent dock are built (`src/cmms/web/`, mounted at `/app`). Follows ARCHITECTURE.md **ADR-019 / 022 / 023**.
> This document focuses on the concrete specification of the MVP screens; the strategic rationale (why an engineer console, why HTMX, why three separate surfaces) is in ADR-019.
> Technology: responsive web (HTMX + FastAPI Jinja2), **phone/tablet first**, served in-process by the `cmms` app. All writes go through a domain service (guardrail #1); writes carry audit data and, where applicable, are subject to gated-write.

---

## 0. Scope

**The MVP audience is the CMA on-site maintenance engineer.** It covers the high-frequency daily loop: **reporting a fault, executing a work order, looking up spare parts, viewing and executing maintenance schedules.**

**Out of scope** (explicitly excluded, to avoid duplicating the downstream analytics consumer): management/roll-up dashboards (→ the downstream consumer), a native mobile app, purchasing/POs, a reporting centre, asset CRUD administration. **(★ Multi-language has since moved from "out of scope" into scope — see §1 Language and ADR-023.)**

---

## 1. Common to All Screens

- **Device**: mobile-first responsive layout (large buttons, single column, one-handed use); tablet/desktop expand to multiple columns automatically.
- **Language (★ ADR-023)**: defaults to **English**, switchable per user to **zh-TW / vi (Vietnamese)**; the choice is **persisted per user** (stored on `user_account`, carried across logins until changed). Interface chrome comes from an i18n catalog with an `en` fallback; **data values (EID, part number, status code, work order free text) are not translated**. The Jira output language is a **separate** preference (`jira_output_locale`, also English by default).
- **Identity**: each user logs in → obtains `human:<id>` (used for the `source_actor` audit field and for gated-write confirmation). The login mechanism is **TBD** (see §6).
- **Navigation**: four bottom tabs — Report / My Work Orders / Parts / Maintenance.
- **QR entry point**: scan an asset QR code (EID) → land directly on that asset's page (one-tap report / see its work orders / see its PMs). The phone browser camera is sufficient; no native app needed.
- **Photos**: every photo capture or view goes through the `attachment` service (R2 + presigned URL).

---

## 2. Screen 1: Report a Fault (highest frequency)

- **Purpose**: a machine broke; log a reactive work order in a few seconds (a mandatory running log — friction must be as close to zero as possible).
- **Entry**: the bottom tab, or a QR scan that pre-fills the EID.
- **Primary action**: pick the asset (EID, or already supplied by the QR scan) → write a brief fault description (`brief_description`, Chinese supported) → take photos (multiple, optional) → submit.
- **Displays**: the selected asset's name / production line / subtype (so the user can confirm they picked the right machine).
- **Calls**: `open_work_order` (`work_type=REACTIVE`, `status=OPEN`) + attachment upload.
- **Boundaries**: the EID must exist in the asset master (otherwise blocked — no silently creating a stub asset); anonymous is rejected (`human:<id>` required); on submit the work order appears in that person's "My Work Orders".
- **Open questions**: whether priority/severity should be captured here (a [UI]-only field); speech-to-text (phase 2).

---

## 3. Screen 2: My Work Order Queue

- **Purpose**: see the work orders assigned to me / raised by me, and push them through to closure.
- **Displays**: a list of work order cards (status, asset, brief description, date opened, whether the machine is down), filterable by `OPEN` / `IN_PROGRESS` / `ON_HOLD`.
- **Primary actions** (state machine ops): start / hold (pick a reason: `TEST_RUN` / `WAITING_PARTS` / …) / resume / complete / close. On completion, record `action_taken` (what was done) + `labor_hours` + before/after photos ([UI] field).
- **Part issue (inline)**: inside a work order, "issue a part" → pick item → quantity → `issue_part_to_work_order` (decrements stock + writes a `stock_transaction`).
- **Calls**: start/hold/resume/complete/close, `issue_part_to_work_order`, attachment.
- **Boundaries**: state transitions follow the state machine (`CLOSED` cannot transition further); downtime is computed by the system from the status intervals (never hand-entered).
- **★ Work order detail = a timeline**: tapping a work order card opens the detail page, which shows an append-only **work log** (`work_order_note`, domain-model §1.6): the initial fault, then each progress / diagnosis / waiting-for-parts update (each carrying a **timestamp + author** — human or Hermes NL), interleaved with the status transitions. For a long downtime (a repair spanning days, or days of waiting for parts) **each update appends a new entry and preserves it verbatim**; the "brief description" is never overwritten. "Add an update" — typed by hand or via Hermes NL — goes down **the same write path** (`source_actor` distinguishes human from agent). This page is also the data source for writes into Jira MRQ tickets (title/subject synthesised, each note mapped 1:1 to a comment; ADR-020 decision 7).
- **Open questions**: which fields close captures ([UI]); whether `labor_hours` is computed automatically (from start/stop times) or entered by hand; finalising the controlled vocabulary for `work_order_note.entry_type` (progress / diagnosis / hold / resume / part / note).

---

## 4. Screen 3: Spare Parts Lookup (with photos)

- **Purpose**: is this part in stock, where is it, what does it look like, is there a substitute.
- **Displays**: search (description `ilike` / `item_code` / `vendor_part_no`) → result cards (thumbnail, name, `on_hand`, storage bin, whether below the reorder point). Tap through to detail: large images (multiple), full description, substitutes, kit BOM, applicable subtypes, supplier.
- **Primary actions**: search; (when arriving from a work order) select this part for issue.
- **Calls**: `list_inventory` / `get_inventory_item` (read) + attachment for images.
- **Photo source**: attachment (a loader uploads from `data/media/inventory/` to R2; the index is the leading token of the filename → `item_code`; the description becomes the caption; **7 parts have multiple images**, preserved as-is).
- **Boundaries**: read-only, open.
- **Open questions**: UOM missing; 40 cross-source `asset_subtype` values — neither blocks lookup.

---

## 5. Screen 4: Maintenance Schedule (PM)

- **Purpose**: **organise and review maintenance tasks**, and execute PMs that are due.
- **Displays**:
  - **Due / overdue list**: PMs coming due and already overdue on my assets (or my production line), sorted by `next_due`, overdue flagged in red.
  - **PM detail**: asset, maintenance task (`task` name/description), frequency (`freq_unit`), last completed, next due, assigned vendor.
  - **Task steps (the checklist / "items")**: ⚠ **TaskStep data has not been extracted yet** ([UI]-only) → the MVP shows the task itself first; the per-step checklist waits on a supplementary extract from eMaint (see the salvage list in §6).
- **Primary action**: press "execute" on a due PM → generate and open its PM work order (which hands off to Screen 2 for execution).
- **Calls**: `pm_schedule` read + `generate_pm_work_order` (governed write).

### 5.1 The Modern Way to Do "Date Arrives → Work Order" (**converged into ADR-021**)

Legacy eMaint: on the `pmnextdate` day, a PM work order is generated automatically. The CMMS has **not** built this auto-generation yet (it is a write operation still to be developed). The modernised proposal:

- **Not just "on the day" → a lead window**: generate N days before the due date (e.g. 7) into a `PLANNED` state, so the engineer can pre-stage parts and schedule the work, instead of it materialising the same morning.
- **Fixed vs Floating (anti-pile-up)**: the next due date is computed either from the *scheduled* date (Fixed) or the *actual completion* date (Floating); Floating prevents PMs from piling up after one is done late. This maps to eMaint's `calendar_freq_type` ([UI] Shadow/Fixed, still to be extracted).
- **Automatic + idempotent + audited, not a human gated-write**: a scheduler runs `generate_due_pm_work_orders` daily; the idempotency key is `pm_id + due_cycle` (no duplicate generation within the same cycle); `source_actor` = `scheduler` (time-based) / `mes-pipeline` (usage-based). A PM is determined by its schedule, not by discretion → it does **not need** propose/confirm (that mechanism is for reactive, discretionary operations).
- **Usage-based + forecast (the ADR-021 modernization win)**: read per-EID production counts from the MES → generate only once cumulative usage since the last PM crosses a threshold (a **dual trigger** of time and usage; whichever fires first wins). Usage-based PMs have no calendar date, so recent production rates are used to **forecast** a due date, which lets them enter the due list and the lead window. This requires MES ingestion (DMZ FTP B2MML), read-only, never written back (ADR-011).
- **Write-back on completion**: PM work order complete → advance `pm_schedule.next_due` (Fixed counts from the due date, Floating from the completion date).

### 5.2 Both Execution Modes Coexist (settled, ADR-021)

The automatic scheduler and "review + execute on demand" are **both required and coexist long-term**: the scheduler is the safety net that never misses a PM; on-demand gives the engineer control and handles non-periodic / discretionary work orders. **The MVP builds "review + on-demand generation" first** (so the engineer can see it and press it); **the automatic scheduler is a fast-follow**; usage-based + forecast layer on afterwards, once MES ingestion exists.

---

## 6. Cross-Screen Dependencies / Open Questions (to be settled when finalising the spec)

- **Login / identity mechanism**: how the web UI authenticates a user as `human:<id>` (SSO / username+password / ?) — this affects the audit trail and gated-write confirmation.
- **The `attachment` slice**: the photo infrastructure (R2 + a pointer table + a loader) is a prerequisite for Screens 1, 2 and 3 → **it should be built first**.
- **eMaint salvage (time-limited — do it while eMaint is still alive)**: ① spare-part images ✅ (downloaded); ② **TaskStep / PM checklist items** (needed by Screen 4, still only in eMaint); ③ `action_taken` history.
- **The PM generation engine**: a write slice; the strategy is **settled as ADR-021** (time / usage / forecast triggers, with scheduler and on-demand coexisting); time-based can be built first, usage/forecast wait on MES ingestion.
- **Backfill of historical part issues**: `data/raw/part_issues.csv` (4,602 rows) → `work_order_part` + `stock_transaction`; `on_hand` is not decremented again (this only rebuilds the ledger). It is a data load rather than UI work, but it is what gives Screens 2 and 3 a real issue history.
- **QR labels**: producing and sticking QR labels on assets is a shop-floor activity, not software; the URL takes the form `/asset/<EID>`.

---

## 7. MVP Screen → Domain Quick Reference

| Screen | Primary domain op (write) | Primary reads | New prerequisite slice |
|---|---|---|---|
| Report a fault | `open_work_order` + attachment | asset lookup | attachment |
| My work orders | start/hold/resume/complete/close, `issue_part_to_work_order` | work_order / work_order_part | attachment (before/after photos) |
| Spare parts lookup | (read-only) | `list/get_inventory_item` + attachment | attachment + image loader |
| Maintenance schedule | `generate_pm_work_order` | `pm_schedule` / `task` | PM generation engine, (TaskStep extraction) |
