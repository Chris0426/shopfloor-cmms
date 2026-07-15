# CMMS Domain Model — 04. Inventory (Parts / Consumables)

> Extraction Spec, page 4. Sources: eMaint X4 UI exploration + the `inventory.csv` export (**every row loaded**; the handful of originally malformed lines are repaired automatically by the loader — issue A3b) + cross-module validation.
> Status: **v1.0 — the read slice for the entity itself (#5) is live and verified against real PostgreSQL** (migration 0005, `alembic check` clean, cp1252 + unescape for `™`/`μ`, the below-reorder-point list computed).
> Rulings: the `ES`/`EC` prefixes are used interchangeably (not a reliable classification) and cost is a single currency (USD); the canonical `asset_subtype` lookup (A3) folds obvious variants automatically, with a residue of cross-source values still to be clarified. **A3b closed**: the malformed lines (unescaped inch marks `"`) are repaired by `repair_malformed_line` (the raw file is left untouched), so no row is silently dropped.
> **Deferred**: uom (no data), Related Parts qty, cleaning up the asymmetric BOM edges.
> **Now live (superseding the corresponding "deferred" items above)**: `stock_transaction` writes (the ISSUE/RECEIVE/ADJUST ledger + `on_hand` movement, since #4b-1), the supplier↔organization link (0018 `supplier_org_id`; since 2026-07-04 `update_item` can set/clear it in the same transaction), the governed master edit `update_item` (an admin edits directly; an engineer's `update_item` becomes a **proposal**, ADR-025 Lane 1) and `adjust_on_hand` (a stock-count ADJUST — a reason is mandatory and the result may not go negative). The numeric columns (reorder_point/quantity, unit_cost, lead_time_weeks) are guarded in the domain layer against negative values.

---

## 0. Where this module sits

Inventory records the stock of **equipment spare parts** and **production consumables**. It does not hang off an individual asset; it is categorized by **asset sub-type** — one item can serve several sub-types. It is consumed both by WorkOrder (repair part issues / PART REQUEST) and by ScheduledActivity (maintenance consumables).

Data shape: on the order of a thousand stocked items, split across two item-code prefixes `ES` / `EC` — apparently a "part vs consumable" classification (**TODO confirm**).

---

## 1. Entity: InventoryItem

**[CSV]** = exported in `inventory.csv`; **[UI]** = needs extraction; **[→junction]** = a multi-valued column that must be split into a relation table.

### 1.1 Identity and description

| Target field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `item_code` | [CSV] `item` | string | ✅ PK | An `ES`/`EC` prefix + a serial number |
| `item_category` | [CSV] the `item` prefix | enum | ⬜ | `ES` / `EC` — apparently a part/consumable classification, **TODO confirm semantics** |
| `name` | [CSV] `sf_desc` | string | ⬜ (27%) | The shopfloor's internal standardized short name (e.g. "Fuse", "O-Ring") — sparsely populated, so it can never be relied on as the display name |
| `description` | [CSV] `descrip` | text | ✅ (99%) | The supplier's full product name; **⚠ contains HTML entities (`&#956;` = μ) and latin-1 `™`; must be cleaned** |
| `vendor_part_no` | [CSV] `vpartno` | string | ⬜ (95%) | Supplier part number |
| `weblink` | [CSV] `weblink` | url | ⬜ (32%) | Product page link |
| `photo_ref` | [CSV] `photo` | string | ⬜ (78%) | Not a file path — an **HTML fragment** wrapping an opaque internal eMaint document id (`<img src=…>`). **The actual image file lives in the eMaint backend and must be extracted separately as binary** |
| `comment` | [CSV] `comment` | text | ⬜ (27%) | |

### 1.2 Stock and replenishment

| Target field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `quantity_on_hand` | [CSV] `onhand` | decimal | ✅ | Quantity on hand; **has decimals** → some consumables are measured by weight/volume, so an integer column would be wrong |
| `reorder_point` | [CSV] `orderpt` | decimal | ✅ | Reorder point; `onhand < orderpt` → needs replenishment (the low-stock list is derived, never stored) |
| `unit_of_measure` | [UI] | string | ⬜ | **Unit of measure (shown as "Unitms" in the eMaint Related Parts view)** — not exported; because `onhand` has decimals this column is essential and must be extracted |
| `lead_time_weeks` | [CSV] `lead_time` | integer | ✅ | Procurement lead time, a small integer. **The unit is presumed to be weeks, TODO confirm** |
| `is_stocked` | [CSV] `stock` | bool | ✅ | Whether it is a regularly stocked item |
| `is_obsolete` | [CSV] `obsol` | bool | ✅ | Discontinued. **The two flags are independent**: some items are both stocked and obsolete (being phased out), so the model must not collapse them into one status |

### 1.3 Cost and location

| Target field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `unit_cost` | [CSV] `cost` | decimal | ✅ | Unit price. The distribution is **extremely long-tailed** — most items are cheap consumables and a few are capital spares orders of magnitude more expensive — so the column needs real precision (`numeric(14,5)`), not a money type sized for the median. **Currency unknown, TODO confirm** |
| `bin_location` | [CSV] `location` | string | ⬜ (60%) | Storage bin (shelf codes such as `02B`, `15C`; `Drawer` is a catch-all bin holding a large share of the items). **This is now a controlled vocabulary** (FK semantics against `storage_bin`, see §1.7): on edit only an active value or blank is accepted, and the write path normalizes case. **Exception (keep-unchanged)**: an existing item holding a legacy dirty value (a CSV column-shift artefact) that is **left unchanged during an edit** is passed through as-is, so it does not block an unrelated edit to another field |

### 1.4 Supplier

| Target field | Source | Type | Required | Notes |
|---|---|---|---|---|
| `supplier_id` | [CSV] `supplier` | string→FK | ⬜ (93%) | **Should be normalized**: the large majority of the distinct supplier names match a supplier company in Contacts exactly → turn this into an FK to a shared `Company`/`Supplier` entity (see §4 and 06-Contacts). The few that do not match are left unlinked rather than fuzzy-matched |

### 1.7 The `storage_bin` controlled vocabulary

`bin_location` changed from free text to a controlled vocabulary. The list of physical bins in the warehouse is seeded in migration **0028** from the warehouse's own bin list (shelf codes such as `01`, `02A`, plus named bins like `Drawer`) — character-for-character, case preserved, and **deliberately excluding the dirty values found in the data** (a bare, malformed code that was clearly a CSV artefact rather than a real bin).

| Column | Type | Notes |
|---|---|---|
| `code` | string PK | The bin code *is* the human-readable value (there is no label); format `^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$` |
| `is_active` | bool | Additive-only: retirement is governed by an admin (deactivating stops it being selectable; existing items are unaffected). Rows are **never deleted** |

Governed operations (admin-only, single write path, mirroring `wo_hold_reason`): `add_storage_bin` (strip + format validation + case-insensitive duplicate check — colliding with a deactivated code returns an honest hint pointing to `/admin/vocab` to reactivate it) and `set_storage_bin_active` (the enable/disable toggle). Entry points: the storage-bin section of `/admin/vocab`, plus the combobox quick-add inside the item edit form (`POST /app/inventory/bins`).

**Write-path validation (`_validate_bin_location`)**: blank → None; equal to the item's current value → **passed through as-is** (legacy dirty values — CSV column-shift artefacts — must not block unrelated edits to other fields); otherwise it is matched case-insensitively against the **active** `storage_bin` rows, returning the canonical code on a hit (normalizing case, e.g. `drawer` → `Drawer`) and rejecting if there is no match. This is wired into `create_item` (current value = None) and `_update_item_impl` (current value = `item.bin_location`); because that core is shared by both the admin's direct edit and the proposal-confirm path, **confirmed proposals are validated automatically too**.

---

## 2. Operations (CRUD + actions)

CRUD; adjusting stock levels (receipt / issue / stock count); marking an item obsolete; purchasing (linking to a Purchase Order — a module outside the scope of these six).

> Modernization notes: stock movements must go through audited domain operations (`receive_stock`, `issue_stock`, `adjust_stock`), each recording who / why / against which document. Never a naked UPDATE of `onhand`.
>
> **★ Issue attribution (ADR-024, live)**: an issue is **not hard-bound to a work order** — the charge target is binary, **WorkOrder | Asset** (in cmms a part is always related to equipment, so there is no cost-center concept). Repairs and PMs go through a work order (`WorkOrderService.issue_part_to_work_order`); non-work-order direct issues go through `InventoryService.issue_to_asset` (attributed via `stock_transaction.charge_target_asset_id`, deducting `on_hand`, creating no `work_order_part` row). A DB CHECK guarantees that `kind='ISSUE'` has exactly one charge target (no orphans, no double attribution).
>
> **★ The item-edit batch (2026-07-05, live)**: ① the UI labels for "Shopfloor description" (`name` = sf_desc) and "supplier description" (`description` = descrip) were corrected (in all three locales); ② the `supplier` text field is now restricted to existing values — `update_item` validates that the given supplier matches an existing `organization.name` (case-insensitively) or an existing `inventory_item.supplier` (covering legacy names not yet linked to an org, plus the item's own current value), rejecting anything else; the web edit form autocompletes via `data-suggest=supplier`, and selecting a value populates a **read-only** "supplier org code" field (nobody types the code by hand); ③ **the applicable machine sub-types are editable**: `set_applicable_subtypes(item_code, subtypes, actor)` (admin-governed, multi-select, overwriting the `inventory_item_asset_subtype` junction; every code must be an existing canonical `asset_subtype`; fully audited); ④ the detail page hyperlinks alternates and kit children, and adds a **reverse lookup of the parent kits** (`get_parent_kits` — the kit edge has a reverse face from parent→child); ⑤ the stock-count reason field became "common values dropdown + free text" (a datalist; the value remains free text and is recorded in the audit trail — it is deliberately not an enum).

---

## 3. Workflow: the replenishment cycle

Inventory has no rich state machine — only the `is_stocked` / `is_obsolete` flags. The "process" is the replenishment cycle:

```
  issue / consumption (WorkOrder part issue, ScheduledActivity consumables)
        │  onhand decreases
        ▼
  onhand < reorder_point  ──▶  triggers replenishment (purchase)
        │  goods arrive after lead_time_weeks
        ▼
  receipt: onhand increases  ──▶ (back to the top)
```

---

## 4. Relationships to other entities

| Relationship | Target | Cardinality | Join key | Validation |
|---|---|---|---|---|
| Applicable asset sub-types | Asset (via sub-type) | **N : M** | `asset_sub` (comma-separated) | Nearly every item carries a value, and a sizeable minority map to **several** sub-types (up to ~16) → it must be split into a junction |
| Kit parent | InventoryItem (self) | N : 1 | `parnt_item` | ✅ 0 orphans |
| Kit children | InventoryItem (self) | 1 : N | `child_item` (comma-separated) | Contains an orphan edge, and the edge count is **asymmetric** with `parnt_item`, so **two-way consistency needs cleaning up** |
| Alternates | InventoryItem (self) | N : M | `alt_item` (comma-separated) | ✅ 0 orphans |
| Supplier | Company / Supplier | N : 1 | `supplier` | Most names match against Contacts |
| Repair issues | WorkOrder | N : M | `stock_transaction.work_order_no` (PART REQUEST / work-order parts) | Usage detail still to be extracted, 02-WorkOrders §7 |
| Direct issues | Asset | N : M | `stock_transaction.charge_target_asset_id` (non-work-order direct issue, ADR-024) | An ISSUE carries exactly WO xor Asset; a direct issue deducts `on_hand` and creates no `work_order_part` |
| Maintenance consumables | ScheduledActivity | N : M | (consumables) | Still to be extracted, 03-ScheduledActivity §7 |

---

## 5. Data-quality issues (migration must handle these)

1. ✅ **Malformed CSV lines** — a handful of rows carry an **unescaped inch mark `"`** inside `descrip` (e.g. `3/8"`, `.046"`) → RFC-4180 column shift, wrong field count. **A3b closed (option A adopted)**: the loader's `repair_malformed_line` re-anchors on "the first 4 fields + the numeric/flag block after `location`" and repairs the row automatically, keeping `descrip` verbatim (the raw CSV is untouched; the repair logic is in git, where it is reviewable). **No row is dropped and none is hand-edited.**
2. ✅ **Encoding** — read as **cp1252** (a Windows export; `™` = \x99 requires cp1252) + `html.unescape` (`&#956;` = μ). Verified against real PostgreSQL: a supplier product name containing both `™` and `0.8 μm` round-trips correctly.
3. 🔶 **`supplier` is free text** — **stored as a text soft reference in this slice**; the FK→`company` normalization is deferred to the Contacts slice (#6).
4. ✅ **Multi-valued columns** — `asset_sub` / `alt_item` / `parnt_item`+`child_item` are split into 3 junctions (`inventory_item_asset_subtype` / `_alternative` / `_kit`); orphan edges (pointing at items that were not loaded) are skipped.
5. 🔶 **`photo`** — stored as `photo_ref` (the eMaint doc id string); extracting the real binary images is deferred.
6. ✅ **`parnt_item` / `child_item` asymmetry** — kits are rebuilt as a **de-duplicated two-way union** (parnt→self plus self→child); self-loops and orphans are skipped. Further two-way consistency cleanup is deferred.
7. ⏳ **UOM missing** — [UI], no data → **the column is not created**, pending extraction (I4).

---

## 6. MES pipeline anchors

Inventory is peripheral to the core MES↔CMMS work-order pipeline, but there are two forward-looking touch points:

| Mechanism | Notes |
|---|---|
| **Consumable-usage forecasting** | Production consumables deplete with output. MES knows the output → it can feed forward a consumption forecast and drive automatic replenishment, instead of relying only on the passive `reorder_point` trigger |
| **Work-order part linkage** | Parts issued when a work order is completed → `onhand` decreases. When the pipeline opens a work order automatically, an estimated part usage could also reserve the stock |

> Note: this module has no MES up/down state coupling (that is WorkOrder's responsibility).

---

## 7. Next: extraction outstanding for this module

- [ ] `unit_of_measure` (Unitms) — the unit-of-measure column, [UI], pending extraction (I4; the column is not created)
- [x] ~~`item_category` (ES/EC) semantics~~ — answered (I1): the prefixes are used interchangeably and are not a reliable classification
- [x] ~~`unit_cost` currency~~ — answered (I2): a single currency, USD; the unit of `lead_time` is still unconfirmed (I3, the column is provisionally named `lead_time_weeks`)
- [ ] min/max stock levels, on-order quantity — not in the export, deferred (I5)
- [ ] The Asset "Related Parts" Qty attribution — [UI], no data; the junction has no qty column for now (I6)
- [ ] **★ The asset sub-types with no cross-source match** — to be clarified one by one; once confirmed, extend `ASSET_SUBTYPE_ALIASES` and re-run
- [x] **Malformed lines** — ✅ closed (A3b): repaired automatically by the loader (unescaped inch marks `"`), no row lost, verified against real PostgreSQL

---

## 8. Target schema (Postgres) — ★ slice #5 is live (migration 0005)

```
inventory_item(                                       -- migrations/versions/20260621_0005_inventory.py
  item_code          text PRIMARY KEY,
  item_category      text REFERENCES item_category(code),   -- ES/EC (legacy prefix, used interchangeably)
  name               text,                                  -- sf_desc (cleaned)
  description        text,                                  -- ★ changed: nullable (defensive; descrip, cleaned)
  vendor_part_no     text,
  quantity_on_hand   numeric(12,3),                          -- ★ changed: nullable (defensive; do not force a 0)
  reorder_point      numeric(12,3),                          -- ★ changed: nullable
  reorder_quantity   numeric(12,3),                          -- ★ ADR-026 (0018): orderqty; if absent the RFQ falls back to reorder_point − on_hand
  lead_time_weeks    integer,                                -- unit unconfirmed (I3)
  unit_cost          numeric(14,5),
  currency           text NOT NULL DEFAULT 'USD',            -- I2: a single currency, USD
  bin_location       text,                                  -- ★ controlled vocabulary (storage_bin; a soft code reference validated on the write path, see §1.7)
  supplier           text,                                   -- free-text supplier name (legacy)
  supplier_org_id    text REFERENCES organization(org_id),   -- ★ ADR-026 (0018): supplier→org link (matched by name; no match → NULL = RFQ-ineligible)
  weblink            text, photo_ref text, comment text,
  is_stocked         boolean NOT NULL DEFAULT true,
  is_obsolete        boolean NOT NULL DEFAULT false,
  -- Audit (AuditMixin / ADR-005/016)
  created_at timestamptz NOT NULL DEFAULT now(), created_by text,
  updated_at timestamptz, updated_by text,
  source_actor text, proposed_by text, confirmed_by text
  -- ★ Not created: unit_of_measure (I4, no data)
)

item_category(code, label)                          -- ES / EC (legacy prefix)
asset_subtype(code, label)                          -- ★ A3 canonical (shared reference for asset + inventory)
inventory_item_asset_subtype(item_code, asset_subtype)  -- N:M applicable sub-types (FK→asset_subtype)
inventory_item_alternative(item_code, alt_item_code) -- alternates, N:M (self-referencing)
inventory_item_kit(parent_item_code, child_item_code) -- kit BOM (self-referencing; ★ changed: qty deferred, no data)
storage_bin(code PRIMARY KEY, is_active boolean NOT NULL DEFAULT true)  -- ★ the storage-bin controlled vocabulary (0028; seeded from the warehouse bin list; bin_location is a soft reference, see §1.7)
-- ★ Deferred: uom(code, label)
-- stock_txn_kind / stock_transaction: already built (canonical schema in 02-work-orders §8;
--   ADR-024 adds charge_target_asset_id → a direct issue is charged to the asset, see §2 above)
-- ★ Backfilled historical issues can be neither re-quantified nor cancelled: the backfill ledger entries
--   (source_actor='human:data-migration' = BACKFILL_ACTOR) were booked with adjust_on_hand=False and
--   **never deducted on_hand**; cancelling or re-quantifying them would book a RETURN and inflate on_hand
--   out of thin air. InventoryService.cancel_asset_issue / update_asset_issue_quantity and
--   WorkOrderService.cancel_part_issue / update_part_issue_quantity therefore all block them explicitly
--   on that marker (`_assert_not_backfill_issue`); the UI also hides the re-quantify/cancel buttons on
--   backfilled rows.
-- asset.asset_subtype: stays a text soft reference to asset_subtype.code (the FK is not retrofitted, to avoid
--   breaking the Asset slice)
```
