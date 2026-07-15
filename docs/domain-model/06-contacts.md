# CMMS Domain Model — 06. Contacts / Organizations & People

> Deliverable 1 (Extraction Spec), page 6 (final page). Sources: eMaint X4 UI exploration + the `contacts` export + cross-module validation.
> Status: **✅ Slice #6 is live and verified against a real PostgreSQL instance**. Migration 0006, `alembic check` clean, loaded
> **212 organizations (the distinct companies in the export + one seeded historical vendor) / 235 persons (2 duplicate records folded into aliases) / 2 person_alias rows**,
> 0 FK orphans. This document also **closes out** the vendor / supplier / person relationships accumulated in 02 / 03 / 04.
> Differences between the draft and the implementation (found by profiling) are tagged **[impl]** below.

---

## 0. Module scope and restructuring

The `contacts.csv` export is one flat table, but it **mixes two entities**: **organizations** (the `company` column) and **people** (everything else). The modernised system splits them:

```
Organization ──< Person          Person ──< PersonAlias (alias contactid → canonical)
     ▲              ▲
     │              │
 pointed at by   pointed at by
 Inventory /     WorkOrder /
 WorkOrder       ScheduledActivity
 (supplier /     (assigned person)
  contractor)
```

Data volume: a few hundred contacts, carrying one of three `category` values (Supplier / Employee / Customer), spread across a comparable number of `company` values — the export is dominated by supplier contacts.

**[impl] Profiling findings**: every row has a consistent column count (**no malformed RFC-4180 rows**, unlike inventory); encoding is latin-1 (only ß/ü, no cp1252-specific bytes, but a couple of rows need `html.unescape` for `&rsquo;`); **no company mixes categories** (each company has exactly one category) → `org_type` derivation is clean; all `contactid` values are distinct and non-null.

---

## 1. Entity: Organization

Extracted from `contacts.company` and classified by role.

| Target column | Source | Type | Required | Notes |
|---|---|---|---|---|
| `org_id` | (new surrogate key) **[impl] = slug of `company`** | string | ✅ PK | Uppercase, non-alphanumeric → underscore, truncated to 40. `CMA` and `SF` fall out naturally; **zero collisions** across the whole company list |
| `name` | [CSV] `company` (`clean_text`) | string | ✅ | one row per distinct company string |
| `org_type` | [CSV] `category`, derived + name override | string→FK | ⬜ | → `org_type` lookup (derivation rules below); nullable as an ingestion defence |
| `is_active` | (derived) | bool | ✅ (default true) | Contractor handover: `CMA` = current, `CMB` = historical. Inactive organizations exist only so that historical work orders still resolve |
| `website` | [CSV] `wweb` | string | ⬜ | **[impl]** org-level representative value: the non-empty value from the lowest `contactid` in that org (deterministic aggregation) |
| `address` | [CSV] `waddress` | text | ⬜ | same aggregation |
| `phone` | [CSV] `wphone` | string | ⬜ | same aggregation |

**[impl] `org_type` derivation rules** (`transform.derive_org_type`):
- **Name override wins first**: `SF` → `Internal` (the Shopfloor contacts carry `category=Customer`, but Shopfloor is the operating company itself).
- Otherwise derive from category: `Supplier` → `Supplier`; `Employee` → `Contractor` (in this CMMS, "Employee" means an outsourced operator); `Customer` → `Customer`.
- The lookup seeds the full 4-value enum (Supplier / Contractor / Customer / Internal, with labels). Observed distribution: nearly every organization is a Supplier, two are Contractors (`CMA` + `CMB`), one is Internal (`SF`); **no organization lands on Customer** (the only Customer-category company, `SF`, is overridden to Internal).

**Two contractor codes, one of them historical**: maintenance execution on this line is outsourced, and the contract has changed hands once. The data model has to carry both codes:

