# REQUIREMENTS — Scope and Requirements Specification

> What this system does, and why. For *how* it is built technically, see `ARCHITECTURE.md`; for the data model, see `docs/domain-model/`.

---

## 1. Background

Equipment maintenance at the Shopfloor Taiwan plant (PLANT-1 site) currently runs on **eMaint X4** — a cloud CMMS (computerised maintenance management system). Production and maintenance execution on the line are outsourced to a contract manufacturer (vendor code `CMA` in the data); an earlier vendor code (`CMB`) survives in the historical records and has to keep resolving.

As a legacy SaaS product, eMaint has real limits: a dated UI, no way to automatically pull production data from the plant MES, no programmatic operation by agent or API, and no fit with modern automation and governance needs. This project rebuilds it.

---

## 2. Source System: the Six eMaint X4 Modules

Scope covers six of eMaint's main modules (`Reservation Requests` is out of scope):

| Module | Purpose |
|---|---|
| **Assets** | Equipment asset master — machine number, class, name, attributes, in-plant location, supplier and serial number. |
| **Work Orders** | Work orders — PM work orders (auto-generated when a schedule falls due), corrective/breakdown work orders (raised by line staff), and other work orders such as engineering experiments and process adjustments. Closed on completion, with downtime settled. |
| **Scheduled Activity** | PM schedules — bind one asset to one maintenance task, with frequency, next due date, assignee, labour hours and consumables. |
| **Inventory** | Spare parts and production consumables — part number, quantity, storage bin, supplier, price, photo. |
| **Tasks** | Maintenance task templates — maintenance tasks recorded per machine class; each task carries a checklist of steps. |
| **Contacts** | Contacts — in-plant operators, supplier contacts, customer contacts. |

Entities, fields, relationships and target schema per module are detailed in `docs/domain-model/01–06`.

---

## 3. System Goals

The new system must deliver:

1. **A modern CMMS** — replace eMaint, covering the core functionality of the six modules above (asset register, work order lifecycle, PM scheduling, inventory, tasks, contacts).
2. **Automatic ingestion of MES production data** — integrate with the plant MES (a SQL Server-backed system with a read-only reporting database) so the two systems move together.
3. **Agent-accessible** — expose query and operation surfaces to LLM agents via MCP; ultimately forming a `cmms-mes-pipeline` capability.
4. **CLI-accessible** — provide a command-line interface.
5. **LLM-governance compliant** — every write is controlled, auditable, and attributable to an actor (human / agent / pipeline).
6. **Production-ready** — not a prototype; reproducible deployment, audit trail, and data-integrity guarantees.

---

## 4. MES Integration Requirements

The MES holds **absolute control** over physical production machines: to run production, a machine must first issue a production request to the MES and receive a response; the MES holds a field that controls the machine's up/down state.

The original `cmms-mes-pipeline` concept was bidirectional. **Following the ADR-011 ruling and the subsequent architecture convergence, this section has been substantially narrowed; the following is the version currently in force:**

- **MES → CMMS (read-only ingest, retained)**: the MES detects a yield anomaly or an equipment fault → a corrective work order is opened automatically in the CMMS (open only; production is never blocked). Firsthand ingestion is *not* built — MES-derived signals arrive second-hand via a downstream analytics consumer that already holds a governed read-only MES connection.
- ~~**State consistency**: keep the CMMS work order state machine in sync with the MES machine up/down field~~ — **rejected (ADR-011)**: the CMMS does not write the MES `Equipment Status`. An hour-latency channel would let the CMMS block a machine that has already been repaired — the tail wagging the dog. Authority over taking a machine down and bringing it back up stays with the operator on the floor and the MES; the CMMS observes the MES, it does not control it.

The full directional ruling is in ARCHITECTURE.md ADR-011.

---

## 5. Non-Functional Requirements

- **Auditability** — every data mutation traceable to who / when / what / why, plus actor type.
- **Data integrity** — foreign key constraints and state-machine transition rules enforced by the system.
- **Reproducible deployment** — infrastructure defined as code and version-controlled.
- **Data scale** — small: on the order of 700 assets, ~21,800 work orders accumulated over more than a decade, ~1,000 PM schedules, ~1,300 inventory items, ~440 task templates, a few hundred contacts. Performance is not the bottleneck; correctness and governance are. (These are migration counts. **None of that data is in this repository** — the demo runs on a synthetic dataset.)
- **Single site** — PLANT-1 only for now; the schema leaves room for multi-site expansion without over-engineering for it.

---

## 6. Constraints

- **The MES sits on the corporate intranet** — behind the corporate network boundary; any component that touches the MES must run inside it.
- **Solo development** — built and operated by one engineer (assisted by Claude Code); the architecture must keep operational burden low.
- **No framework or style constraints** — technology choice is open (see the proposal in ARCHITECTURE.md).
- **Existing assets** — the developer already has a read-only MES MCP; reuse and integrate it rather than rebuild it.

---

## 7. Out of Scope (for now)

- eMaint's `Reservation Requests`, `Purchase Orders`, `Reports`, `Dashboards` and similar modules.
- The remaining items on eMaint's top function bar.
- Multi-site; a native mobile app.
- Replacing any part of the MES itself — the MES remains the source of truth for machine control.

> **★ Scope change**: "Multi-language UI" was originally listed here as out of scope; it has since been **moved into scope** — the CMMS web UI defaults to English and can be switched per-user to **zh-TW / vi (Vietnamese)** with the choice persisted (the shop floor includes Vietnamese speakers). The agent that writes Jira MRQ tickets has a separate output-language preference (also English by default). See **ADR-023 (Internationalisation / Localisation)**.

---

## 8. Users

- **Operators** — responsible for line production and maintenance execution, supplied by the contract manufacturer; the primary day-to-day users of the CMMS.
- **Engineers** — raise engineering-experiment / process-adjustment work orders, analyse data.
- **Agents / automation** — query and execute governed operations via MCP; pipelines open work orders automatically.
