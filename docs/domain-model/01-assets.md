# CMMS Domain Model — 01. Assets (Equipment Master)

> Extraction Spec, page 1. Sources: eMaint X4 UI exploration + the `assets.csv` export + cross-module validation.
> Status: **draft v0.1** — fields still to be filled in are marked `⚠ TODO`. This is a living document, not a one-shot artefact.

---

## 0. Where this module sits

Assets is the **root entity** of the whole CMMS: Scheduled Activity and Work Orders both point at it via `compid` (EID) as a foreign key; Inventory relates to it indirectly through `asset_subtype`. It is also the **primary anchor for the CMMS↔MES pipeline**. Get this entity right first — every later slice stands on it.

Data shape: roughly half the asset master has ever carried a work order or a PM schedule; the rest are "passive" master records (mostly Jigs / Meters). Any modelling that assumes every asset is an active, maintained machine will be wrong.

---

## 1. Entity: Asset

Fields come from two places: **[CSV]** = present in the `assets.csv` export; **[UI]** = visible only on the eMaint detail form, not exported. We model both — the raw data fixes enums and cardinality, the UI fills in the missing columns.

### 1.1 Identity and classification

| Target field | Source | Type | Required | Notes / enum / FK |
|---|---|---|---|---|
| `asset_id` | [CSV] `compid` | string | ✅ PK | Pattern `EID-\d{5}`; **immutable** (resolved: in practice EIDs are never changed → **Key Change is disabled in the new system**; replacing an asset = retire the old one + create a new one. Child-table FKs therefore need no `ON UPDATE CASCADE`) |
| `description` | [CSV] `comp_desc` | string | ✅ | Machine name; **not unique** — machines of the same model share a name, so a substantial fraction of the rows are duplicates by name. Never use it as a key |
| `parent_asset_id` | [UI] | string | ⬜ | FK→`Asset.asset_id`; a **denormalized cache** of the single-parent `contains_module` edge (authority = `asset_relationship`, ADR-018); must not form a cycle |
| `asset_type` | [CSV] `assettype` | enum | ✅ | `Production` / `Support` / `Jig` / `Meter` / `Computer` — **5 values total; UI exploration only surfaced 4** (a reminder that the data, not the UI, fixes the enum) |
| `asset_subtype` | [CSV] `assetsubtp` | string→lookup | conditional | Free text, a few dozen distinct values, sparsely populated. **Conditionally required**: nearly every `Production` and `Computer` asset carries a value; `Meter` and `Support` assets essentially never do. Should be promoted to an `AssetSubType` lookup table |
| `department` | [CSV] `department` | enum→lookup | ✅ (0 NULL after override) | A short code list (`EQ` = Equipment Engineering / `QA` = Quality Assurance / `PE` = Process Engineering / `QS` = Quality Systems / `SQE` = Supplier Quality Engineering / `PD` = Product Development / `ME` = Manufacturing Engineering / `SF` = Shopfloor / `BD`). A single raw row had a blank department → a curated override fills it in (`transform.KNOWN_DEPARTMENT_OVERRIDES`). The column is **deliberately nullable** (ingestion defence) |
| `process_segment_class` | [UI] | string | ⬜ | **Key field for the MES pipeline** — maps to the MES process segment |

### 1.2 Location within the plant

| Target field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `line_no` | [CSV] `line_no` | string→lookup | ✅ (almost always populated) | A short list of line codes: the production lines, a sub-assembly loop, end-of-line, incoming QC, a calibration centre, the warehouse, plus an `Others` catch-all. **Dirty data: the same line occurs under two spellings differing only in capitalisation — migration must normalize** |
| `site` | [UI] | string | ✅ | Observed constant `PLANT-1`; becomes an FK if we ever run multiple sites |
| `building` | [UI] | string | ⬜ | |
| `floor_level` | [UI] | string | ⬜ | |
| `room_space` | [UI] | string | ⬜ | |

> **Addendum — line-code rename (migration 0033)**: one line code carried a zero-padded prefix that existed purely as a legacy eMaint workaround, to force its alphabetical sort into the right place. The migration renames it to its real name; the loader keeps the padded form as an **alias**, so re-loading `assets.csv` converges on the canonical code automatically. Dropdowns then sort by magnitude rather than by string, via a derived `line_sort_key`.

### 1.3 Manufacturer and identification