- `CMA` — the current vendor code, **active**; the people attached to it are the CMMS's live users.
- `CMB` — the earlier vendor code, **inactive**; it has no people in the contacts export. **[impl]** the loader's `SEED_ORGANIZATIONS` seeds it as a historical organization with **`org_type=Contractor`, `is_active=false`** (it is **not** in the contacts export), purely so that the `assignto` values on legacy work orders (see 02-WorkOrders) still resolve. **No `CMB` person records are created.**
- `SF` — the operating company itself (`Internal`).

These three are exactly the `assigned_vendor` values used in 02-WorkOrders / 03-ScheduledActivity. **[impl] note**: `assigned_vendor` currently FKs to the pre-existing `vendor` lookup (created by the SA/WO slices), **not** to this slice's `organization` table; the two tables coexist (`CMA` / `CMB` / `SF` appear in both). The ruling was that this slice builds the entities only and does **not** retrofit the FK (see §6).

---

## 2. Entity: Person

| Target column | Source | Type | Required | Notes |
|---|---|---|---|---|
| `person_id` | [CSV] `contactid` | string | ✅ PK | e.g. `SUPENG`, `JLEE` |
| `org_id` | [CSV] `company` (slugged) | string→FK | ✅ | → `Organization` |
| `category` | [CSV] `category` | string→FK | ⬜ | **[impl] stores the raw eMaint classification (Supplier / Employee / Customer); it is not reinterpreted as a role** — see below |
| `first_name` | [CSV] `fname` | string | ⬜ (93%) | |
| `last_name` | [CSV] `lname` | string | ⬜ (97%) | |
| `full_name` | [CSV] `fullname` | string | ⬜ (94%) | 13 rows missing; not always consistent with fname/lname |
| `email` | [CSV] `email` | string | ⬜ (94%) | |
| `work_phone` | [CSV] `wphone` | string | ⬜ (86%) | |
| `extension` | [CSV] `ext` | string | ⬜ (17%) | |
| `mobile` | [CSV] `mobile` | string | ⬜ (27%) | |
| `work_address` | [CSV] `waddress` | text | ⬜ (89%) | |
| `home_address` | [CSV] `haddress` | — | [DROP] | 1 row only, and it is garbage; dropped |

**[impl] Why `category` (the raw classification) instead of the `role` enum the draft proposed**: profiling showed that **every** contact with `category=Customer` has `company=SF` and an internal email address — i.e. they are internal staff (for example `JLEE` = Jordan Lee). Following the draft and mapping `Customer` → `CustomerContact` would have mislabelled internal staff as "customer contacts". Guardrail #8 (do not guess semantics) → **keep the raw `category` as truth** (via a `contact_category` lookup) rather than forcing a role that distorts it. `category` and `org_type` are not 1:1 (Employee → Contractor; `SF`'s Customer → Internal); each carries its own information.

**[impl] PII governance** (§3): `email` / `work_phone` / `mobile` / `extension` / `work_address` are PII. Bulk enumeration (REST `GET /persons` + MCP `list_persons`) returns a **non-PII summary only** (`person_id` / `org_id` / `category` / `full_name`); only the single-record read `GET /persons/{id}` (and MCP `get_person`) returns the full record — and it **automatically resolves an alias contactid back to the canonical person**.

---

## 3. Operations

CRUD (people, organizations). This is reference master data with a low change rate. **[impl] This slice shipped as a read slice**: get/list (organizations, people), alias resolution (`resolve_person`), org member listing; governed writes came later.

> **★ Suppliers/contacts are editable (live)**: admin governed writes were added — `update_organization` (change name / org_type / website / address / phone; **`org_id` is read-only** because it is the PK and is referenced by `person.org_id` and `inventory_item.supplier_org_id`; renaming `name` is safe because nothing FKs to it), `create_person` (attach to an existing org; synthesises a `PSN-xxxx` person_id), `update_person` (name / category / contact PII / `is_main`; `is_main` follows the "one per organization" rule — clear then set). All three call `assert_active_admin` (RBAC is enforced in the domain service, not merely hidden in the UI); **engineers are read-only here** (contact data is not engineering master data, so it does not go through the engineer proposal flow that inventory uses). PII governance is unchanged (details are admin-only).
>
> Note: on privacy grounds the new system must not collect or emit PII lists inside automated flows; agent access to person data is restricted. **[impl]** Implemented as "bulk = summary, single = full" (see §2).

