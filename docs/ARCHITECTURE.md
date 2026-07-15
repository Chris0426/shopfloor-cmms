# Architecture

> The architecture record for the CMMS modernisation project — the replacement of a legacy commercial CMMS (eMaint X4) with a modern, governable, production-ready system for the Shopfloor Taiwan plant (site `PLANT-1`). It opens with the system overview and the foundational decisions: the headless domain core, the write-path and audit rules, the MCP tool boundary, the technology stack, the hosting platform, and the MES integration interface. The later ADRs cover the cross-system asset identity service, gated writes, on-box reporting, agent governance, and the delivered application surfaces. Decisions are recorded as ADRs. Status markers: **Accepted** = decided; **Proposed** = proposed, pending confirmation; **Open** = undecided.

---

## 1. Overview

```
   Cloud (Fly.io sin region, public HTTPS, ADR-013 Tier B)
   ┌──────────────────────────────────────────────────────────────────┐
   │  Fly app                                                         │
   │  ┌─────────────────────────────────────────────────────────────┐│
   │  │  App Machine (FastAPI + MCP + pipeline worker)               ││
   │  │  ┌─────────────┐ ┌──────────┐ ┌─────────────┐              ││
   │  │  │  MCP tools  │ │   CLI    │ │ future Web  │              ││
   │  │  └──────┬──────┘ └────┬─────┘ └──────┬──────┘              ││
   │  │         │ (thin clients — no business logic) │              ││
   │  │         └────────────┬───┴────────────┘                      ││
   │  │                      ▼                                       ││
   │  │            ┌───────────────────┐                             ││
   │  │            │   Domain Service   │ ← sole write path + audit  ││
   │  │            └─────────┬─────────┘                             ││
   │  │                      ▲ ▼                                     ││
   │  │            ┌─────────┴──────────┐                            ││
   │  │            │  pipeline worker   │ ← scheduled, via domain svc││
   │  │            └────────────────────┘                            ││
   │  └──────────────────────│──────────────────────────────────────┘│
   │                         │ *.internal private network             │
   │  ┌──────────────────────▼──────────────────────────────────────┐│
   │  │  Postgres Machine (self-managed PG 16/17 + persistent volume)││
   │  │  ┌───────────────────┐                                       ││
   │  │  │   PostgreSQL      │ ← system of record (daily pg_dump)    ││
   │  │  └─────────┬─────────┘                                       ││
   │  └────────────│──────────────────────────────────────────────────┘│
   │               ▼ pg_dump (cron)                                    │
   │       ┌───────────────┐                                           │
   │       │ Cloudflare R2 │ ← 30-day backup retention (IaC, cron)     │
   │       └───────────────┘                                           │
   └──────────────────────┬───────────────────────────────────────────┘
                          │ FTP (IP whitelist)  ★ ADR-014: hourly batch
                          ▼
                 ┌───────────────────┐
                 │  Shopfloor DMZ FTP│   B2MML Equipment XML
                 └────────┬──────────┘   (the sole CMMS↔MES interface)
                          │
                 ┌────────▼────────┐
                 │   MES nodes     │   internal network, hourly drop directories cycle
                 └─────────────────┘

   Developer workstation (unchanged, personal use):
     A pre-existing read-only MES query tool — used only for interactive queries
     from a desktop agent. It takes no part in the production
     pipeline and is decoupled from the cloud architecture.
```

---

## 2. Architecture Decision Records

### ADR-001 — API-first headless core ‹Accepted›

Domain logic is concentrated in a single headless domain service, which is the **sole write path**. MCP, the CLI, and any future web UI are thin clients containing no business logic.

**Rationale**: logic is not duplicated across surfaces; the state machine and validation rules are implemented exactly once. This is the precondition for a system that is both governable and production-ready.

### ADR-002 — PostgreSQL as the system of record ‹Accepted›

**Rationale**: the dataset is small but highly relational, with foreign-key and state-machine requirements. Postgres is mature, strong on constraints, and cheap to operate.

### ADR-003 — MCP tool boundary = domain operations, not raw SQL/CRUD ‹Accepted›

MCP tools are **domain operations** such as `close_work_order`, `schedule_pm`, `get_asset` — never `run_sql` or `update_table`.

**Rationale**: an agent must not hold arbitrary mutation capability. Governance needs something to grip; domain operations give it a boundary. This continues the SELECT-only philosophy of the pre-existing MES MCP.

### ADR-004 — Read/write separation and governed writes ‹Accepted›

Read tools are open. Write tools must support dry-run plus explicit confirmation. High-risk operations (voiding, bulk deletes, key changes) are not exposed to agents at all.

**Rationale**: reads are low-risk; writes require a human, or an explicit grant of authority.

### ADR-005 — Single write path + full audit ‹Accepted (2026-06-20: audit columns extended by ADR-016)›

Every mutation goes through the domain service and writes an audit record: who / when / what changed / why, plus `source_actor` ∈ {`human`, `agent:<name>`, `mes-pipeline`}. Quantities such as stock on-hand may never be updated in place; they are derived from movement records (see `stock_transaction` in domain model 04).

**Rationale**: this is the core of LLM governance — behaviour must be traceable and attributable.

**2026-06-20 extension (ADR-016)**: when a write travels the two-phase "propose → externally confirm" path, a single `source_actor` column cannot express "**agent proposed, human confirmed**". The audit columns are therefore extended with `proposed_by` (e.g. `agent:<name>`), `confirmed_by` (`human:<id>`), and proposal/confirmation timestamps. The final `source_actor` takes the **confirmer**: ultimate accountability belongs to the person who pressed confirm, while `proposed_by` preserves the origin of the proposal for traceability. Direct writes (human GUI, mes-pipeline) keep a single `source_actor` and leave `proposed_by` / `confirmed_by` null. See ADR-016.

### ADR-006 — Idempotency ‹Accepted›

Writes triggered by the pipeline or by an agent — automatic work-order creation above all — must carry an idempotency key (e.g. the MES event id). A retry must never produce a duplicate record.

**Rationale**: in any distributed integration, automation and retries are inevitable.

### ADR-007 — LLM-readable schema metadata ‹Accepted›

Every entity, field, and enum carries a natural-language description so an agent can reason about it correctly. This reuses the YAML schema-metadata pattern from the existing MES project; the two projects can share the mechanism.

**Rationale**: to operate correctly, an agent needs the *semantics* of the schema, not just the column names.

### ADR-008 — Deployment: self-hosted on a VPS, not a BaaS ‹Superseded by ADR-013 (2026-05-24)›

~~The CMMS core (Postgres + domain service) is self-hosted with Docker on the developer's existing VPS; if hand-managing the database proves painful, switch to that provider's managed Postgres. Public-cloud BaaS offerings (Supabase et al.) are rejected.~~

**Why superseded**: the original ADR rested on three premises that later turned out to be false.

1. *"The MES lives on the internal network and must never be exposed to the cloud."* — Untrue. The MES ships an official third-party integration path (DMZ FTP + B2MML XML; see ADR-014), the plant's equipment data has already run in the cloud for years, and no security policy forbids it.
2. *"Self-hosting on a VPS is the opposite of a public-cloud BaaS."* — A false dichotomy. A VPS *is* public cloud. The real criterion is **whether the control plane lives in git, or whether the platform forces web-console clickops** — not "self-hosted vs managed".
3. *"The existing VPS is the ready-to-hand choice."* — Measured in practice: **the provider's IP ranges were not reachable from one of the networks the system has to serve**, making it unusable outright.

See ADR-013 for the replacement criteria and decision. The core spirit of ADR-008 — *IaC lives in git; the AI does not act as the production operator* — still holds and is preserved separately as ADR-009.

### ADR-009 — Infrastructure as Code, in git ‹Accepted›

Infrastructure is defined declaratively (Docker Compose / IaC) and version-controlled; deployments are reproducible and reviewable. **The AI writes the IaC; the AI is not the production operator.**

**Rationale**: "let an AI client click around a platform console and run things" leaves infrastructure state opaque and irreproducible, which is incompatible with both production-readiness and governance.

### ADR-010 — cmms-mes-pipeline: an orchestration layer at first, not a separate MCP ‹Accepted›

The existing MES MCP stays read-only and untouched. A new CMMS MCP is built (read + governed write). The pipeline starts life as an orchestration component (a job/service): read via the MES MCP → transform → write through the CMMS domain service. Once the interface has stabilised, higher-level tools may be exposed as a pipeline MCP. Any component that touches the MES runs inside the internal network.

**Rationale**: validate the orchestration logic before freezing an interface around it; avoid premature abstraction.

### ADR-011 — WorkOrder ↔ MES up/down status coupling ‹Partially Resolved (2026-06-26): MES→CMMS read-only ingest; the CMMS→MES control direction is rejected›

The WorkOrder state machine and the MES machine `Equipment Status` (`In Service` / `Out of Service`) form a distributed pair of states that ought to stay consistent (under repair/maintenance ↔ Out of Service; work complete ↔ In Service).

**The MES control surface, as established during interface review (2026-05-24)**:

- The controlled field is the equipment status property in the B2MML `Equipment` document, valued `In Service` / `Out of Service` (B2MML is the public ISA-95 XML standard).
- The MES also offers real-time internal interfaces on its own network — **which we deliberately do not use** (guardrail #7: the CMMS touches the MES only through the documented third-party integration path).
- The only path available to us is therefore the **DMZ file drop** (hourly batch; see ADR-014): a B2MML `Equipment` document is deposited and picked up on the MES's own cycle.

**The hard constraint imposed by ADR-014**: once the DMZ FTP path is adopted, latency is measured in hours. **A real-time interlock is therefore not achievable from the CMMS side** — this is not a design trade-off, it is the physics of the interface.

**★ 2026-06-26 ruling — do not build a write-side interlock in which the CMMS controls MES machine run-ability**:

- **The fatal mode**: if "close the work order → CMMS writes `In Service` back to the MES (machine released)" travels an hours-latency channel, you get a machine that is repaired and a work order that is closed, yet the machine cannot produce for tens of minutes because the write has not landed. **The maintenance system would be blocking production — the highest priority — which inverts the whole point.** The downtime direction has the same defect: latency destroys the interlock's value, and it risks stopping a healthy machine.
- **Direction of convergence**: ✅ **MES→CMMS read-only ingest** — (i) an MES fault event automatically opens a reactive work order (opens a ticket, never blocks production); (ii) reading MES production calendar/status lets us compute downtime accurately. ❌ **The CMMS→MES control direction is rejected**: the CMMS does not set `Equipment Status`. **Authority over stopping and releasing a machine stays with the operator on the floor and with the MES.** If we ever do want to write towards the MES, it may only be advisory (the MES *displays* the work-order state and a human decides) — never gating production.
- The original goal ("WO status ↔ MES up/down stay consistent") is downgraded to "the CMMS observes the MES; it does not control the MES", which is consistent with the project's MES-boundary guardrail.

**Still open (to be settled during the pipeline phase)**:

- Whether the MES is configured to export Equipment status changes to the DMZ drop at all. If it is not, the reactive direction (MES → CMMS opens a ticket) needs another mechanism: email parsing, or asking IT to enable the export.
- ~~How to build the mapping table from MES equipment id to `Asset.asset_id`.~~ → **Resolved (2026-06-20)**: the MES equipment id and `asset_id` are **the same identifier for the same thing** (identity; both are `EID-xxxxx`). The mapping is an identity function: **no** crosswalk rows and no mapping table are required, and the pipeline joins the MES directly on `asset_id`. The `asset_external_id` crosswalk (ADR-015) is reserved for systems whose identifiers *differ* from ours.

### ADR-012 — Technology stack ‹Accepted›

**Python 3.12** throughout:

- **Domain service + HTTP API**: FastAPI (async, pydantic v2 schema validation).
- **MCP server**: the official [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk), Streamable HTTP transport (externally reachable) plus optional stdio (local development and testing).
- **ORM + migrations**: SQLAlchemy 2.x + Alembic.
- **Lint + format**: ruff (replacing the black + isort + flake8 trio).
- **Testing**: pytest + pytest-asyncio + httpx (API tests) + testcontainers-postgres (integration tests against a real Postgres).
- **Container**: Docker (multi-stage), base image `python:3.12-slim`.

**Rationale**:

- Same language (Python) as the existing MES tooling: knowledge transfers, and the two projects can share modules such as schema metadata.
- FastAPI + SQLAlchemy + Alembic + pydantic v2 is the mainstream modern Python backend combination — mature docs, mature community, few surprises.
- The official MCP SDK is Anthropic-maintained, and the Python version supports Streamable HTTP — the officially recommended transport for exposing tools to remote agents. That aligns directly with this project's "cloud-hosted, agents connect from outside" architecture.
- Fly.io treats Python + Docker as a first-class citizen; `flyctl launch` generates a working Dockerfile and `fly.toml`.
- ruff is 10–100× faster than the trio it replaces — a material quality-of-life gain for a single-maintainer project.

**Alternatives rejected**: Node/TypeScript (would fragment knowledge shared with the MES MCP); Go (less mature MCP SDK, and not the developer's primary language); Rust (overkill — pure cognitive overhead for a one-person project).

### ADR-013 — Hosting platform: Fly.io, Singapore region (supersedes ADR-008) ‹Accepted — corporate-network reachability tests passed 2026-05-27; the contractor-site network is being unblocked through that site's own IT process›

The whole CMMS deploys to a single **Fly.io** app in the `sin` region. The app contains **two Machines**:

- **App Machine** — FastAPI domain service + MCP server + pipeline worker (shared-cpu-1x, 512 MB–1 GB).
- **Postgres Machine** — **self-managed** Postgres 16/17 on its own Machine with a persistent volume, exposed only on the Fly private network (`*.internal`) to the App Machine.

Every environment is reproducible from git via `fly.toml` + `Dockerfile` + Alembic migrations + the `pg_dump` backup IaC. **No production configuration is ever changed through the web console.**

**Criteria (replacing ADR-008's false "self-hosted vs managed" dichotomy)**:

1. **The control plane lives 100% in git** — IaC, Dockerfile, migrations, backup scripts, and the secrets manifest all live in the repo; changes go through PR + CI; the web console is read-only.
2. **Low vendor lock-in** — standard Postgres and standard Docker only. No platform-specific Auth, Edge Functions, or BaaS DSL.
3. **Reachability** — public HTTPS is reachable from the networks the users actually sit on, and the platform can offer a dedicated egress IPv4 for the cases where an outbound integration has to be allow-listed on the other side.
4. **Operable by one person** — the Fly Machine model is simple, `flyctl deploy` is smooth, and the backup script is written once and left to cron.

**Why Tier B (self-managed PG) rather than Fly Managed Postgres**:

- Actual load is very low (~30 writes/week plus 168 pipeline polls; the database is projected under 100 MB), so the operational burden of a self-managed Postgres is close to zero.
- Cost difference: Tier B ≈ $8–10/month, Managed PG (Tier C) ≈ $15–25/month. For a single-maintainer project, $10/month is a real difference.
- Backups are `pg_dump | gzip` pushed to **Cloudflare R2** (or an equivalent object store), on a daily schedule, in a script under 30 lines, with the IaC in git.
- **Crucially, Tier B and Tier C are mutually reversible.** If the self-managed database ever becomes a burden (data growth, a need for PITR or replicas), the migration is `pg_dump` → `pg_restore` into Fly Managed PG, change the connection string, delete the old Machine — **zero application code changes**, because both ends are a standard Postgres URL. This is "save money now, buy peace of mind later", not an irreversible commitment.

**Why Fly.io over the alternatives**:

- **The incumbent VPS provider** — its IP ranges are not reachable from one of the networks the system must serve. Eliminated outright.
- **Render** — a peer candidate with friendlier PR previews, but its regions are further from Taiwan. **Kept as Plan B if Fly ever becomes unreachable.**
- **AWS/GCP/Azure** — overkill for one operator, with complicated billing.
- **Supabase / Neon** (database only) — one more vendor, a cross-platform connection, and governance discipline split across two places. This project is too small to justify it.
- **An internal-network VM** (provided by Shopfloor IT) — the long-term ideal, but it depends on IT alignment. Not blocking today; retained as a long-term upgrade path.

**Mandatory discipline (inheriting the governance spirit of ADR-009)**:

- Production configuration must never be changed from the Fly web dashboard (scale, env, secrets — all from IaC, apart from the initial `set` at deploy time).
- All schema changes go through Alembic + PR. No ad-hoc DDL against the database.
- Secrets are injected with `flyctl secrets set`. **Values** never enter git, but the **key list and its provenance** are documented in the infra secrets manifest.
- **Backup discipline**: the scheduled `pg_dump`, the R2 upload, and the retention policy (e.g. last 30 days) are all IaC. A restore is rehearsed monthly (one `pg_restore` onto a throwaway Machine) — *a backup that has never been restored is not a backup.*

**Reachability**: the platform must be reachable from every network the users actually sit on. That was tested before committing to it, and one of those networks — the contractor site's — has to be opened through its own IT process. The detailed results are an operational matter and are not published here.

**Conclusion, at two levels**:

- **Platform choice**: the corporate paths are reachable → stay on Fly.io; do not move to Render.
- **Go-live**: **the day-to-day, high-frequency users sit at the contractor site** (the plant owns, develops and administers the system, but is a low-frequency user). The system cannot be delivered until that network can reach it. This is an **independent milestone: it does not block the scaffold, but it does block go-live.** If the path cannot be opened, the fallbacks are: contractor staff connect over VPN into the corporate network; the Render Plan B; or the internal-VM upgrade path.

### ADR-014 — Pipeline ↔ MES integration interface: DMZ FTP B2MML (hourly batch) ‹Accepted›

**Decision**: between `cmms-mes-pipeline` and the MES, we use **only the MES vendor's documented third-party integration path** — a DMZ file drop carrying B2MML XML documents. We do not connect to the MES's internal database, and we do not call its internal web services.

**Interface contract (capability level — the properties the design actually relies on)**:

- **A documented third-party integration path exists**: the MES exposes a **DMZ file drop**, provided expressly so that external third-party systems can exchange data with the MES. Access is controlled by **authenticated credentials plus IP allow-listing**, with the DMZ providing network isolation. Security comes from those layers, **not** from "the sender must be inside the plant network".
- **Payload = B2MML XML** (the public ISA-95 standard). The document types that matter here are `MaterialLot`, `ProductionResponse` and **`Equipment`** (the one this project uses). Filenames are structured (document type, identifier, timestamp, site) so that a drop is self-describing and reconciliable.
- **Batch cycle, not real time**: the MES collects the inbound drop, parses it, and publishes its outbound drop on a fixed hourly cycle. **Latency profile: ~1 hour, worst case approaching 2.** This is a property of the interface, not a tuning parameter.
- **Reconciliation**: the MES records the processing state of each document it ingests (accepted / rejected / processed), which the pipeline can read back to reconcile its own sends rather than assuming success.
- **★ Consequence — the sender does not need to be on the internal network.** A cloud (Fly.io) pipeline is a legitimate third-party sender under this path, so **no internal-network VM is required**. This removes the residual "the pipeline must run on the internal network" concern inherited from ADR-008.
- **Required administrative steps**: plant IT must (i) add the Fly.io dedicated egress IP to the DMZ allow-list, and (ii) issue the file-drop credentials. These correspond exactly to the two controls above.
- **⚠ Unconfirmed (do not guess)**: whether an external sender needs only IP allow-listing, or must additionally establish a VPN into the DMZ, was not settled from the documentation and is folded into the IT ticket for confirmation. **Guardrail #8 applies: we ask rather than assume.**

> **On what this section deliberately does not contain**: the MES is a third-party commercial product whose reference documentation is confidential. This ADR therefore records only the **capabilities the CMMS design depends on** — that a documented external integration path exists, what it costs in latency, and how it is authenticated — and never the vendor's internal schema, API names, or security model. The rule the code enforces (guardrail #7) is simply: *use the documented path, and nothing else.*

**The write direction (CMMS → MES)** is achievable:

- When a WorkOrder enters `IN_PROGRESS`, the pipeline drops a B2MML `Equipment` document setting the equipment status to `Out of Service`; the MES collects it on its next cycle.
- When a WorkOrder reaches `CLOSED`, the same mechanism flips it back to `In Service`.

**The read direction (MES → CMMS) — two unresolved questions**:

1. Is the MES configured to publish equipment status changes to its outbound drop, so the pipeline can pull them? **Pending confirmation of the MES-side configuration by plant IT.**
2. If it is not, automatic reactive ticketing (MES detects a fault → CMMS opens a work order) must take one of these routes:
   - (a) ask IT to enable publication of equipment events to the DMZ; or
   - (b) parse the notification email the MES already sends on a support request; or
   - (c) accept that the reactive use case **degrades to a manual trigger** (the operator sees the fault in the MES and raises the work order in the CMMS by hand), leaving the pipeline to handle only CMMS → MES planned-downtime synchronisation.

**Why not the internal real-time interface**: it can only be called from inside the plant network — and, per guardrail #7, it is not part of the documented third-party path in any case. Using it would force the pipeline onto an internal VM, pulling in plant IT and turning a one-person operation into a two-site deployment. This project accepts that **near-real-time batch coupling is the reality**, and declines to complicate the architecture in pursuit of immediacy. If an internal VM is eventually provided, an internal-pipeline upgrade path can be added as a revision to this ADR.

**Consequence for ADR-011**: a real-time bidirectional interlock is unreachable → on 2026-06-26 this converged to *"MES→CMMS read-only ingest; the CMMS does not control MES run-ability"* (see the ruling in ADR-011). No CMMS→MES production gating is built.

---

### ADR-015 — Assets as the canonical cross-system equipment identity service ‹Accepted (2026-06-20)›

**Origin**: a requirements request from a downstream analytics consumer, which needs to recognise "the same machine" across systems. Evaluated here and written up as our own ADR (counter-proposals allowed). This also closes out the structural open question "EID ↔ MES equipment id mapping" (P-1).

**Verdict: accept (modified)**. Downstream consumers all need a shared equipment identity; treating `assets` as the shared identity source is right. But the originally proposed *flat `sensor_id` column* has the wrong cardinality — we replace it with a normalised crosswalk.

**Decision**:
- `assets` is designed from day one as the **canonical cross-system equipment identity source**, not a cmms-private internal table. `asset_id` (= eMaint `compid`, shape `EID-\d{5}`) is the **standard anchor** shared by every consuming system.
- Provide **read-only** identity resolution: an API endpoint plus MCP read tools (`get_asset` / `resolve_asset_identity`), so other systems can look up the same machine by `asset_id` or by any external id. Reads are open (consistent with ADR-004 read/write separation).
- External system identifiers are **not** flattened into the `asset` master table; they live in a normalised **crosswalk table `asset_external_id(asset_id, namespace, external_id)`**. `namespace` is a controlled vocabulary — initially `mes_equipment` (MES equipment id) and `layer_b_sensor` (the analytics consumer's physical sensor layer), with room for more.

**Why a crosswalk instead of a flat `sensor_id` column**:
1. **Correct cardinality**: one machine can carry **many** sensors (current/vibration CT clamps, later PLC I/O taps). A flat `sensor_id` is 1:1 — wrong. A crosswalk is natively 1:N.
2. **One table, many systems**: the same table carries the MES equipment id (solving P-1), the sensor id, and any future system. Adding a new system never changes the `asset` schema.
3. **No guessing at MES naming (guardrail #8)**: at the time of writing it was unconfirmed whether the MES equipment id equals the EID. The crosswalk lets us record the mapping **explicitly** once confirmed, rather than assuming equality.

**Boundaries / responsibilities**:
- **Reads** (identity resolution) are open to consuming systems.
- **Writes** to the crosswalk (binding/unbinding sensors, registering MES ids) still go through the domain service — the single write path (ADR-001/005) — and are fully audited. A consumer registering its own sensor bindings is a **thin client** of cmms, going through a governed write; it never touches the table directly.
- cmms does **not** manage sensor hardware lifecycle (that belongs to the analytics consumer's sensing infrastructure); cmms only stores the identity mapping "which sensor id belongs to which asset".

**Impact on ADR-011**: the EID ↔ MES equipment id question is closed. **2026-06-20: P-1 confirmed — the MES equipment id and `asset_id` are the same thing under the same name (identity; both `EID-xxxxx`)** → the MES mapping is an identity function and needs **no** crosswalk rows; the pipeline joins directly on `asset_id`. The practical use of `asset_external_id` therefore narrows to **systems that do not share the name** (primarily `layer_b_sensor`, and any future system whose id ≠ EID).

---

### ADR-016 — Two-phase gated write, confirmable over an external channel ‹Accepted (2026-06-20); revises ADR-004 / ADR-005›

**Origin**: a downstream analytics consumer runs a clarification loop in which an **agent proposes** opening/closing a work order, but the **human confirmation happens in an external channel (the corporate chat platform)** — not in a cmms UI or session. The proposer (an agent) and the confirmer (a human) are therefore **different principals**.

**Verdict: accept**. This is structurally identical to the harness philosophy already in ADR-003/004: agents propose, deterministic scaffolding gates.

**Decision (extending the ADR-004 gated write)**: on top of the existing "dry-run + confirm within one session", add an **API-able asynchronous two-phase commit**:

1. **propose**: a caller (e.g. `agent:<name>`) submits a domain-operation intent + parameters + idempotency key → the domain service does **not** execute. It persists a **pending proposal** (intent, the dry-run diff of expected changes, proposer identity, idempotency key, **expiry**, single-use) and returns a **pending token**.
2. **confirm**: an external caller presents a **verified human identity** (`human:<id>`) + the pending token → the domain service validates (token valid, unexpired, unused, confirmer **authorised**) and then executes **through the single write path**.
3. **reject / expire**: the confirmer may reject; the proposal lapses on timeout. Neither changes data; both are audited.

**Key constraints (architectural correctness)**:
- **The single write path is unchanged** (ADR-001): confirm still executes via the domain service. A proposal is a *pending intent*, not a side-channel write.
- **Token ≠ authorisation**: holding a token does not entitle you to execute. **Authorisation comes from the verified confirmer identity.** Confirm must carry a concrete `human:<id>`; **anonymous confirmation is rejected**. The token only identifies *which proposal*.
- **Agent privileges are not widened**: only the low/medium-risk domain operations that ADR-004 already lets agents touch (e.g. `open_work_order` / `close_work_order`) may be proposed this way. High-risk operations (void, mass delete, key change) do **not** become agent-proposable just because a human confirms out-of-band. Moving the human confirmation to an external channel does **not** relax the agent's operation set.
- **Idempotency** (ADR-006): a proposal carries an idempotency key; re-proposing with the same key yields no second pending row, and re-confirming the same token does not re-execute.
- **Identity trust boundary**: cmms cannot itself verify an external chat platform's user identity. The confirm API demands an already-verified principal; the mapping and signature from external user → cmms principal must be supplied by the channel owner, and cmms must land on a **concrete human id and reject anonymity**.

**Audit extension (revises ADR-005)**: a two-phase mutation records `proposed_by` (`agent:<name>`) + `confirmed_by` (`human:<id>`) + both timestamps + the pending token. The final `source_actor` is the confirmer.

**Implementation (2026-06-22, verified against real PostgreSQL)**: `pending_proposal` table (migration 0008); `WorkOrderService.propose/confirm/reject` (confirm rejects anonymous and agent principals, demands `human:<id>`; idempotent propose; dry-run diff; executes through the single write path); MCP `propose_open/close_work_order` + `confirm_work_order_proposal`.

> **★ Revision (2026-07-04)**: the "identity trust boundary" clause above is **closed by adopting the cmms position: confirm happens at home.** Profile A confirmation is performed **exclusively with a cmms-native identity** (ADR-022 local account session / `mcp_scoped_token`) on a cmms surface. The external chat channel is **demoted to a pure notification channel** (the clarification message carries a deep link into cmms), and the "external user → cmms principal signature" contract is **withdrawn and shelved** (measured deep-link friction was acceptable; the external-assertion path was not worth its complexity). "Reject anonymous, require a verified `human:<id>`" is unchanged — what changed is only *where verification comes from*: cmms verifies it itself instead of trusting an external assertion.

---

### ADR-017 — Identity model for on-box reactive work orders, and the "single-step gated write" (Profile B) ‹Accepted (2026-06-21); supplements ADR-004/005/006/016›

**Origin**: a one-button "report a fault" tool at the machine itself. An operator standing in front of an equipment PC — a legacy, network-isolated machine controller with no route to the cloud — presses one button when they are certain of a fault → an on-box agent captures the HMI screen plus lightweight telemetry → the evidence is relayed out through an intermediary host on the plant side → a downstream service is then responsible for **opening a cmms reactive work order** and emitting a high-confidence status label. Opening a WO is a *write* to cmms, so it collides with our gated-write / audit / principal rules. The asset linkage is clean: the WO's asset is the machine-level EID = `asset_id` (P-1 identity confirmed).

**Verdict: accept**. This write is additive, low-risk and machine-attributed — the same class as the operations ADR-004 lets agents touch. But its identity model is **a different road** from ADR-016 (the external clarification loop) and must be kept explicitly separate; the two must not be conflated.

**Core: two gated-write identity profiles coexist, each in its own lane**

| | **Profile A (ADR-016, existing)** | **Profile B (this ADR, new)** |
|---|---|---|
| Scenario | Clarification loop: agent proposes, human confirms out-of-band | On-box one-button fault report |
| Confirmer / principal | An individual `human:<id>` (individually auditable) | Non-personal, channel-signed `agent:onbox` (machine/station attribution) |
| Operation set | open/close and similar low–medium-risk domain ops | **Only** `open_reactive_work_order` + `cancel_reactive_report` (soft-cancel) |
| Shape | Asynchronous two-phase: propose → token → confirm | **Single step**: edge-confirmed, signed, idempotent create |
| Individual human identity | Present (outside the red line: the engineer exercises judgement and must be accountable) | **Deliberately absent** (inside the red line: downtime/faults attach to the machine, and must not flow into personnel appraisal) |

**Q1 ruling — do not add a `device:` kind to `source_actor`**: `source_actor` (ADR-005) is a **governance actor taxonomy** (provenance: *what class of actor* caused this write). Its kinds {`human`, `agent:<name>`, `mes-pipeline`} stay stable. The proximate actor of an on-box write is the on-box automation channel → reuse the existing `agent:<name>` slot as **`agent:onbox`** (`Actor.agent("onbox")`, **zero schema change**). The "machine/station attribution" that consumers care about is **domain attribution**, not `source_actor`: it is carried by (a) `asset_id` (machine-level EID, FK, canonical) for the subject, and (b) an optional `origin_station` column on the WO for the station. We do **not** stuff the station into `source_actor` — that would pollute the governance taxonomy and inflate the enum with one value per station.

**Q2 ruling — a machine-attributed write with no individual human confirmer is a legitimate write in this architecture (restricted to the Profile B operation set)**: ADR-016's "reject anonymous, require a verified principal" **stands**. What is relaxed is **not** "reject anonymous" but "the principal must be a `human:<id>`". Profile B accepts a **channel-signed, non-personal principal** (`agent:onbox`) as the authorising principal, **provided**: ① the operation is additive and low-risk (open one reactive report — non-destructive, insert-only, same class as ADR-004's agent-permitted set); ② it is fully audited (channel principal + station + `evidence_ref` + on-box event id); ③ it is reversible (Q4 soft-cancel). **Hard boundary**: Profile B is **strictly limited** to `open_reactive_work_order` + `cancel_reactive_report`. Machine attribution does **not** open the door to close / general void / key change / mass delete — those remain Profile A / human, per ADR-016. "Verified" is achieved by the channel signing the request and cmms verifying it; **no signature = anonymous = rejected** (reject-anonymous is unchanged).

**Q3 ruling — single step; the button press *is* the confirmation gate, so no cloud propose→confirm round trip**: the physical on-box button press is a **human-in-the-loop confirmation** by an operator who is physically present and certain of the fault — already captured at the edge. We therefore allow a **single-step** "edge-confirmed, signed, idempotent create" and do **not** require a cloud-side dry-run + token + confirm round trip, because: ① the operation is additive and low-risk (ADR-004's strict two-step is reserved for high-risk/discretionary operations); ② the air-gapped topology (XP cannot reach the cloud; evidence relay is one-way) makes a synchronous round trip physically unsuitable; ③ the shape of a create is known in advance — a dry-run diff would tell nobody anything. The confirmation semantics travel to cmms carried by the **signature attestation + idempotency key**. ADR-016's two-phase round trip is **reserved for Profile A** (remote, discretionary confirmer).

**Q4 (cancel/void)**: supported — but **not** as the high-risk general `void_work_order` primitive of ADR-004. Instead a **narrowly scoped soft-cancel** `cancel_reactive_report`, which moves the WO to `CANCELLED` (audit trail preserved; not a physical delete), **and only if** the WO was (a) opened via the on-box channel, (b) still open, (c) not yet picked up or acted on by maintenance, and (d) within a short time window. General void remains Profile A and restricted.

**Q5 (idempotency, ADR-006)**: the idempotency key is the **on-box event id**, generated **at the edge** (at the moment of the button press) and carried unchanged along the whole evidence-relay path. cmms puts a unique constraint on it: a replay with the same key returns the existing WO and does not open a second one.

**Q6 (`asset_id` = MES EID)**: confirmed (P-1; identity; both `EID-xxxxx`; machine-level). **Integrity boundary**: the EID **must already exist in the cmms asset master** (FK). An on-box create carrying an unknown EID is **rejected** — cmms never silently mints a stub asset.

**Q7 (scope)**: new reports are opened in the **modernised cmms** (the new system of record), **not eMaint**. This replaces exactly one eMaint function — passive capture of downtime/fault cause (i.e. "operator sees a fault → manually opens an eMaint ticket" becomes one button) — and touches **nothing else** (not PM scheduling, not inventory, not asset registration). In essence it is a new trigger source for `open_work_order` with `work_type = REACTIVE`.

**Impact on Contacts**: this also settles that the "reporter / opened_by" of a reactive WO is **not** modelled as a Person FK, but as **station/channel attribution** (`agent:onbox` + `origin_station`). This matches the finding that many legacy "reporter" values are not people at all but **shared area/station labels**: this path originates from a station and does not identify an individual operator.

**Integration contract (2026-06-21)**:

1. **Channel principal signature (for gated *writes*)**: contract frozen as **`onbox_principal_sig.v1`**. The upstream channel service holds a private key (never leaves it) and signs every Profile B request as a **compact JWS** (EdDSA/Ed25519; ES256 as an alternative).
   **JWS claims**: `iss="onbox-channel"`, `sub="agent:onbox"`, `op ∈ {open_reactive_work_order, cancel_reactive_report}`, `asset_id=<EID>`, `idempotency_key`, `origin_station`, `evidence_ref`, `iat`, `exp (~5 min)`, `jti`.
   **cmms verification**: fetch the public key from the channel's JWKS endpoint (`/.well-known/onbox-jwks.json`; `kid` selects the key, rotation supported) → verify the signature + `sub == agent:onbox` + `op ∈ Profile B set` + not expired + known `kid` + **the JWS `asset_id` claim == the `asset_id` of the create**. Missing signature / verification failure / expiry / unknown `kid` = anonymous = rejected (ADR-016 unchanged).
   **Principal-type routing**: `agent:onbox` → this JWKS path; `human:<id>` → the Profile A human-verification path (individually auditable). **The two trust anchors are never mixed.**
   ★ Note this is distinct from *evidence resolution* signing: this contract answers "who may open a WO" (cmms governance); the presigned/signed access to an evidence blob answers "who may fetch the blob" (owned by the evidence store, and none of cmms's business).

2. **Station → EID resolution point**: vetted and accepted as **(a) static per-station configuration of the EID** (`profiles/<machine>.json` carries `eid` = `asset_id`, which travels out of the edge with the key and payload). The on-box agent does **not** call the cloud to resolve it at runtime. cmms retains the Q6 safety net: the EID must already exist in the asset master or the write is rejected.

3. **`idempotency_key` / `evidence_ref` string shapes** (canonical form owned by the edge tooling):
   - **`idempotency_key` = `onbox:<station>:<EID>:<edge_ts>:<nonce>`** (generated at the edge; the JWS claim carries this **verbatim**). `station` = hostname (alphanumeric); `EID` = `EID-…`; `edge_ts` = compact `YYYYMMDDTHHMMSS`; `nonce` = GUID.
   - **`evidence_ref` = `onbox-evidence:v1:onbox:<station>:<EID>:<edge_ts>:<nonce>`** (i.e. a fixed prefix + the full idempotency key; the evidence id *is* the whole key — self-correlating, 1:1 with the WO). The **stored literal has no `:artifact` suffix → it denotes the bundle**; fetching a single artifact (e.g. the screenshot) is done via an `?artifact=` query at the resolver and **never enters the string cmms stores**.
   - **cmms's stance (unchanged)**: both are **opaque; stored verbatim; never parsed**. EID integrity is enforced by #1's **explicit JWS `asset_id` claim == the create's `asset_id`**, **not** by parsing the key — even though the format would technically permit it (`split(':')[2]` = EID), cmms deliberately stays uncoupled from the key's internal structure (future-proofing).
   - **Column widths**: pinned at **`idempotency_key VARCHAR(128)`, `evidence_ref VARCHAR(160)`**. The component maxima (a short station label, a 9-char EID, a timestamp, a nonce, the fixed prefix and the colons) add up to roughly 100 characters, so 128/160 leaves headroom. **Err loose, not tight**: this is a byte-exact dedup key — truncating the column means bytes don't match, means dedup silently fails (a correctness break). `VARCHAR` is variable-length, so a few spare bytes cost essentially nothing.

**Implementation (2026-06-22, verified against real PostgreSQL)**: migration 0008 adds `work_order.idempotency_key VARCHAR(128)` (unique) + `evidence_ref VARCHAR(160)` + `origin_station`, all stored verbatim and never parsed. `src/cmms/domain/work_order/onbox.py` performs JWS verification (EdDSA via JWKS, injectable key resolver, `sub`/`op`/`exp`/`kid` checks, returning the claims so `asset_id` can be read). `WorkOrderService.open_reactive_work_order_onbox` (verify → create REACTIVE/OPEN, `source_actor=agent:onbox`, idempotent, unknown EID rejected) and `cancel_reactive_report_onbox` (soft-cancel, with the state machine guaranteeing the WO is still OPEN). API endpoints `/work-orders/on-box/{reactive,cancel}` (503 when `CMMS_ONBOX_JWKS_URL` is not configured).

---

### ADR-018 — Asset composition graph (machine ↔ module) and the identity of non-EID modules ‹Accepted (2026-06-25); extends ADR-015, refines P-1›

**Origin**: once a downstream analytics consumer settled that utilisation/yield/output are reported **against the physical machine (the MES parent EID)** with sub-modules as a drill-down, it raised an asset-hierarchy alignment request. cmms is the provider; we evaluated it and wrote our own ADR.

**Background facts (checked against the asset and work-order exports, not assumed)**:
- The asset export has **no parent/child or hierarchy column**; the cmms asset table is currently **flat** (the self-referencing `parent_asset_id` column exists but is **empty**).
- **Module-level EIDs already exist as independent asset rows**: several machine families are registered both as a parent machine and as its constituent modules, each with its own EID. The parent/child relation is **hidden only in the free-text `comp_desc`** (e.g. "…- Module 1") — an untrustworthy source that guardrail #8 forbids us to parse.
- **Historical WOs are of mixed granularity**: for those families, both the parent machine and each of its modules carry hundreds of work orders. WOs have always been filed against "whichever EID was relevant at the time" — both machine-level and module-level. **Granularity cannot be assumed to be uniform.**
- **Some machine families (e.g. the FlexBonders) carry exactly one EID each**; the modules they nominally contain exist in **neither MES nor cmms** (no system identifier whatsoever).
- **Some assets are shared control resources serving several machines at once** (e.g. an industrial PC driving multiple production machines).

**Verdict: accept (direction) + counter-proposal (cardinality / identity)**. cmms being the system of record for the machine→module asset tree/graph is the **right direction** (cmms owns the asset master; the tree is maintenance-relevant, since PM/WO are performed on modules; the MES tree is itself incomplete). But two counter-proposals are needed on the data structure and on identity.

**Decision**:

1. **Two asset↔asset relationships, stored in a single typed authoritative junction `asset_relationship`**:
   - **`contains_module`**: machine (`from`) ⊃ module (`to`). **Strictly acyclic tree**: a module has ≤1 active container at a time.
   - **`shared_dependency`**: shared resource (`from`, e.g. a shared industrial PC) → served machine (`to`). **N:M** (one resource serves many machines; one machine may depend on many resources).
   - Columns: `from_asset_id`, `to_asset_id`, `relationship_type` (FK → `asset_relationship_type` lookup), `source` (provenance), `valid_from`, `valid_to` (temporal; nullable = open/permanent) + AuditMixin. Surrogate `id` PK plus a **partial unique index on `(from_asset_id, to_asset_id, relationship_type) WHERE valid_to IS NULL`** (only one active edge per type per pair; history retained).

2. **Rationale for the counter-proposal** (structurally identical to ADR-015 rejecting a flat `sensor_id` in favour of a normalised crosswalk): ① **correct cardinality** — a shared module is N:M, which the existing 1:N `parent_asset_id` cannot express; ② **one table, many relationship types** — a containment tree and a dependency graph share one table, and future types (calibration reference, redundancy pair, …) need no schema change; ③ **provenance + temporality** are built in; ④ **no guessing** — relationships are registered explicitly, never inferred from `comp_desc`.

3. **`parent_asset_id` (the existing 1:N self-reference) is retained** as a **denormalised cache** of the single-parent `contains_module` edge (the junction is authoritative; the asset service maintains the cache on link/unlink, giving tree queries a fast path). `shared_dependency` does **not** write `parent_asset_id` — it is not a containment relation.

4. **Modules invisible to MES get no minted id and do not enter the cmms master** (this supersedes an earlier draft that would have minted synthetic ids). Reasons: ① it preserves **`asset_id` = EID = MES-EID + P-1 with zero exceptions** — an invariant load-bearing across every consumer and across the gated-write Q6 safety net, not worth punching a hole in for an edge case; ② the phantom modules have **no data source at all** (MES cannot see them, the legacy CMMS never had them), so minting them yields hand-maintained empty shells; ③ their only module-level signal (electrical draw picked up by add-on sensing) belongs to the analytics consumer's sensing-and-fusion layer, and ADR-015 already established that cmms does **not** manage sensor hardware lifecycle — pushing phantom modules into cmms would cross that boundary; ④ for those machines the WO history is empirically **entirely machine-level**. → Module granularity for them lives only in the analytics consumer's fusion layer; in cmms, **module-level maintenance = a WO on the machine EID plus a note**.

5. **The reversible correct path**: if module granularity ever genuinely becomes necessary, the answer is to **instrument the module in MES and obtain a real EID** (preserving the invariant), **not** to mint a synthetic id. → This slice therefore builds **no `identity_source` / `MOD-` mechanism** (YAGNI; the asset master stays pure-EID). The earlier draft (`identity_source ∈ {emaint_eid, cmms_minted}` + `MOD-\d{5}`) is rejected, and kept only as a documented fallback should cmms ever truly need to represent an asset with no external id.

6. **P-1 refined (not broken)**: identity (`asset_id` = MES EID) is **per-EID and tier-agnostic**. cmms `asset_id` **includes module-level EIDs** (they *are* the MES module EIDs) — it is not machine-level only. **"Machine vs module" is expressed by the composition graph, not by P-1.** This corrects the framing that "P-1 means machine-level". Consumers must therefore keep two rollups distinct: (a) runtime utilisation/output attribution goes by **per-segment EID co-tagging** (not via the static tree, so shared modules are never double-counted); (b) maintenance/structural rollup (machine WOs = self + descendants) goes via this tree, consuming the cmms rollup read.

7. **WO/PM attribution unchanged**: a WO stays on **the EID it was filed against** (machine and module levels coexist — that is the empirical reality, and force-normalising granularity would distort it). **Rollup to the machine follows `contains_module` descendants only**; **`shared_dependency` edges roll into no parent machine** (a shared resource is maintained on its own account). cmms **provides the rollup read** (machine WOs = self + `contains_module` descendants); consumers use it rather than reassembling it. This is a read-side capability (read API / MCP), implemented in this slice (migration 0009).

8. **Single write path (guardrail #1)**: linking/unlinking always goes through governed asset-domain operations (`link_containment` / `link_shared_dependency` / `unlink_relationship` (soft, via `valid_to`) / `upsert_relationship_type`), fully audited (`source_actor`). Because we do not mint ids (decision 4), there is **no `mint_module_asset`**.

9. **Link provenance + classification rules**: `mes_dependent_equipment` (the MES dependent-equipment export — a self-referencing parent/child list) plus `cmms_curated` (manual curation). **Never parse `comp_desc`** (guardrail #8). **Classify each edge from that export**: a child with exactly one parent → `contains_module`; **the same child with multiple parents → `shared_dependency`** (e.g. one shared industrial PC serving three parent machines). **Direction mapping in the loader**: `contains_module` → `from = parent (machine)`, `to = child (module)`; `shared_dependency` → `from = child (shared resource)`, `to = parent (machine)` (direction flips by type; the loader handles it, the source table is taken as-is). **EID is the only reliable join key** — empirically, the human-readable machine label in the CMMS master disagrees with the MES label for the same EID, and the same label is sometimes reused across two different EIDs. Everything therefore binds on EID; the human labels are a separate reconciliation problem and do not affect edge definition.

**Impact on ADR-015**: adds a **composition** dimension to the canonical identity service.

**Impact on ADR-011 / P-1**: refines P-1 (per-EID, tier-agnostic) without changing its identity conclusion.

**Implementation (2026-06-25)**: migration 0009 + `asset_relationship` + the `asset_relationship_type` lookup + governed ops + descendant-tree and WO-rollup reads + API + MCP + a dependent-equipment classification loader (`classify_dependent_equipment` removes self-loops and duplicates; `read_dependent_equipment_rows`) + tests. Verified against real PostgreSQL on 2026-06-26 (Docker testcontainers: full pytest suite green, `alembic upgrade 0001→0009`, `alembic check` clean). **The asset master is untouched.** The edge load is deliberately **lossy and counted**: the raw export contains whole-row duplicates, null children and self-references, so the loader collapses to distinct edges, drops the unbindable ones, and reports **raw / distinct / bound / dropped** rather than silently binding what it can. Roughly half of the raw rows survive as real edges, split between `contains_module` and `shared_dependency`. The actual DB bind is done atomically at deploy time with end-to-end reconciliation. **An honest count that does not match the raw row count is the point** — a loader that reported "success" here would be lying.

---

### ADR-019 — cmms user surfaces and front-end strategy (engineer console + natural language + deployment topology) ‹Accepted (2026-06-28)›

**Origin**: whether cmms should have a UI at all; where spare-part and work-order photos live; forwarding work orders into Jira MRQ tickets; a natural-language front end (Hermes); and "how many machines does cmms actually need". This ADR converges all of it into one surface strategy.

**Background facts (not assumptions)**:
- cmms has been **headless** to date: domain service + MCP + CLI, **no graphical UI** (a future web UI was always foreseen as a thin client; REQUIREMENTS #3 demands "agent-accessible").
- REQUIREMENTS explicitly lists as **non-goals**: native mobile apps, multi-language UI, multi-plant.
- The primary users are the **on-site maintenance engineers of the contractor that runs the assembly floor (high frequency)**. Management dashboards/rollups are provided by a downstream analytics consumer → **cmms is not a management BI tool**.
- Photos: `inventory_item.photo_ref` only stores a legacy-CMMS back-end pointer; the actual images were never extracted. Every part image has since been downloaded locally (filename = `item_code`), so the legacy index can be retired.
- The Jira instance the plant uses is **reachable from where the CMMS runs**, and each engineer authenticates to it with their own account and personal access token (2026-06-28 correction: an earlier assumption that it was unreachable from the CMMS turned out to be wrong, which is what unblocked ADR-020).
- Legacy work orders already carry `MRQ-xxxx` in `external_ref` (~5%) → **WO ↔ MRQ referencing is existing practice**, not an invention of ours.

**Verdict: accept (direction)**. It is time to plan a UI, but the cmms UI is strictly scoped to an **engineer console**: management belongs to the analytics consumer, natural language belongs to an agent front end, and all three surfaces share one domain service (the single write path, guardrail #1).

**Decision**:

1. **Three user surfaces, separated by responsibility** (all thin clients, no business logic, all calling the domain service):
   - **Engineer console (the subject of this ADR) = a responsive web UI**: fault reporting, work-order queue, spare-part lookup, part issue, PM.
   - **Natural-language surface = an MCP client agent** (Hermes is the leading candidate): ad-hoc queries, cross-system work (WO → MRQ), speed/accessibility.
   - **Management/rollup surface = the downstream analytics consumer** (cross-system fusion; **not rebuilt inside cmms**).

2. **Web UI technology = HTMX + FastAPI server-side rendering (Jinja2) + responsive CSS, served in-process by the `cmms` app.** Rationale: Python end to end (maintainable by one person), no second JS build/deploy pipeline, **IaC stays in git with zero clickops** (aligns with ADR-009), console-grade interactivity is well within HTMX's range, it is a thin client by construction, and writes only ever go through the domain service (guardrail #1). **Rejected**: an SPA (React/Vue — a JS toolchain is overkill for a single maintainer) and low-code platforms such as Retool/Appsmith (cloud clickops / one more service, in tension with our governance discipline).

3. **Responsive web, not a native app** (respecting the REQUIREMENTS non-goal): usable straight from a phone browser, **zero install**. **Assets carry a barcode → scanning opens the fault-report form for that EID directly** — the single biggest friction reducer in a modern CMMS, and a natural fit for an EID-keyed model (the phone camera suffices; no native app needed). **Multi-language (★ corrected 2026-07-01, see ADR-023)**: default English, switchable per user to zh-TW / vi and persisted — replacing the original "single-language zh-TW", because **the shop floor is multilingual** and no single language covers everyone.

4. **Photos/media = a standalone `attachment` slice**: binaries live in **Cloudflare R2** (reusing the existing R2 account with an added `cmms-media` bucket; R2 has no egress fees); PostgreSQL stores only the pointer `attachment(owner_type, owner_id, r2_key, content_type, caption, + audit)`. Uploads go through a governed write; retrieval uses presigned URLs. **The eMaint `photo_ref` is retired** in favour of our own "filename = item_code" index. One table serves part images, before/after work-order photos and asset photos (DRY).

5. **NL surface: direction accepted, specifics to be verified.** **Hermes** (MIT-licensed, MCP client, desktop + gateway modes, **pluggable model**) is the leading candidate. **Model pluggability is a governance dial**: want reliability → point it at a frontier model; want data to stay in-plant → point it at a local model. **Verify before building**: whether the gateway centrally hosts MCP connections on behalf of connected clients, and tool-calling reliability. **Pin the version** (v0.x iterates fast). "Answer only from cmms content" is enforced at two layers: the **action layer** is locked by the MCP tool boundary itself (guardrail #2); the **answer layer** is enforced by a system prompt that mandates tool grounding (every factual claim must come from a cmms tool). **The cmms MCP endpoint needs authN/authZ** — the NL front end is just another principal; its writes still go through gated write (ADR-016) with `source_actor=agent:<name>` in the audit trail.

6. **WO → MRQ forwarding = agent-layer orchestration across two MCP servers; the cmms core does not talk to Jira directly (a governance choice, not a reachability constraint)**.
   - **2026-06-28 premise correction**: the original reason ("the company Jira is intranet-hosted and unreachable from Fly") **no longer holds** — Jira is reachable over the public internet via SSO (per-engineer accounts/PATs) plus the corporate 2FA step. A cloud cmms **can** physically reach Jira; "no direct connection" is therefore demoted from *forced by reachability* to a **deliberate governance choice**, supported by these weaker-but-still-valid reasons:
     - ① **PATs stay out of the cloud secret store**: Jira writes use each engineer's **own PAT**; the PAT stays on whichever side runs the agent, and is **not** pooled into the Fly secret store of cmms — shrinking the credential blast radius and avoiding "one cloud service account represents the whole plant".
     - ② **No outbound sprawl from cmms**: cmms remains a headless system of record and **does not initiate connections to arbitrary third-party systems**; every additional "cloud cmms → external" link adds coupling and governance surface, against the spirit of ADR-001/009.
     - ③ **Per-engineer PATs give correct Jira attribution**: writing with an individual's PAT means the MRQ change is **natively attributed to the person who actually did it** in Jira, rather than to an anonymous service account — the same spirit as ADR-005 ("writes are attributable"). The corporate 2FA step protects interactive login; programmatic writes go through that engineer's PAT.
   - **Forwarding path**: the agent (cloud or client-side, see decision 7) reads the work order from the cmms MCP → writes Jira via an Atlassian MCP server holding that engineer's PAT. cmms only needs to: ① serve the work-order read (already built); ② record the back-reference `work_order_external_link(work_order_no, system, external_key)` (N:1 — one MRQ covering many WOs; also backfilling the legacy `MRQ-xxxx` values in `external_ref`).
   - Detailed design deferred to **ADR-020**.

7. **Deployment topology (answering "how many machines")**:
   - **cmms proper = 2 machines on Fly** (`shopfloor-cmms-db` + `shopfloor-cmms`; the shorter names `cmms-db`/`cmms` were globally taken, hence the prefix), **0 machines on the corporate intranet**.
   - **The web UI is served in-process by the `cmms` app → no extra machine.** Users open a web page; zero install.
   - **★ 2026-06-28 correction: no intranet machine is needed at all.** The original claim that "one intranet machine is needed once Hermes + Jira forwarding is enabled" rested **solely** on "only an intranet host can reach Jira", and that premise is false. Therefore:
     - **The NL/Jira-forwarding agent can run in the cloud or client-side**; both placements reach cmms (HTTPS) and Jira (public SSO):
       - **Client-side (originally the suggested default)**: the engineer runs Hermes on their own machine with their **own PAT** → satisfying decision 6's "PAT out of the cloud + per-engineer attribution", with no server per person and no intranet requirement.
       - **Cloud**: technically fine, but requires putting some Jira PAT into the cloud secret store (triggering decision 6 ①) → not recommended unless a central service account is a deliberate choice. If central hosting is genuinely wanted, use **gateway mode** on **one** host (cloud or intranet) — still **not** an expansion of cmms proper.
     - **Intranet stays at 0 machines**: there is no longer any hard requirement for an intranet host merely to reach Jira.
   - **Data flow when forwarding (corrected)**: cloud cmms → (HTTPS) agent (client-side or cloud gateway) → Atlassian MCP (holding the engineer's PAT) → Jira (public SSO endpoint).

**MVP (starting scope)**: three screens — **fault reporting + my work-order queue + spare-part lookup (with photos)** — covering roughly 80 % of an engineer's day and pulling the `attachment` slice along with it. Layered on afterwards: part issue, PM due list, barcode reporting, the NL surface, MRQ forwarding.

**Impact on existing ADRs / guardrails**: implements ADR-001 (headless; UI = thin client), ADR-003/004/016 (writes through the domain service / gated write), ADR-009 (IaC, zero clickops), ADR-013 (2-machine Fly topology). Adds the `attachment` (media) slice and foreshadows **ADR-020** (WO ↔ external knowledge base links) and **ADR-021** (PM generation strategy).

**Status**: **★ delivered 2026-07-01 (engineer console)**: web UI scaffold (FastAPI + Jinja2 + HTMX, `src/cmms/web/`, mounted at `/app`) + five screens (report / queue / detail timeline with writes and state transitions / spare parts / PM) + responsive three-column desktop layout + the agent dock shell. The `attachment` slice landed on 2026-06-28.

> **★ 2026-06-29 follow-up corrections (ADR-020 / ADR-022)**: ① decisions 6/7's default of "PAT stays client-side / every engineer runs Hermes on their desktop" is revised by **ADR-020** to "**a central Hermes gateway + per-user encrypted PATs stored in cmms**", because the contractor's engineers are not in the corporate identity provider and because of the zero-install principle; ② the login mechanism left open here is settled by **ADR-022** as "**cmms-native local accounts (not SSO) + admin/engineer RBAC**".

> **★ 2026-07-01 addendum (panel information architecture + Hermes sandbox)**:
> - **① Panel IA = two surfaces (execution vs governance), never mixed in one navigation**: (a) the **engineer console (execution)** — mobile-first, four bottom tabs; future *execution* panels (asset detail, meter reading, barcode) drill down under existing tabs and **do not add tabs**, keeping the shop-floor view minimal. (b) The **admin console (governance)** — a new `/admin`, desktop-first, left navigation, high density (accounts/roles [ADR-022], PAT vault status, controlled-vocabulary/lookup maintenance, audit-trail inspection, PM schedule configuration, asset-relationship maintenance, attachment governance), RBAC-gated (engineers never see it). (c) Both surfaces share **one design-token set, two postures** (mobile / at-a-glance / fast vs desktop / dense / deliberate). **Routing rule for any new panel**: on-site execution → engineer console; system governance → admin console; **cross-plant rollup/BI → don't build it — it belongs to the analytics consumer** (the cmms admin console is for *operating this system of record*, not for being a plant dashboard).
> - **② Hermes sandbox = process isolation + MCP-only + scoped token (strengthening decision 5)**: Hermes runs as a **separate process** (not inside the cmms app), and its **only channel into cmms is the MCP endpoint (HTTPS) with a scoped token**. Its toolset consists solely of domain operations (guardrail #2), with **no `run_sql`, no file reads, no path to the machine** — so even a hallucinating or compromised Hermes can only make a bounded set of domain calls (each audited; discretionary writes still gated). The token carries the current human identity + RBAC, so Hermes **acts as that engineer and is bound by their role** — never god-mode. **The MCP tool boundary is the sandbox wall; a separate process plus a limited token turns that wall from logical into physical.**
> - **③ The NL agent is pluggable; no hard dependency on Hermes**: cmms depends on the **MCP interface**, not on any particular agent — Hermes, Claude Code, Codex or a hand-rolled agent can all be the NL surface. Claims of "self-evolving, gets better as it runs" are treated with reserve: real gains come from **accumulated grounding data + curated tools/prompts** (which any agent can consume), not from a vendor's secret sauce. The investment therefore stays in the **interface + clean data**, and the agent stays swappable with zero cmms changes.

---

### ADR-020 — Work order ↔ external knowledge base (Jira MRQ) linking and natural-language forwarding ‹Accepted (2026-06-29); follows ADR-019 decisions 6/7; depends on ADR-022›

**Origin**: foreshadowed by ADR-019 decision 6. The confirmed use case: an engineer selects one or more work orders and asks Hermes, **in natural language**, to consolidate them into a Jira MRQ ticket (**open a new MRQ** or **append to an existing one**), with each user bringing **their own** Jira PAT.

**Background facts (not assumptions)**:
- ~5 % of legacy work orders already carry an `MRQ-xxxx` in `external_ref` → WO ↔ MRQ referencing is **existing practice**.
- MRQ = the material/purchase request ticket type in the plant's Jira; an Atlassian MCP server has been verified able to read and write it.
- Users span plant staff and the maintenance contractor's engineers. **The contractor's staff are not in the corporate identity provider**, but they **can each obtain a Jira PAT** (see ADR-022 §1).
- cmms is still headless with no live surface; Hermes is the NL candidate selected in ADR-019 decision 5.

**Verdict: accept**. This realises the WO → MRQ use case. However, ADR-019 decision 7's default ("every engineer runs Hermes on their desktop") collides both with the zero-install principle and with the reality of the contractor's users, so **this ADR relocates the agent to a central Hermes gateway**.

**Decision**:

1. **The cmms core does not talk to Jira directly (a governance choice; carried over from ADR-019 decision 6)**: the three reasons stand — ① per-user PATs are never merged into one plant-wide service account; ② cmms stays a headless SoR with no outbound sprawl; ③ writing with an individual PAT attributes the change natively to that person in Jira. **cmms does only two things**: serve work-order reads (already built) and record the back-reference link.

   > **★ 2026-07-06 revision (decision 1 reversed: cmms now calls the Jira REST API directly)**. The original route ("all forwarding happens gateway-side through an Atlassian MCP server") is **abandoned**, for two reasons: (a) **the Atlassian MCP write schema never materialised** — without the MRQ write schema, the gateway had nothing to call; and (b) **event-driven auto-sync is impossible from outside** — the requirement "once a WO is linked, every new note on it syncs to the MRQ automatically" is triggered by a cmms-**internal domain event** (`add_note`), and the gateway/agent is simply not in that event loop; only cmms itself can act at the moment `add_note` happens.
   > → Forwarding is therefore done by **cmms calling Jira REST v2 directly** (`HttpJiraForwarder` in `src/cmms/jira_forwarder.py`), using the **per-user Jira PAT of the person who created the link** (ADR-022 vault, `system='jira'`).
   > **All three governance reasons of decision 1 are preserved**: the PAT is still per-user (native Jira attribution, revocable, blast radius limited to one person), and the *capability* is **hard-narrowed per decision 8** — the forwarder can do exactly two things (`create MRQ issue`, `append comment`), with project and issue-type hard-wired in config, so it cannot reach any other part of Jira.
   > Implementation: a `jira_outbox` table (migration 0025). The initial `forward_work_orders_to_mrq` (an MCP tool: dry-run preview → human-in-the-loop approval → execute) opens one MRQ and posts one comment per note, in global chronological order across the selected work orders. Thereafter every `add_note` on a linked work order **enqueues into the outbox in the same transaction** and the web layer **flushes it immediately in the background** (requirement ②), with the CLI `jira-flush-outbox` as a backstop. A comment is the note's **original text, faithfully, untranslated** (auto-sync involves no LLM); the summary/description are generated by Hermes in the user's `jira_output_locale` (requirement ③). Jira Data Center is assumed (Bearer PAT); moving to Jira Cloud changes one auth header in the forwarder. **If unconfigured (`CMMS_JIRA_*` / master key / PAT missing), the outbox honestly marks the row `config-missing` / `pat-missing` — it never fakes success.**

2. **Forwarding = a central Hermes gateway orchestrating across MCP servers (★ this revises ADR-019 decision 7's per-desktop default)**:
   - **Why revise**: ADR-019 decision 7 suggested each engineer run Hermes on their own desktop with their PAT staying local. But (a) Hermes is a program that must be installed and run, which contradicts ADR-019 decision 3's zero-install promise for the engineer console; and (b) **contractor engineers**, who are not on plant-managed machines, are even less likely to install it. → Instead, host **one central Hermes gateway** (one cloud process/machine) that every user reaches through the web UI, giving zero-install coverage for both user populations.
   - **The accepted cost**: with a central cloud process writing on the user's behalf, the user's Jira PAT must go to the cloud and be stored on the cmms side → carried by the **per-user encrypted credential vault of ADR-022 §5** (one key per person, revocable, fully audited, positioned as a user-managed credential with a blast radius of exactly one user). This is the **only** path that satisfies "usable by everyone + zero install"; ADR-019 decision 6 ①'s "no PAT in the cloud" principle therefore **yields**, compensated by encryption at rest + revocability + individual attribution.
   - **Data flow**: the user, logged into the web UI (`human:<id>`), selects work orders and issues an NL instruction → the central Hermes **reads the work orders through the cmms MCP** (authN per decision 5) → drafts the MRQ → **the user previews and confirms in the web UI** (decision 4) → the MRQ is written to Jira **using that user's PAT** (decrypted on demand from the ADR-022 vault) → the link is **written back into `work_order_external_link`**.

3. **cmms-side data model = `work_order_external_link` (new)**:
   - Columns: `id` PK, `work_order_no` (FK → work_order), `system` (controlled; initially `jira`), `external_key` (e.g. `MRQ-1234`), `link_type` (controlled: `created` / `appended` / `referenced`), `title` (optional; cached MRQ title), `created_by` (`human:<id>`), `source_actor`, `created_at` + AuditMixin.
   - **Cardinality = N:M** (one MRQ covering many WOs — e.g. a batched purchase; one WO may reference several MRQs) → hence a junction table.
   - **Idempotency** (ADR-006): the same `(work_order_no, system, external_key, link_type)` is never recorded twice.
   - **Legacy backfill**: the `MRQ-xxxx` values already sitting in `work_order.external_ref` are migrated once (`link_type=referenced`, provenance tagged `legacy`) so existing references become queryable.

4. **New MRQ vs append to existing, with a human in the loop**:
   - NL intent selects the action: new → `created`; append to a user-specified MRQ key → `appended`; pure reference without touching Jira → `referenced`.
   - **Preview + user confirmation is mandatory before any Jira write**: Jira is an external side effect and not easily retracted, so Hermes's draft (which work orders it summarises, what fields it will write) is rendered in the web UI first, and only sent when the user confirms. **The confirmation happens inside the cmms web UI** (not in an external chat channel — a different road from ADR-016 Profile A); its semantics are "the human who initiated this external write confirms it, here and now".

5. **Governance layering**:
   - **The cmms-side write is only the link record** (additive, low-risk) → it goes through the work-order domain service's single write path with full audit (`source_actor=agent:<name>`, `created_by=human:<id>`). **No propose/confirm needed** — recording an external reference is low-risk and additive, the same class as an ADR-017 on-box open.
   - **The Jira-side write is an external system**, outside the cmms governance surface; attribution rests on the user's own PAT (Jira's native audit records the person). cmms does not vouch for Jira content; it only records "we initiated this, and it links to that MRQ".
   - **MCP endpoint authN/authZ (★ named in ADR-019 decision 5; landed here)**: the central Hermes must pass principal verification to reach the cmms MCP. The connection identity is `agent:<name>`, but **the human behind it must ride along with every request** (the user is already logged into the web UI). Mechanism: the web login session mints a **short-lived scoped token** (carrying `human:<id>` + a scope) → Hermes presents that token to the cmms MCP → cmms validates the token (valid / unexpired / in scope) and recognises the agent connection, recording `source_actor=agent:<name>` with attribution to `human:<id>`. **This is the last governance brick on this line** — structurally identical to the ADR-016 trust boundary, except the human is **cmms-native (self-verified)**, which is far simpler than an external assertion.
   - **Agent privileges are not widened**: the operations Hermes can invoke remain the low/medium-risk set ADR-004 permits agents; NL does **not** bypass gated write (discretionary writes still require propose/confirm or an in-UI confirmation).

6. **The "only cmms content" boundary for NL** (from ADR-019 decision 5): the **action layer** is locked by the MCP tool boundary itself (guardrail #2); the **drafting layer** is bound by a system prompt enforcing tool grounding (every fact written into Jira must originate from a cmms work order; the user's instruction decides only how it is consolidated, never invents part numbers or quantities).

7. **MRQ presentation mapping = synthesised header + one comment per work-order note (★ 2026-07-01)**:
   - **Background**: a long-downtime work order is not a single `brief_description`; it is a log of `work_order_note` entries appended over time (manual and NL input both go through this append-only table). The MRQ presentation must mirror that faithfully rather than squashing many updates into one paragraph.
   - **Title + description (the MRQ header)**: **synthesised by Hermes** from the work order's **current full picture** (problem + notes + parts + status), editable and re-generatable as the WO evolves. This is the *interpretation layer*, where Hermes is allowed to summarise. **Generated in the user's `jira_output_locale`** (ADR-023; default English).
   - **Comments (the MRQ body stream)**: every `work_order_note` maps **1:1 and faithfully** to one Jira comment, **preserving the original timestamp and author, without rewriting or merging** (light formatting only). This is the *fact layer* and stays faithful to the original. **Language follows the user's `jira_output_locale`**: the original text is stored in cmms `work_order_note` (the SoR), and the Jira comment is rendered in the chosen language — **faithfully translated where necessary** (still 1:1: no aggregation, no omission, language only; the original may be appended for traceability). **Translation never merges notes.**
   - **Append symmetry**: a new note on the work order → Hermes **appends one comment to the existing MRQ** (`link_type=appended`; decisions 3/4) rather than opening a new one. → **The MRQ presentation equals the cmms work order**: both are append-only streams, which natively supports incremental, multi-day update styles.
   - **Idempotency** (ADR-006): each note→comment carries an idempotency key (`note_id ↔ jira_comment`), so re-runs never double-post.
   - **Not done**: Hermes never fabricates note content; a comment's facts may only come from an existing note (grounding, per decision 6).

8. **The Jira write scope is hard-narrowed to MRQ only (★ 2026-07-01)**:
   - **Problem**: the company Jira has many projects, issue types and sections. A user may pick the wrong one; an agent may hallucinate one. The agent must not be able to write anywhere but MRQ.
   - **Hard narrowing (enforced by an allowlist at the gateway/forwarder layer, not merely by the preview)**: the agent's Jira writes are **restricted to two operations, and only against MRQ** — ① create a new issue **in the MRQ project with the MRQ issue type**; ② **add a comment to an existing MRQ** (key matching `MRQ-\d+`, with its issue type verified as MRQ). **Always refused**: other projects, other issue types, editing non-comment fields, status transitions, deletions, or anything outside MRQ.
   - **Enforcement point**: before any Jira call, the target is **validated** (project + issue type + operation ∈ allowlist); non-conforming calls are blocked and never sent — even if the NL instruction or the preview asked for them (whether from user mis-selection or agent hallucination). In the same spirit as guardrail #2 ("tools are domain operations"), the Jira write surface collapses into two **narrow operations** — *create an MRQ* and *append a comment to an MRQ* — rather than generic Jira CRUD.
   - **Preview + confirmation (decision 4) is the second line, not the only line**: the allowlist is a hard wall (bounding **scope**); the preview lets a human check **content**. They stack.

**Impact on existing ADRs**: implements ADR-019 decision 6; **revises ADR-019 decision 7** (Hermes placement: per-desktop → central gateway); depends on **ADR-022** (accounts + PAT vault + MCP token); adds the `work_order_external_link` domain model; **adds a dependency on `work_order_note`** as the factual source for the comment mapping; Jira output language follows ADR-023's `jira_output_locale`, and the Jira write scope is hard-narrowed to MRQ by decision 8.

**Status**: **★ 2026-07-02 — governance core landed (migration 0017)**: `work_order_external_link` (WO ↔ MRQ; idempotent + MRQ-shape gatekeeping + dual attribution) + `user_external_credential` (the ADR-022 §5 PAT vault: Fernet envelope encryption, plaintext never persisted, owner-only access, revocable, fail-closed with no master key) + `mcp_scoped_token` (derived from a web session, carrying the delegated `human:<id>`, instantly revocable — the MCP authN/authZ brick) + a `JiraForwarder` port (InMemory/Null fakes; the note↔comment 1:1 idempotency contract) + `WorkOrderService.record_external_link` / `backfill_legacy_mrq_links` + MCP `record_work_order_external_link` + CLI `cred-set` / `cred-revoke` / `backfill-external-links`; verified against real PostgreSQL (alembic 0001→0017, `alembic check` clean, 16 DB/unit tests). **★ Follow-up (2026-07-02)**: a per-user PAT web form (`/app/settings`, store/revoke; if the master key is unset it says so and fails closed rather than buffering the secret) and MCP `record_work_order_external_link` gained **verified delegation via `scoped_token`** (the identity inside the token beats any `on_behalf_of` assertion; if both are absent the call is refused).

---

### ADR-021 — PM generation strategy (time / usage / forecast triggers; scheduler and on-demand coexist) ‹Accepted (2026-06-28)›

**Origin**: eMaint cannot obtain MES production counts, so many "every N production cycles" usage-based PMs could only ever be judged **manually, on demand** in the legacy system. cmms's whole edge is "automatically ingesting MES production data" (REQUIREMENTS #2): pull per-EID production counts from MES → automatically determine whether a usage-based PM is due, and **forecast** the due date from the production rate.

**Background facts (not assumptions)**:
- cmms already has **time-based scheduling data** (`pm_schedule`: `frequency_interval` / `frequency_unit`, `next_due_date`, `last_pm_date`, `is_suppressed`; `frequency_interval = 0` means non-recurring). **But no "due → auto-generate a PM work order" write operation exists yet.**
- **The usage-based data source is currently empty inside cmms**: the legacy CMMS could not obtain MES production counts. The corresponding `asset_type = Meter` rows and their readings remain unextracted.
- **The only legitimate path to MES production data is the DMZ FTP B2MML interface** (`ProductionResponse`, hourly batch; guardrail #7 / ADR-014). ADR-011 settled that MES→CMMS is a **read-only ingest** → using production counts to drive PM is exactly a read-only-ingest use case; there is zero directional conflict (we never write back to MES).
- PM generation is **determined by schedule/data, not by discretion** → it needs **no** propose/confirm (gated write is reserved for reactive/discretionary writes). A scheduler plus idempotency plus audit is sufficient.

**Verdict: accept**. The three trigger types correspond to three levels of data maturity and are **delivered in phases**; the automatic scheduler and the "review + generate on demand" mode **coexist, and both matter**.

**Decision**:

1. **Three due-determination triggers, delivered in phases**:

| Type | Trigger condition | Data source | Depends on | Status |
|---|---|---|---|---|
| **time-based** | `next_due_date` reached | existing `pm_schedule` | none | **can be built now** |
| **usage-based** | cumulative production since last PM ≥ threshold | MES `ProductionResponse` (per-EID production count) | MES ingestion | pending pipeline |
| **forecast** | extrapolate from recent throughput to predict the **date** the usage threshold is hit | MES production time series | same as usage, layered on top | pending pipeline |

   - **Forecast is a predictive layer over usage, not an independent trigger**: a usage-based PM has no calendar date, so engineers cannot pre-stage parts. Forecast converts "how many units to go" into an **estimated due date** using recent throughput, letting it appear in the due list and the lead window. **Actual generation still requires the real cumulative count to reach the threshold** (forecast decides *when to start showing/preparing*, not *whether it is really due*), so forecast error never opens a work order by itself.
   - **Dual trigger**: one PM may carry both a time and a usage threshold; whichever fires first triggers.

2. **Two execution modes coexist, both through the single write path (guardrail #1)**:
   - **Automatic scheduler (unattended) — ✅ landed 2026-06-28**: `generate_due_pm_work_orders` (WorkOrderService), called daily by a scheduler (CLI `cmms pm-generate-due`, triggered by a Fly scheduled machine / cron). It generates `WorkOrder(type=PM, status=OPEN)` for every PM that is due (`next_due_date <= as_of`), not suppressed, and **recurring** (`frequency_interval > 0` with a non-empty `frequency_unit`). **Idempotent** (ADR-006): if this cycle already has an unclosed PM work order (the `last_work_order_no` soft link points at a non-terminal WO), it returns that WO instead of generating a second (the equivalent key is `pm_id + due_cycle`, where the cycle is implied by close advancing `next_due_date`). **One transaction per PM** (a single failure is isolated); fully audited (`source_actor=scheduler`); not gated. **Recurring only**: non-recurring/one-off PMs stay on-demand (otherwise closing them would not advance `next_due_date` and every run would regenerate them).
   - **Review + on-demand execution (built first, for the MVP)**: on the PM screen, an engineer presses "generate" for a given PM → `generate_pm_work_order`. This covers `frequency_interval = 0` (as-needed) and discretionary cases.
   - **Why both**: the scheduler is the safety net that never misses a due date; on-demand gives the engineer control and handles non-recurring/discretionary work. Ship on-demand first, fast-follow with the scheduler.

3. **Generation behaviour (common to all three)**: a **lead window** (generate N days before due — default 7 — into a `PLANNED` state, so parts can be staged and work scheduled); **Fixed vs Floating** anti-pile-up (the next due date counts from the scheduled date or from the actual completion date; `calendar_freq_type` still to be extracted); **write-back on completion** (time: advance `next_due_date`; usage: reset the cumulative baseline).
   - **★ Implementation note (2026-06-28)**: the time-based scheduler **ships without the lead window for now**. Opening a WO N days early requires a non-downtime `PLANNED` preparatory state, but the current canonical 7-state machine has no `PLANNED`, and `OPEN.is_downtime = true` — so opening early as OPEN would count the parts-staging wait as machine downtime and poison the downtime calculation. The lead window, the `PLANNED` state and Fixed/Floating are therefore bundled together and deferred until `calendar_freq_type` is extracted and the state machine is extended (respecting guardrail #8: do not guess at semantics). The current scheduler generates OPEN **on or after the due date** (equivalent to a 0-day lead window), which is semantically safe.

4. **Governance and audit (`source_actor`)**:
   - **usage-based / forecast** are driven by ingested MES production data → `source_actor = mes-pipeline` (accurate by name).
   - **time-based** is driven by an internal clock/scheduler and has nothing to do with MES → **add `source_actor = scheduler`** (honestly labelling clock-driven writes and keeping them distinct from `mes-pipeline`, so the audit trail is not misread; `scheduler` is reusable for any future internal scheduled write). This is a **legitimate evolution** of the actor taxonomy (a genuinely new class of automated actor) — categorically different from the "inflate the enum with attribute sub-types" that ADR-017 Q1 rejected.
   - None of the three go through gated write; the subsequent start/hold/complete/close of a PM work order still runs on the existing state machine with full audit.

5. **Not doing / boundaries**: **never write back to MES** (usage is read-only production data; a due PM never sets an MES `Equipment Status` — ADR-011); **never guess at meter semantics** (the exact fields of meter readings and the MES `ProductionResponse` production count await MES confirmation — guardrail #8).

**Impact on existing ADRs**: it is the first *write* use case realising REQUIREMENTS #2; it is a consumer of ADR-011's read-only ingest and depends on the ADR-014 interface; it is orthogonal to ADR-016/017 gated write; **it extends the `source_actor` taxonomy with the `scheduler` kind** (ADR-005/017); it will require `pm_schedule` to gain usage-trigger columns (threshold, cumulative baseline, dual-trigger flag) plus a production-count landing table (to be finalised with the MES ingestion slice).

**Status**: Accepted. (a) **time-based ✅ complete**: on-demand `generate_pm_work_order` + the automatic scheduler `generate_due_pm_work_orders` + CLI `pm-generate-due` + `Actor.scheduler()` (2026-06-28; full suite green, lint clean); the lead window / `PLANNED` state are deferred per the note in §3. (b) usage + forecast layer on after MES ingestion.

---

### ADR-022 — cmms human accounts, authentication, authorisation, and per-user credential custody ‹Accepted (2026-06-29)›

**Origin**: the web UI (ADR-019) needs a login. Users span plant employees and the maintenance contractor's engineers, and **the contractor's staff are not in the corporate identity provider** (SSO cannot cover them) — but they **can each obtain a Jira PAT**. We need a cmms-native account layer producing a **trustworthy** `human:<id>` to feed the audit trail (ADR-005) and gated-write confirmation (ADR-016), and to hold each user's Jira PAT for ADR-020 forwarding.

**Background facts (not assumptions)**:
- cmms has had **no human authentication at all**: `human:<id>` was merely an audit string with **nothing verifying it** (a grep of `src/` confirms no `User` model, no login, no role authorisation; the existing "session"/"role" strings refer to DB sessions and contact roles).
- Gated write (ADR-016) demands a verified principal and rejects anonymity; until now the human principal could only be asserted externally.
- User population: plant employees + contractor engineers (the latter outside the corporate SSO, but each able to obtain a Jira PAT).

**Verdict: accept**. This fills the login gap ADR-019 left open. **Because a large share of the users are not in the corporate identity provider, no single SSO can cover everyone → cmms builds local accounts** rather than integrating the corporate SSO.

**Decision**:

1. **cmms-native local accounts; no SSO**: the user base spans two organisations, one of which is outside the corporate identity provider, so one SSO cannot cover them. Issuing our own accounts gives both populations one login.
   - `user_account`: `user_id` PK (the `<id>` in `human:<id>`, e.g. `jlee`), `username` (unique), `display_name`, `password_hash` (argon2id), `org` (controlled: `plant` / `contractor` / …), `role`, `is_active`, **`ui_locale`** (default `en`, ∈ `en`/`zh-TW`/`vi`), **`jira_output_locale`** (default `en`, same domain; both per ADR-023), **`emaint_assignee`** (nullable; the exact string used in work-order/PM `assigned_person` — e.g. "Alice Fang" — used to filter "my work orders / my PMs". **Deliberately not a `person_id` FK**: empirically only a minority of the distinct `assigned_person` values match any `contacts.fullname`, and several of the busiest technicians are absent from contacts entirely — so we store the exact string and compare by equality rather than manufacturing a foreign key that would not hold. Migration 0014), `created_by` + AuditMixin.
   - **A path to layering SSO later is reserved**: should we ever want to *optionally* add corporate SSO as an alternative login for plant employees, adding `external_idp` / `external_subject` columns suffices, keeping the local-account path for everyone else. The MVP is **purely local**.

2. **Authentication**: `username` + password (argon2id hashed; plaintext and reversible forms are never stored). A successful login issues a **server-side session** (cookie), which fits the same-process HTMX web app naturally and can be revoked instantly. The MVP chooses sessions over JWTs (simpler, instantly revocable, no long tail of leaked tokens). The session secret lives in a Fly secret (IaC, not clickops).

3. **The `human:<id>` namespace, coexisting with ADR-016's external assertions (the two trust anchors never mix — carrying forward ADR-017's principle)**:
   - A cmms-native account → `human:<user_id>` (cmms verifies it itself; login *is* the assertion).
   - An externally asserted principal (ADR-016) → mapped through an external IdP and a cross-system trust boundary, with a **separate naming lane** (e.g. `human:ext:<…>`), so it **cannot collide** with cmms-native ids.
   - Both ultimately land as `human:<id>` for audit and authorisation, but their **trust origins differ and remain distinguishable in the audit trail** (the auth source is recorded). **The two anchors are never mixed**: cmms-native goes through session self-verification; external goes through the ADR-016 signature/mapping path.

4. **Authorisation: coarse-grained RBAC (admin / engineer / operator), enforced in the domain service layer**:
   - `role ∈ {admin, engineer, operator}` (**★ `operator` added 2026-07-12**; the MVP had two levels).
   - **engineer (the default)**: report faults, progress their own/assigned work orders, issue parts, look up spares, view/execute PM, set **their own** PAT, initiate **their own** WO→MRQ forwarding.
   - **admin**: everything an engineer can do, plus account management (create/deactivate accounts, change roles, reset passwords on behalf of users) and the future high-risk operation surface (void/bulk — still bounded by the existing ADR-004/016 gates).
   - **operator (★ 2026-07-12; shared shop-floor tablet accounts)**: **allowlisted; only** ① open a REACTIVE fault report (including the photo and first report note taken at report time); ② cancel a **false report opened by their own account that is still OPEN**; ③ read, plus change their own password/language in `/app/settings`. Every other governed write (state machine / close / assignment / part issue / note correction / PM generation / proposals / MRQ) raises `AuthorizationError` in the domain layer. The principle: **an operator does not do non-production work.** `is_operator()` applies only to human actors, so the agent / scheduler / on-box paths are entirely unaffected.
   - **The enforcement point is the domain service** (not merely hiding buttons in the UI): reads stay permissive (ADR-004 read/write separation); writes are gated by `role` + ownership (an engineer may only touch their own/assigned work orders and their own credentials).
   - **Agent privileges are not widened**: RBAC governs the human surface. The agent (Hermes / on-box) operation set remains bounded by ADR-004/016/017; RBAC never relaxes anything for an agent.

5. **Per-user external credential vault (Jira PAT; encrypted at rest in cmms)**:
   - `user_external_credential`: `id` PK, `user_id` (FK), `system` (controlled; initially `jira`), `secret_ciphertext`, `key_version`, `label`, `created_at`, `last_used_at`, `revoked_at` (nullable) + AuditMixin.
   - **Envelope encryption**: the application encrypts the PAT with a master key held as a Fly secret (**never in the database**); **the DB stores ciphertext only**. The plaintext PAT **never** reaches a log, an audit record or an API response — the audit trail records "user X's PAT was used to write Jira", **never the value**.
   - **One key per person; revocable at any moment** (set `revoked_at`, effective immediately); optional expiry.
   - **Access boundary**: only an **ADR-020 forwarding action initiated by that user themselves** may decrypt and use their PAT. It **cannot be borrowed across users** and cannot be bulk-exported in plaintext.
   - **Positioning: a user-managed credential, not a plant-wide service account.** This directly answers ADR-019 decision 6 ①'s concern — this is not one master key for the whole plant, it is N individually-attributed, individually-revocable personal keys. The blast radius of a cmms compromise is contained by *encryption + master key outside the DB + revocability*, and a leak can be rotated person by person instead of shutting the plant down.

6. **Deployment (no extra machines; consistent with the ADR-019 topology)**: auth, account management and the PAT settings page are all served in-process by the `cmms` app (the login page is part of the web UI scaffold). The master key and session secret are Fly secrets (IaC).

**Guardrail compliance**: single write path (account/role/PAT changes all go through the domain service); full audit (`source_actor` — excluding, of course, the PAT value); read/write separation.

**Impact on existing ADRs**: settles ADR-019 §6's open login question; gives ADR-016 a **local human principal source** (cmms-native, where previously only an external assertion was possible); extends ADR-017's "two trust anchors never mix" to the human identity plane; provides ADR-020 with the PAT vault and the identity basis for MCP scoped tokens.

**Status**: **★ core landed 2026-07-01**: migration **0013** (`user_account` + `user_session`) + `IdentityService` (create / authenticate [argon2id + timing equalisation] / resolve / logout [instant revocation] / set_locale) + web login/logout + RBAC guard + the `cmms user-create` CLI; models in `src/cmms/domain/identity/`; verified against real PostgreSQL (alembic 0001→0013, `alembic check` clean, identity DB tests). **★ §5 PAT vault landed 2026-07-02** (migration 0017: `CredentialVault` envelope encryption + CLI `cred-set` / `cred-revoke`; see ADR-020). **★ Admin account UI + personal settings page landed 2026-07-02** (`/admin` account CRUD + `/app/settings` for password/locale/assignee; RBAC enforced at both layers).

---

### ADR-023 — Internationalisation (UI locale + Jira output locale, persisted per user) ‹Accepted (2026-07-01)›

**Origin**: (a) the cmms web UI defaults to English, but a user may switch to **Traditional Chinese (zh-TW)** or **Vietnamese (vi)**, and cmms **remembers** the choice across logins until changed; (b) the agent writing Jira MRQ tickets likewise **defaults to English** (regardless of the language of the work-order content), switchable per user and persisted the same way.

**Background facts (not assumptions)**:
- **The shop floor is multilingual**: the user base spans a Traditional-Chinese-speaking site team and a contractor workforce that does not share that language → no single language covers everyone; English is the lowest common denominator and therefore the default.
- REQUIREMENTS §7 originally listed "multi-language UI" as **out of scope** → this ADR **overturns that non-goal** and brings i18n into scope.
- The UI display language and the Jira output language are **two independent requirements**: a user may want a Traditional Chinese UI but English Jira output (external / cross-team convention).

**Verdict: accept**. Multi-language moves into scope; **UI locale and Jira output locale are two independent, per-user, persisted preferences**, both defaulting to `en`.

**Decision**:

1. **Two independent per-user preferences, stored on `user_account`** (ADR-022):
   - `ui_locale` (default `en`, ∈ `{en, zh-TW, vi}`) — the web UI display language.
   - `jira_output_locale` (default `en`, same domain) — the language the agent writes Jira MRQ content in.
   - **Persistence semantics**: the user switches in settings → the value is written back to `user_account` → every subsequent login inherits it until changed again (**not session-scoped, not browser-scoped**). Pre-login pages (e.g. login) do a best-effort read of the browser `Accept-Language`, defaulting to `en`.

2. **UI localisation mechanism (built into the scaffold)**: every user-facing string goes through an i18n catalogue (one per locale); templates **never hard-code** any language; `en` is the fallback. **Data values are never translated** (EIDs, part numbers, status codes, and user-entered work-order content are shown verbatim) — only the **interface chrome** (labels, buttons, hints) is translated. Three MVP locales: `en` / `zh-TW` / `vi`.

3. **Jira MRQ output language (following ADR-020 decision 7)**:
   - **Header (title/description — the interpretation layer)**: Hermes generates it directly in `jira_output_locale`.
   - **Comments (the fact layer — the 1:1 mapping of `work_order_note`)**: cmms `work_order_note` is the **original-text SoR** (whatever language the engineer wrote in). When written to Jira, the comment is rendered in `jira_output_locale` — if the original is in another language it is **faithfully translated** (still 1:1: **no aggregation, no omission, no change of fact** — only the language, and the original may be attached as "Original: …" for traceability). **The anchor of the original is the cmms SoR**; Jira is a downstream language rendering. Translation **never merges** multiple notes (still one note, one comment).

4. **Not doing / boundaries**: three MVP locales (en/zh-TW/vi), no others for now (the schema is not specialised — adding a locale value suffices); no machine translation of UI **content** (only static chrome is translated); **no guessing at sensitive terms** (part numbers, machine names and status codes are never translated).

**Impact on existing ADRs / REQUIREMENTS**: **overturns REQUIREMENTS §7's "multi-language UI is a non-goal"** (now in scope); **corrects ADR-019 decision 3**'s "single-language zh-TW" to "multi-language, default en"; adds the `ui_locale` / `jira_output_locale` columns to **ADR-022**'s `user_account`; refines **ADR-020 decision 7**'s comment language handling.

**Status**: **★ UI landed 2026-07-01**: an i18n dict catalogue (`src/cmms/web/i18n.py`; en/zh-TW/vi, with Accept-Language / cookie / `user_account` negotiation and an `en` fallback) + `user_account.ui_locale` / `jira_output_locale` (migration 0013) + language switching written back to the account. **Pending**: applying the Jira output locale in the ADR-020 forwarding slice; completing the `vi` catalogue.

---

### ADR-024 — Part-issue charge model (charge target: WorkOrder | Asset; non-WO direct issue + rescue of historical missing-WO rows) ‹Accepted (2026-07-01)›

**Origin**: eMaint hard-binds a **part issue** to a work order — you must have a work order before you can issue parts against it. Is that binding necessary, how do we relax it, and how do the past (all bound) and the future (partly unbound) coexist?

**Background facts (not assumptions)**:
- Current model: `stock_transaction`'s `work_order_no` is **already nullable** (`RECEIVE` / `ADJUST` never had a work order); `work_order_part`'s `work_order_no` is NOT NULL (it is the work-order-side projection of an issue). Both the governed `issue_part_to_work_order` and the historical `backfill_part_issue` always populate `work_order_no`.
- The hard binding has **already broken against real data**: out of a work-order corpus in the tens of thousands, `load-work-orders` **discards a few hundred mis-created ghost rows** — which is precisely why the hard binding could not be assumed to hold. `load-part-issues` then skips two further classes: **issues pointing at work orders that no longer exist** (missing-WO) and **issues naming part numbers absent from the inventory master** (missing-item, by far the larger group).
- **Every row in the part-issue export already carries a `compid` (EID)** (the loader already extracts it as `PartIssueImport.expected_asset_id`, originally only for cross-checking) — so the charge target (the asset) is **natively present in the data and does not depend on the work order**.
- Domain judgement: ① in this plant, the legacy CMMS is used **only** to manage equipment maintenance and equipment spares, so **every part in cmms is equipment-related**; ② the two part-code prefixes in use have, over many years of practice, **no physical distinction** (confirming the earlier "suspected classification, unconfirmed" note as unreliable); ③ pure consumables (general shop-floor supplies) are **out of cmms's scope**.

**Verdict: accept**. The invariant a CMMS actually needs is **not** "every issue has a work order"; it is "**every issue resolves, auditably, to one asset**". A work order is the richest charge target, but not the only legitimate one. Hard-binding to a work order is simultaneously **over-constraining** (it forces ghost work orders into existence) and **under-constraining** (when a WO has no asset, the cost still cannot be rolled up to a machine). Since parts in cmms are always equipment-related, the charge target is **always an asset** → the model collapses to a binary **WorkOrder | Asset**, and a `cost_center` node is **unnecessary** (YAGNI; pure consumables never enter cmms).

**Decision**:

1. **The charge target is binary — WorkOrder | Asset — and an ISSUE carries exactly one of them (an exactly-one invariant).**
   - `stock_transaction` gains `charge_target_asset_id` (nullable, FK → `asset.asset_id`, indexed). Work-order issue: `work_order_no` set, `charge_target_asset_id` null (the asset is resolved through the work order). Direct issue: `charge_target_asset_id` set, `work_order_no` null.
   - **A DB CHECK is the gatekeeper (a real guardrail, not just application logic)**: `kind <> 'ISSUE' OR num_nonnulls(work_order_no, charge_target_asset_id) = 1`. Non-ISSUE kinds (`RECEIVE` / `ADJUST` / `RETURN`) are unconstrained (both may be null). This **forbids** an orphan issue that resolves to no asset, and equally forbids binding both (which would make the charge ambiguous).
   - **`work_order_part` is untouched**: a direct issue with no work order writes only a `stock_transaction` row and **no** `work_order_part` row. Work-order issues behave exactly as before. → **Not one historical row changes, and existing work-order issue logic changes not at all.**

2. **Corrective and PM work goes through a work order (the default); a non-WO direct issue must name an asset.**
   - Reactive and PM work stay on the work-order path — per-machine cost and fault history are richest there.
   - A new governed domain operation `InventoryService.issue_to_asset(asset_id, item, qty, actor, ...)` handles direct issues (stock consumption charged to a machine — an inventory responsibility; it decrements `on_hand` exactly as a work-order issue does). It goes through the single write path, is fully audited, and is idempotent. It is an **ordinary governed write, not gated per transaction** (gated write remains reserved for high-value/anomalous cases, ADR-016).
   - **No ES/EC split**: the policy treats all items alike (overturning an earlier draft's "ES via work order / EC via cost centre" — a default founded on a *presumed* physical distinction between the prefixes, which domain review disproved).

3. **Rescue historical missing-WO rows by charging them to the asset (additive; history is not rewritten).**
   - `backfill_part_issue` gains an `asset_id` fallback: WO exists → charge the work order (old behaviour); **WO missing but the `compid` is a valid asset → charge the asset** (`INSERTED_ASSET`, still with `adjust_on_hand=False`, matching backfill semantics); WO missing and `compid` invalid/absent → `MISSING_WORK_ORDER` (genuinely unrescuable).
   - **An item absent from the inventory master is always judged `MISSING_ITEM` first** (the item is the more fundamental blocker — neither a WO nor an asset can rescue a part number that does not exist).
   - **Two axes, kept distinct**: missing-**WO** (the work order is gone) is rescuable when a `compid` is present; missing-**item** (the part number is gone) is not (`stock_transaction.item_code` has no FK target) — and per ADR-018's "never mint phantom ids", those rows continue to be skipped.
   - Re-runs are idempotent: rows already loaded hit their idempotency key → DUPLICATE; previously-skipped missing-WO rows that carry a valid `compid` are newly rescued as `INSERTED_ASSET`. The actual rescued/remaining counts are whatever the loader reports (honest counting, not a pre-declared guess).

**Guardrail compliance**: single write path (direct issue, backfill and work-order issue all go through the domain service); MCP tools are domain operations (`issue_to_asset`, not raw SQL); read/write separation (a direct issue is a write and is idempotent); full audit (`source_actor` + `reason`); idempotency (`idempotency_key`); **no guessing at data semantics** (ES/EC not split; charge to an asset only when the `compid` is valid; never mint a missing item). The DB CHECK kills "an orphan issue that resolves to no asset" at the persistence layer — direct issue is exactly the high-risk mouth for stock shrinkage, so the charge target is mandatory and non-null.

**Impact on existing ADRs / domain model**: extends ADR-005 (`stock_transaction` gains a charge dimension); **supersedes** an earlier draft's `cost_center` charge target and ES/EC split (neither was ever implemented; both were converged to asset-only).

**Status**: **★ landed 2026-07-01**: migration **0015** (`stock_transaction.charge_target_asset_id` + index + CHECK `ck_stock_transaction_issue_charge`) with the `StockTransaction` model's `__table_args__` aligned; `InventoryService.issue_to_asset` + `charge_target_asset_id` threaded through `post_stock_transaction`; `WorkOrderService.backfill_part_issue` asset rescue + `PartIssueOutcome.INSERTED_ASSET`; a `rescued` counter on the loader's `LoadResult` echoed by `load-part-issues`; the `cmms issue-to-asset` CLI; verified against real PostgreSQL (Docker testcontainers; alembic 0001→0015, `alembic check` clean, DB tests).

**Open**:
- A front-end entry point for direct issue (scan EID → pick item → issue), plus a "per-machine parts consumption" read (work-order issues ∪ direct issues).
- Whether direct issue should be gated (propose/confirm) for high-value items — the MVP keeps it an ordinary governed write and will tighten based on practice.
- RETURN (parts returned to stock) charge semantics are untouched (this slice governs ISSUE only).

---

### ADR-025 — Change-request workflow (two lanes: data changes via gated write / new features via the dev loop) ‹Accepted (2026-07-02)›

**Origin**: a "GitHub-PR-like" change-request mechanism for engineer feedback. The requester's own two-way split was correct; the crux is the **runtime/dev boundary**.

**Decision (two lanes, never mixed)**:

1. **Lane 1 = data/configuration changes** (edit equipment information, PM cycles, PM owner, …) → **reuse the ADR-016 gated write**: an engineer (or an agent drafting on their behalf) **proposes** → a `pending_proposal` (with a dry-run diff) → **an admin reviews and confirms in the admin console** → execution through the single write path, fully audited (`proposed_by` = the proposer, `confirmed_by` = the admin). The agent only **drafts and proposes**; execution is the admin's governed confirm (never god-mode). Each admin-scope change is a **concrete governed operation** (e.g. `update_pm_cycle`) — never a generic edit-any-field (guardrail #2).
2. **Lane 2 = new features/code** → the **dev loop** (Claude Code + a real git PR + migration/tests/ADR), which **never enters the runtime** (guardrail #6: the AI *writes* code, it does not *act as* the production operator). The app only needs a lightweight "feature request" collector to feed the backlog (not yet built; a later small slice, or carry it on a work-order `note` in the meantime).

**Guardrail compliance**: Lane 1 runs end to end through the single write path + the two-phase commit (ADR-016) + refuses anonymous confirmation; Lane 2 gives the runtime no path whatsoever to change code.

**Open / not done**: Lane 1's proposable operation set is currently ADR-016 Profile A (`open_work_order` / `close_work_order`); admin-scope governed ops such as PM cycle/owner are opened one small slice at a time. The Lane 2 feature-request collector is unbuilt.

**Status**: **★ Lane 1 review UI landed 2026-07-02**: `/admin/proposals` (admin-gated; lists PENDING proposals with their params and dry-run diff, plus confirm/reject; the confirmer is the logged-in admin's `human:<id>`, anonymous refused) + `WorkOrderService.list_proposals`; web smoke and DB tests. The propose/confirm/reject core landed earlier (migration 0008).

---

### ADR-026 — RFQ / procurement (`reorder_quantity` + supplier↔org + email adapter) ‹Accepted (2026-07-02)›

**Origin**: post-launch feedback — the supplier table was not visible; one supplier has many contacts with one primary; a one-click/batch RFQ email is wanted. Supplier lookup landed earlier; this ADR closes the RFQ/procurement line.

**Background facts (not assumptions)**: `inventory_item.reorder_point` already exists; `reorder_quantity` **does not** (an `orderqty` export must be supplied). `inventory_item.supplier` is **free text** (the FK to org was deferred). `person` has no `is_main`. The shared workspace mail account used for outbound RFQs has no app password yet.

**Verdict: accept**. Split into a **buildable core** (schema + domain operations + email adapter, all testable offline) and the parts **blocked on live credentials** (actually sending mail requires the `orderqty` data + an app password for the mail account + a Fly secret). The adapter boundary makes the live part a drop-in.

**Decision**:
1. **Quantity**: `reorder_quantity` (from the `orderqty` import); if absent → the RFQ falls back to `max(0, reorder_point − on_hand)` (still usable).
2. **supplier ↔ org**: `inventory_item.supplier_org_id` FK → organization; `link-suppliers` matches on `organization.name` exactly (case-insensitively), hitting ~95 %. Unmatched rows stay NULL and are **RFQ-ineligible** (awaiting manual linkage).
3. **Recipient**: the `is_main` person's email at the organization; falling back to the lowest `person_id` that has an email. `person.is_main` (one per organization; `set_main_contact` clears then sets).
4. **The agent surface is draft-only**: MCP `draft_below_safety_stock_rfqs` is a **read-only dry run** (it never sends mail). Actual sending is human-initiated (a web button / admin), consistent with ADR-016/020.
5. **Email adapter**: an `EmailSender` port (`src/cmms/email.py`; a real SMTP implementation plus InMemory/Null fakes, mirroring `storage.py`). Real SMTP is used only when all three keys (host + user + password) are present; otherwise it falls back to InMemory (dev/CI never send).
6. **RFQs are persisted under governance**: `rfq_request` + `rfq_request_line` (status `drafted` / `sent` / `failed`; idempotency key; audited); `ProcurementService.create_rfq` (single write path + idempotency).

**Guardrail compliance**: single write path; MCP tools are domain operations (draft/preview, not a raw mailer); read/write separation (the agent may dry-run; only a human may go live); full audit + idempotency; **no guessing at data semantics** (a missing `orderqty` falls back rather than being invented; an unmatched supplier stays NULL rather than being force-linked).

**Status**: **★ core landed 2026-07-02 (migration 0018)**: `inventory_item.reorder_quantity` / `supplier_org_id` + `person.is_main` + `rfq_request` / `rfq_request_line` + `ProcurementService.create_rfq` / `draft_below_safety_stock` / `resolve_supplier_email` + `InventoryService.link_supplier_org` / `autolink_suppliers` / `set_reorder_quantity` + `ContactsService.set_main_contact` + the `EmailSender` adapter + MCP `draft_below_safety_stock_rfqs` (dry run) + CLI `load-orderqty` / `link-suppliers`; verified against real PostgreSQL (alembic 0001→0018, `alembic check` clean, procurement DB tests). **★ Follow-up (2026-07-02): the one-click web button landed** — an RFQ button on the spare-part detail page (visible only once a `supplier_org` is linked) + `POST /app/inventory/{code}/rfq` (nonce-idempotent; **if SMTP is unconfigured it runs `dry_run=True`, records the RFQ as `drafted` and says so** — an honest degradation, rather than letting the InMemory sender pretend it was `sent`).

---

### ADR-027 — Agent landscape and identity constitution ‹Accepted (2026-07-04)›

This ADR anchors, as authoritative text on the cmms side, the parts of the cross-system agent settlement that **constrain cmms**.

**Decisions**:

1. **Agent landscape (boundaries)**: the **user-facing agent lives in exactly one place** (the downstream analytics consumer: the NL entry point + the clarification loop; its principals are registered). **cmms is a passive tool surface** (MCP tools + the Hermes gateway) — it waits to be called and grows no user-facing agent of its own. The edge/on-box tooling never gets an agent and never stands up an MCP server (its CLI is frozen). Cross-system interaction is **permitted only through frozen contracts** (CLI / HTTP / MCP): importing another system's code and connecting directly to another system's database are both forbidden.
2. **Identity**: agent principals are uniformly named `agent:<system>-<surface>` (`agent:onbox` being the archetype). Agent writes are **always governed** (propose/confirm, or JWS) and **never anonymous**. The cmms `source_actor` taxonomy — {`human`, `agent:<name>`, `mes-pipeline`, `scheduler`} — is unchanged; the Hermes gateway takes its principal name from this convention when it lands (replacing the provisional `agent:hermes` in ADR-020 §5).
3. **Service reads vs agent reads**: a sibling **service** reading cmms uses **HTTP JSON + a static bearer token** (a Fly secret; the on-box POST endpoints, which carry their own JWS, are exempt). **MCP is reserved for the agent surface, in Phase 2.** Adding authN to the cmms read API is a ratified breaking change, shipped with the next deploy.
4. **EID grounding (cmms is the authority)**: resolving any agent's colloquial machine name → EID **never passes through an LLM**. **The alias authority is the cmms asset master** (P-1: `asset_id` ≡ EID); an alias seed table maintained elsewhere is a **synonym layer only, not authoritative**. If a name cannot be resolved, ask the user — **never guess**. The cmms read API *is* the authoritative lookup surface; **no second copy of EID data is created anywhere.**
5. **AI output entering a work order (cmms is the SoR implementer)**: AI output lands in `work_order_note` with `entry_type = ai_candidate` (a governed lookup value) and an "AI candidate (unconfirmed)" badge in the UI. The **`evidence_ref` is a standard prefix line inside the note body — `evidence: <ref>` (v1, verbatim, opaque)**. This is naturally separated from the human conclusion layer (`action_taken`, and in future `confirmed_reason_code`); **a candidate is never promoted into a confirmed value** (the confirmed-reason feedback loop reads only human-confirmed columns).
6. **Work order → confirmed reasons feedback loop (Phase 2)**: cmms emits, the analytics consumer stores, downstream systems consume. The **prerequisite** is that the shared failure vocabulary be promoted to a committed contract (cmms holds it as a governed lookup). The cmms-side shape is an optional `close_work_order(confirmed_reason_code=)` parameter plus exposing the column on the read surface (consumers pull; cmms does not call out — per ADR-020 decision 1).
7. **The whole agent line is Phase 2 and not part of the v1 gate**: publicly deploying the MCP server, the Hermes gateway, the decision-8 allowlist and live Jira are all Phase 2. Their preconditions are the master-key secret, the Jira write schema, and this ADR being signed off. Until then, "**the MCP server is not deployed, therefore the agent attack surface is zero**" is the honest posture.
8. **The honest-restatement clause is implemented by the analytics consumer** (coverage must be visible to a human; figures are quoted, never recomputed; the evidence chain survives; a provenance badge is shown). cmms is aware of it and has no implementation obligation — but if the cmms read surface is ever used for agent restatement, the same iron rules apply.

**Impact on existing ADRs**: supersedes the agent-naming and deployment-sequencing details of ADR-019 decision 5 and ADR-020 decisions 2/5; read alongside the ADR-016 revision ("confirm happens at home"); ADR-017 is unchanged (on-box remains the archetype).

**Status**: implemented at the document layer by this ADR. Code-layer work, in order: the static bearer middleware (ships with the next deploy); publishing a golden fixture for downstream consumers; `CMMS_ONBOX_JWKS_URL`; the shared failure-vocabulary slice; `ai_candidate` + badge; `list_work_orders_active_in`; the engineer confirm deep-link page (not urgent before Phase 2).