| Target field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `manufacturer` | [UI] | string | ⬜ | |
| `model_no` | [CSV] `model_no` | string | ⬜ | Partially populated (roughly half the rows) |
| `serial_no` | [CSV] `serial_no` | string | ⬜ | Partially populated; **not unique enough to be an identity** — `asset_id` stays the key |
| ~~`owner`~~ | — | — | — | **Dropped in 0031**: the single-owner column was replaced by the multi-owner cross table `asset_owner` (below). |

> **★ Asset owners `asset_owner` (many-to-one, 0031)** — **hard-to-maintain machines have several owners** who jointly own their PM and repairs. Modelled as a cross table `asset_owner(asset_id, person_name, position)` (composite PK; `position` only orders the list — it feeds the backward-compatible single-value view (`assigned_person` = first owner) and the display order; all owners are equal). It is the **source of truth for work-order assignees**: when a REACTIVE work order is opened without an explicit assignee (`_open_impl`) **all** owners are assigned, and PM generation (`_generate_pm_impl`) does the same; open/close notifications go to all owners. `person_name` stores the exact legacy string (not a person FK — it is the same key "My work orders" filters on), normalized through `clean_person_name` and de-duplicated in order. 0031 backfilled `asset.owner` (single value, 0029) as position=0 and then dropped the column. Service: `get_owners` / `owners_map` / `set_owners` (whole-set replace, admin-only) / `set_owner_bulk` (REPLACE semantics).

### 1.4 Status flags

| Target field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `available_for_service` | [CSV] `available` | bool | ✅ | Currently usable or not. The large majority of the fleet is available at any time |
| `is_active` | [UI] | bool | ⬜ | Whether the asset record is live (soft delete / retirement). **Semantically distinct from `available` — both columns coexist**: out for repair = active true + available false; only scrapping sets active false. [UI]-only, no data yet; migration creates the column with default true, to be populated once extracted. **★ Governance semantics (2026-07-05)**: `is_active=false` (retired) **blocks REACTIVE work-order creation** — domain `_open_impl` rejects `work_type="REACTIVE"` (a retired machine is off the line and should not generate reactive/downtime work orders). The guard lives in the domain layer, so every channel (web report, on-box, confirm, future MES) is covered consistently; the web layer additionally disables the report button on the asset detail page and shows a friendly banner on submit. PM generation (`work_type="PM"`) is **not** subject to this guard (schedule-driven determinism, separate cycle). |
| `up_down_tracking` | [UI] | bool | ⬜ | Whether uptime / downtime tracking is enabled; **feeds MES OEE calculation** |

### 1.5 Integration and misc

| Target field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `asset_ref` | [UI] | string | ⬜ | Probably an ERP / fixed-asset cost number — semantics to be confirmed |
| `host_name` | [UI] | string | ⬜ | Hostname for computer-class assets; `Computer` assets map to MES terminals |
| `weblink` | [UI] | url | ⬜ | |
| `comments` | [UI] | text | ⬜ | |
| `picture` | [UI] | blob/url | ⬜ | |
| `product` | [UI] | string | ⬜ | Semantics to be confirmed |

### 1.6 Cross-system identity (canonical identity service, ADR-015)

Assets is also the **canonical equipment-identity source across systems**: `asset_id` (EID) is the shared anchor used by cmms and by a downstream analytics consumer. External-system identifiers are **not** flattened into the asset master table; they are carried in a normalized crosswalk table (correct cardinality + one table for many systems + no guessing at MES naming).

| Target field/table | Type | Notes |
|---|---|---|
| `asset_external_id` | table | Crosswalk: `(asset_id, namespace, external_id)`. One machine can map to several external ids (1:N) |
| └ `namespace='mes_equipment'` | enum value | MES equipment id. **Confirmed to be the same identifier as `asset_id`** → MES joins directly on `asset_id`, so **this namespace does not need to be populated in practice**; the enum value is kept for completeness. The crosswalk's real use is the non-identical systems below |
| └ `namespace='layer_b_sensor'` | enum value | Physical sensor id in the downstream consumer's Layer B (current/vibration CTs, PLC I/O taps; one machine can have several) |