---

## 4. Relationships

| Relationship | Target | Cardinality | Join key | Status |
|---|---|---|---|---|
| Person → organization | Organization | N : 1 | `person.org_id` | ✅ FK |
| Alias → canonical person | Person | N : 1 | `person_alias.person_id` | ✅ FK |
| Supplier ← part | Inventory | 1 : N | `inventory_item.supplier` (text) | ⏳ soft reference (not retrofitted) |
| Contractor ← work order / PM | WorkOrder / ScheduledActivity | 1 : N | `assigned_vendor` → `vendor` lookup | ⏳ coexists with `organization` |
| Assigned person ← work order / schedule | Person | 1 : N | `assigned_person` (text) | ⏳ soft reference (not retrofitted) |

---

## 5. Data quality findings / modelling clarifications

1. **What `category="Employee"` really means** — the "Employee" rows are attached to the contractor organization, not to the operating company. They are **the operating users of this CMMS** (production and maintenance); because those functions are outsourced, they are contractor staff. **This is not a data error; it is the outsourcing arrangement.** **[impl]** We do not build a role from the literal string `Employee`; `category` stores the raw value, `org_id` expresses actual affiliation, and `org_type` derives to `Contractor`.
2. **The absence of `CMB` people is correct, not a gap** — `CMB` is the historical vendor code; no person records for it exist in the export, and none are invented. **[impl]** The loader seeds `CMB` as an `is_active=false` historical Contractor organization (so that the `assignto` values on legacy work orders resolve); **no `CMB` person records are created**.
3. **Most `assignto` values do not resolve to a person** — fewer than half of the distinct `assignto` strings match a contact. That belongs to the 02/03 slices' `assigned_person` (a text soft reference); this slice does **not** retrofit an FK. Names from the earlier contractor era are kept as historical free text; station values such as `Wet Operator` do not get a Person (see §7 + ADR-017).
4. **★ Duplicate records (profiling found far more than the draft)** — the draft mentioned a single duplicate; profiling found **seven suspected duplicate pairs in three groups**:
   - **Group A (same company, near-identical)**: e.g. `SAMWU99` / `SMWU`, where the emails differ only in case, and a supplier pair whose email and phone are identical.
   - **Group B (different companies, same email)**: one person recorded under two companies.
   - **Group C (same name only, different email)**: almost certainly different people who happen to share a name.
   **The ruling was "be conservative"**: merge **Group A only** (write a `person_alias` row; the canonical record is the alphabetically-first contactid, e.g. `SAMWU99`; the alias does not get its own Person). **Groups B and C are kept as independent persons** — one person working with two companies is a real relationship; do not merge it and do not lose it.
5. **Low column fill rates** — `home_address` is essentially empty (dropped); `mobile`, `ext` and `wweb` are filled on a minority of rows.

---

## 6. ★ Cross-module close-out: vendor / supplier / person references ★

The "string, not FK" issues left behind by earlier documents were resolved with a ruling: **this slice builds the organization/person entities only; existing soft references are not retrofitted into FKs** (cheapest option; it does not disturb the loaders/tests of the first three slices — a dedicated reconciliation pass can be opened later if needed).

| Source column | Raw format | Current state | Follow-up |
|---|---|---|---|
| `WorkOrder.assignto` | `CMB (First Last)` | `assigned_vendor` → `vendor` lookup (FK already); `assigned_person` stays text | `CMB` now resolves (seeded by this slice) |
| `ScheduledActivity.assignto` | `CMA (First Last)` | same | — |
| `Inventory.supplier` | free-text company name | text soft reference (most values match `organization.name`) | retrofit deferred |
| `WorkOrder.opened_by` | includes values like `Wet Operator` | station/role, not a Person (ADR-017) | station lookup table pending UI extraction (C1) |
| `WorkOrder.closed_by`, `edituser` | system accounts | text; the **`app_user` table is deferred to the #4b write slice** | an account ≠ a contact; do not merge them |

> **[impl] Handled in the migration**: (a) `CMB` seeded as an `is_active=false` historical Contractor; (b) station operators do **not** get a Person (partially settled by ADR-017); (c) `app_user` (login account) and `Person` are separate concepts — `app_user` is **not** built in this slice, it is deferred to #4b.

---

## 7. Open / pending

- [~] **Production station operators** — values like `Wet Operator`, `10K Operator` are not people. **Partially settled (ADR-017)**: the reporter of a reactive WO does **not** get a Person; instead the report is attributed to a station/channel (`source_actor=agent:onbox` + `origin_station`). → The contacts slice does **not** create Persons for these station values. **Still open**: whether the station strings already present in historical eMaint `opened_by` values need a station lookup table, or stay as plain text (C1).
- [ ] Whether `assigned_person` on `CMB`-era work orders needs any preservation beyond the current text soft reference.
- [ ] `app_user` (login accounts) — deferred to the #4b write slice (only needed once `closed_by` / `edituser` system accounts must be attributed on write).
- [ ] Soft-reference FK retrofit (`inventory_item.supplier` → organization, `assigned_person` → person) — deferred.

---

## 8. MES pipeline anchor

People and organizations are reference data; their pipeline role is minor. The one hook: for work orders opened automatically by MES, `opened_by` must be a **system actor** (`source_actor = 'mes-pipeline'`) rather than a real person — consistent with the LLM-governance audit columns used system-wide.

---

## 9. Normalized target schema (PostgreSQL) — [impl] live (migration 0006)

```
org_type(code, label)                  -- Supplier / Contractor / Customer / Internal
contact_category(code, label)          -- raw eMaint classification: Supplier / Employee / Customer

organization(
  org_id     text PRIMARY KEY,         -- company slug (new surrogate key)
  name       text NOT NULL,            -- original company string
  org_type   text REFERENCES org_type(code),        -- derived + SF override; nullable (ingestion defence)
  is_active  boolean NOT NULL DEFAULT true,         -- CMB = false (historical vendor code)
  website    text, address text, phone text,        -- org representative values (non-empty value of the lowest contactid)
  + audit(created_at/by, updated_at/by, source_actor, proposed_by, confirmed_by)
)

person(
  person_id    text PRIMARY KEY,       -- contactid
  org_id       text NOT NULL REFERENCES organization(org_id),
  category     text REFERENCES contact_category(code),   -- raw classification (not reinterpreted as a role)
  first_name   text, last_name text, full_name text,
  email        text, work_phone text, extension text, mobile text,
  work_address text,
  is_main      boolean NOT NULL DEFAULT false,           -- ADR-026 (0018): one main contact per organization; RFQ recipients prefer its email
  + audit(...)
)

person_alias(                          -- conservative de-duplication: alias contactid → canonical person
  alias_contact_id text PRIMARY KEY,   -- e.g. SMWU
  person_id        text NOT NULL REFERENCES person(person_id),  -- e.g. SAMWU99
  + audit(...)
)

-- deferred: app_user (login accounts, #4b); supplier/assigned_person FK retrofit; station lookup table (C1)
```

---

## ✅ Deliverable 1 completeness

All six core domain-model documents are complete: 01-Assets, 02-WorkOrders, 03-ScheduledActivity, 04-Inventory, 05-Tasks, 06-Contacts. **All six read slices (#1–#6) are live and verified against a real PostgreSQL instance.**

**Outstanding extraction items** (per document §7): module-specific UI-only columns, MES up/down control columns (pipeline ADR stage), the station-operator lookup table (C1), and `app_user` (#4b).

**Next**: the **WorkOrder write slice (#4b)** — state machine + ADR-016 two-phase propose/confirm + ADR-017 Profile B on-box gated write + idempotency + downtime.