- **Read** (identity resolution): the API + MCP read tools (`get_asset` / `resolve_asset_identity`) are open read-only to downstream consumers.
- **Write** (bind/unbind, register a MES id): still goes through the single domain-service write path, fully audited. When a downstream consumer registers a sensor binding it acts as a thin client of cmms via a governed write; it never writes the table directly.
- cmms does **not** manage sensor hardware lifecycle (that belongs to the downstream consumer's Layer B infrastructure); it only stores the "sensor id ↔ asset" identity mapping.

### 1.7 Asset composition graph (machine ↔ module, ADR-018)

cmms is also the **system of record for the machine→module asset tree/graph** (the downstream analytics consumer overlays its Layer B/C module-level state onto this tree). MES's own module structure is incomplete (only a handful of machines have child-module EIDs at all, while physically multi-module machines such as the FlexBonder appear in MES as a single asset), so the truth cannot come from MES alone.

**Two relationship kinds, stored in a single typed authoritative junction `asset_relationship`**:

| relationship_type | Meaning | Direction | Cardinality | Example |
|---|---|---|---|---|
| `contains_module` | Machine **contains** module | `from` = machine, `to` = module | Strictly acyclic tree (each module has ≤1 active container) | `EID-10019` (a multi-module Curer) ⊃ `EID-10020` / `EID-10021` / `EID-10022` |
| `shared_dependency` | Shared resource **serves** machine | `from` = resource, `to` = machine | **N:M** | One industrial PC serves several Aligners |

- Columns: `from_asset_id` / `to_asset_id` / `relationship_type` (FK lookup) / `source` (`mes_dependent_equipment` | `cmms_curated`) / `valid_from` / `valid_to` (temporal) + audit columns.
- **Edge source + classification**: authority is the MES MES dependent-equipment export (a self-referencing parent/child list); **we never parse `comp_desc` (guardrail #8)**. Classification rule: a child with exactly one parent → `contains_module`; **the same child with multiple parents → `shared_dependency`**. Direction follows the type (contains: from = machine; shared: from = shared resource). **Edges bind on EID**, never on the display name — some machines carry a stale display name in cmms that disagrees with MES, and that is a data-reconciliation item which must not be allowed to affect the edge.
- **`parent_asset_id`** is the denormalized cache of the single-parent `contains_module` edge (the junction remains authoritative).
- **Non-EID module identity** (decision D2=(a); **supersedes the original (b) proposal of minting synthetic ids**): modules that MES cannot see (e.g. the sub-modules of a FlexBonder) get **no minted id and no row in the cmms asset master** → preserving `asset_id = EID = MES-EID` with zero exceptions. Module granularity lives only in the downstream consumer's **Layer B/C fusion layer**; on the cmms side, **module-level repair = a work order against the machine EID + a note**. Long term, if this is really needed, MES should instrument the modules and issue real EIDs (never synthetic ids). **This slice creates neither `identity_source` nor a `MOD-` namespace.**
- **`asset_id` = MES EID is per-EID and tier-agnostic**; cmms `asset_id` therefore includes module-level EIDs (they *are* MES module EIDs).
- **WO/PM attribution** stays on whichever EID was used at open time (machines and modules are mixed in the historical data — that is the empirical reality); cmms provides a **rollup read** (a machine's WOs = its own + `contains_module` descendants; **`shared_dependency` is not rolled up**).
- **Writes** (bind/unbind) go through governed domain-service ops, fully audited (guardrail #1).

> **Status**: ADR-018 design locked and the **implementation slice is built** (D2=(a)): migration 0009 + `asset_relationship` + `asset_relationship_type` lookup + `link_containment` / `link_shared_dependency` / `unlink_relationship` governed ops + tree-descendant and WO rollup reads + API + MCP + dependent-equipment classification loader + tests; **verified against real PostgreSQL** (full suite green + `alembic upgrade` to 0009 + `alembic check` clean); **the asset master table is untouched (no `identity_source` column)**. The dependent-equipment export is noisy and the loader is written for it: the raw rows collapse to a much smaller set of distinct edges (duplicates collapsed, null-child rows skipped, self-references dropped), and of those only the edges whose **both** endpoints exist in the asset master are bound — the rest are **dropped and counted, never repaired by minting phantom ids**. The counts are reported by the loader at load time, not hard-coded here.

---

## 2. Operations (CRUD + actions)

From eMaint UI exploration:

**Single-record actions**: Create / Read / Update / Delete, Key Change (change primary key), Bar Code Labels, Generate QR Code, Add Work Order, Add Request, Print, Link To External App, Log Location.

**List/batch actions**: Mass Change Values, Mass Delete Records, Data Export, Distribute PM's.

> Modernization notes: `Add Work Order` / `Add Request` should be cross-entity domain-service operations (Asset → WorkOrder) in the new system, not methods on Asset itself. **`Key Change` is disabled** (EIDs are never changed in practice; `asset_id` is immutable). `Mass Delete` is high risk — under the governed-write model it needs dry-run + confirmation, and must not be exposed to agents.

---

## 3. Workflow / state machine

**Asset has no rich state machine of its own** — only two independent flags: `available_for_service` (Yes/No) and `active`. The real state machine lives in WorkOrder (open `O` → history `H`). Do not force a lifecycle onto Asset.

---

## 4. Relationships to other entities

| Relationship | Target | Cardinality | Join key | Validation |
|---|---|---|---|---|
| PM schedules | ScheduledActivity | 1 : N | `compid` | ✅ 0 orphans; only a minority of assets carry PMs |
| Work orders | WorkOrder | 1 : N | `compid` | ✅ 0 orphans; only a minority of assets have ever carried a WO |
| Machine ⊃ module | Asset (self) | 1 : N (tree) | `asset_relationship` type=`contains_module` (`parent_asset_id` cache) | **ADR-018**: module-level EIDs are already asset rows where MES issued them; MES-invisible modules get **no minted id and no master row** — that granularity stays in the downstream consumer's Layer B/C (D2=(a)) |
| Shared resource ↔ machine | Asset (self) | **N : M** | `asset_relationship` type=`shared_dependency` | **ADR-018**: one industrial PC serves several machines (D1=(b)) |
| Related parts | Inventory | **N : M** | **via `asset_subtype`, not a single asset** | inventory.`asset_sub` is a comma-separated multi-value string; must be split into a junction table |
| Owner | Contact / Person | N : 1 | `assignto` (indirect) | ⚠ Dirty: formatted `VENDOR (Person Name)`, not a contactid; needs parsing and mapping |

---

## 5. Data-quality issues (migration must handle these)

1. **`line_no` case inconsistency** — the same line appears under two capitalisations. Build a lookup table and map to canonical values.
2. **The CSV export carries only a third of the real column set** — the [UI] fields in §1.x must be extracted separately from eMaint (see §7).
3. **`asset_subtype` is free text** — **partially resolved (Inventory slice #5)**: a canonical `asset_subtype` lookup now exists (the union of the values seen in asset ∪ inventory, with obvious variants folded automatically by an alias table — e.g. an `<X> CALIBRATOR` / `CALIBRATOR <X>` word-order flip). The `inventory_item_asset_subtype` junction FKs into it; **`asset.asset_subtype` stays a text soft reference** (the values are already canonical and join on `= asset_subtype.code`; we do not retrofit the FK, to avoid breaking this slice's loader/tests). **The subtypes with no obvious cross-source match still need to be clarified one by one**; once confirmed, extend the alias table and re-run.
4. **Encoding** — `assets.csv` is clean `utf-8-sig`; but neighbouring modules (work_orders / contacts / inventory) are latin-1 and contain HTML-entity-encoded CJK, so cross-module joins must normalize encoding first.
5. **`department` had one blank row (fixed)** — a single asset came through with a blank `department`. The correct value was confirmed with the owning engineer and applied via `transform.KNOWN_DEPARTMENT_OVERRIDES` (**the raw CSV is not modified** — the fix lives in code, under review, not in the data); after load, **0 NULLs**. The column is **deliberately left nullable** as an ingestion defence (a future gap loads as NULL + flag, which beats hard-rejecting the whole batch) — not because the current data is incomplete.

---

## 6. MES pipeline anchors (what this module contributes to the CMMS↔MES pipeline)

| CMMS field | MES counterpart | Purpose |
|---|---|---|
| `asset_id` (EID) | MES equipment id | **Primary join key** — confirmed to be **the same identifier** (both `EID-xxxxx`); join directly, no crosswalk row needed |
| `process_segment_class` | MES process segment | Process-segment alignment |
| `line_no` | MES line | Line-dimension alignment |
| `host_name` | MES terminal hostname | Computer-class assets map to MES terminals |
| `up_down_tracking` + WO downtime | MES OEE / availability | Utilization and availability calculation |

> Candidate first pipeline use case: MES equipment running hours → drive meter-based PM in the CMMS (matching the `Meter` assets and the Meter Readings tab).

---

## 7. Next: extraction still outstanding for this module

The following need to be pulled from the eMaint UI (browser automation) before the Asset entity is complete:

- [ ] Types, required-ness and defaults of every **[UI]** field in §1.x (especially the semantics of `asset_ref`, `product`, `process_segment_class`)
- [ ] The field structure of `Meter` assets and the Meter Readings tab — and how it relates to `Monitor Points Master` / `Monitor Class`
- [ ] The actual behaviour of Key Change (what happens to child records after the key changes)
- [ ] Whether `assettype` / `department` are system-configurable lookups or hard-coded enums

---

## 8. Proposed normalized target schema (Postgres — for use in later dev prompts)

> **Slice #1 scope vs final target**: below is the **final** target schema. The Asset slice (#1) lands first: the `asset` table itself (the exported CSV columns populated + [UI] columns created empty), the three lookups `asset_type` / `department` / `line`, and `asset_external_id` + `external_id_namespace` (ADR-015). **Deferred**: `asset_subtype` is stored as text in this slice (not an `asset_subtype_id` FK — deferred to the Inventory slice) and the `asset_part` N:M table (Inventory slice). `is_active` is created with default true (data pending UI extraction).

```
asset(
  asset_id              text PRIMARY KEY,          -- EID-xxxxx (= MES-EID; D2=(a), no synthetic ids)
  description           text NOT NULL,
  parent_asset_id       text REFERENCES asset(asset_id),  -- contains_module single-parent cache (authority = asset_relationship)
  asset_type            text NOT NULL REFERENCES asset_type(code),
  asset_subtype         text,                       -- text in this slice; becomes a lookup in the Inventory slice
  department            text REFERENCES department(code),  -- nullable: one raw row had no department (ingestion defence)
  line                  text REFERENCES line(code), -- code-keyed lookup; normalization removes the case problem
  site                  text NOT NULL DEFAULT 'PLANT-1',
  building              text, floor_level text, room_space text,
  manufacturer          text, model_no text, serial_no text,
  process_segment_class text,
  host_name             text,
  asset_ref                text,
  available_for_service boolean NOT NULL DEFAULT true,
  up_down_tracking      boolean,                    -- [UI], data unknown, nullable
  is_active             boolean NOT NULL DEFAULT true,
  weblink text, comments text, picture_url text, product text,
  -- Audit columns (AuditMixin; enforced by the single write path; ADR-005/016)
  created_at timestamptz, created_by text,
  updated_at timestamptz, updated_by text,
  source_actor text,         -- 'human:<id>' | 'agent:<name>' | 'mes-pipeline'
  proposed_by text, confirmed_by text   -- two-phase external confirmation (ADR-016); blank for direct writes
)

asset_type(code, label)            -- Production/Support/Jig/Meter/Computer
department(code, label)             -- the engineering / quality / shopfloor department codes
line(code, label)                   -- normalization removes the capitalisation problem
asset_subtype(code, label)          -- controlled lookup (created in the Inventory slice)
asset_part(asset_subtype, item)     -- N:M, unpacks the multi-valued inventory.asset_sub string (Inventory slice)

-- Asset composition graph (ADR-018; machine↔module tree + shared-resource graph) — built in this slice (migration 0009)
asset_relationship_type(code, label)  -- contains_module / shared_dependency
asset_relationship(
  id                bigserial PRIMARY KEY,
  from_asset_id     text NOT NULL REFERENCES asset(asset_id),  -- machine (contains) / resource (serves)
  to_asset_id       text NOT NULL REFERENCES asset(asset_id),  -- module (contained) / machine (served)
  relationship_type text NOT NULL REFERENCES asset_relationship_type(code),
  source            text NOT NULL,        -- mes_dependent_equipment | cmms_curated
  valid_from        timestamptz, valid_to timestamptz,         -- temporal (nullable = open)
  created_at timestamptz, created_by text, updated_at timestamptz, updated_by text,
  source_actor text, proposed_by text, confirmed_by text
  -- UNIQUE (from_asset_id, to_asset_id, relationship_type) WHERE valid_to IS NULL  -- partial: one active edge per (pair, type)
)

-- Cross-system identity crosswalk (ADR-015; canonical identity service)
asset_external_id(
  asset_id     text NOT NULL REFERENCES asset(asset_id),
  namespace    text NOT NULL REFERENCES external_id_namespace(code),  -- mes_equipment / layer_b_sensor / ...
  external_id  text NOT NULL,
  -- Audit (writes go through the domain service; reads are open to downstream consumers)
  created_at timestamptz, created_by text, source_actor text,
  PRIMARY KEY (namespace, external_id),          -- an external id maps to exactly one machine
  UNIQUE (asset_id, namespace, external_id)
)
external_id_namespace(code, label)   -- controlled: mes_equipment / layer_b_sensor / ...
```
