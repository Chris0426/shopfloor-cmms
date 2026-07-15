# EXECUTION-PLAYBOOK — How This Project Is Actually Built

> The institutionalised output of an architect-level review performed by the highest-capability model available.
> Purpose: **externalise high-level judgement into rules**, so that quality holds even when the work is later
> executed by mid- or low-cost models, or by agents. This file is **long-lived doctrine**; it does not record
> session progress. It operates alongside the hard guardrails in CLAUDE.md; where the two conflict, the
> guardrails win.

---

## 1. Model / Capability Tier Routing (by capability tier, not by model name)

| Tier | Give it | Test | Positive example | Negative example |
|---|---|---|---|---|
| **T1 highest capability** (expensive, scarce) | Contract design and change rulings; writing/overturning ADRs; security and governance review; assembling evidence ahead of a data-semantics ruling; multi-document consistency audits; "replan from scratch" | Getting it wrong **propagates outside this system**, is **irreversible** (schema / contract / external commitment), or requires reasoning about consistency across 5+ long documents simultaneously | The review that produced this file caught "a consumer normalises `status_history` into a shape we never emit, and its fallback hides that" — visible only by reading the emitted contract and the consuming code side by side | "Add a filter chip to the work order queue" does not need T1 |
| **T2 mid tier** (the daily workhorse) | Vertical slice implementation (once the ADR and domain model are nailed down); migrations; tests; web pages; loaders; bug fixes | The spec already exists, verification is mechanical (pytest + `alembic check` + ruff), and the blast radius stays inside a single repo | The admin-surface batch (an Opus subagent implementing to spec) | "Should `hold_reason` be admin-editable?" is a governance ruling, not a T2 implementation task |
| **T3 low tier** (cheap) | Doc sync; filling in i18n catalog strings; count reconciliation; formatting; drafting compressed status banners; running batch scripts | There is a clear diff target, an error is visible at a glance, and the whole thing can be redone in bulk | Compressing old status banners into a historical summary | Editing the scope narrative in REQUIREMENTS (that is a scope ruling) |

**Routing iron rule**: which tier a task belongs to is decided by the **blast radius of an error**, not by the size of the job. A one-line schema change can be T1 (if it touches a consumed contract surface); a thousand lines of UI can be T2.

## 2. "Stop and Rethink" vs "Just an Execution Detail"

**Signals that the *direction* is wrong (stop; escalate to T1 or ask the human owner):**
1. You need to **guess** data semantics to continue (guardrail #8).
2. You are changing the **shape** of any read surface that a downstream consumer is known to consume (the repo keeps a list of these, one golden fixture per surface, under `tests/fixtures/`).
3. You find two authoritative documents contradicting each other (e.g. the "bidirectional state consistency" line in REQUIREMENTS §4 vs its rejection in ADR-011).
4. A test only passes if you **change the assertion** (rather than the implementation).
5. You are fixing the same bug for the third time (the first two fixes treated symptoms, not the cause).
6. Making the feature work requires routing around any hard guardrail.

**Signals it is merely an execution detail (T2 fixes it itself; no escalation):** a failing test whose assertion is correct, a lint error, a mistyped field name, a wrong HTMX swap target, a missing i18n string, a migration chained to the wrong parent revision.

## 3. When to Ask the Human (the ask-a-person test)

**Must ask**: eMaint / factory-practice semantics (the human owner is the only SME); scope changes (whether to do it, not how); external commitments (anything the contract manufacturer, corporate IT, or a downstream consumer has to go along with); risk appetite (e.g. tolerance for PII exposure, go-live timing); anything that costs money.

**Don't ask (decide yourself and record the reasoning)**: technology-choice details (the repo already has a convention); facts that can be settled by an experiment or a test; two equally reversible options (pick one, write down why).

**How to ask**: a plain-language question + your recommendation + the consequence of not taking it. No menu widgets.

## 4. Contract Hardening for Downstream Consumers (★ the core prescription of this review)

Status quo before this doctrine: read contracts were "registered" in prose and relayed by hand. That is a memory system, and memory systems fail quietly.

**The failure that motivated the rule** (generalised; it is a provider-side failure, and the provider is us): a read surface documented only in prose had one field the prose never pinned down — the entries of `status_history`. A consumer, wiring up against the docs rather than a sample, normalised those entries into a shape of its own invention (`{status, at}`) instead of the shape actually emitted (`{from_status, to_status, hold_reason, changed_at, source_actor}`). Every entry then failed to parse and was dropped, the history came out empty, and a pre-existing legacy fallback ("no history → infer it from `opened_at` → `closed_at`") took over. Result: **100% of records silently degraded, and nothing anywhere raised an error.** The mismatch was found by reading the emitted JSON next to the consuming code — not by any test on either side.

Two things were wrong, and only one of them belonged to the consumer: the provider published a shape it never made mechanically checkable.

**The doctrine (three rules, applied in order):**

1. **Golden fixtures live in the repo**: for every read surface that is consumed, produce a **real serialised sample JSON** under `tests/` (e.g. `tests/fixtures/contract_wo_detail.v1.json`), generated and asserted by tests (shape changes → test goes red). Every fixture carries a `schema_id`; a breaking change bumps the version rather than overwriting in place. When a consumer wires up, it **copies that fixture into its own selftest**. Shape change = fixture diff = red CI = a mechanically triggered notice. **This turns "did I remember to tell the consumer?" from a memory problem into a CI problem.**
2. **A version-matching ritual (run on wiring-up day)**: before a consumer goes live, it pulls `/openapi.json` (FastAPI gives you a machine-readable schema for free) plus the golden fixtures, and runs its own normalisation against the real sample. **Any silent point in the contract (a shape the docs never specified) must be raised as an explicit question to the provider; inventing one is forbidden.**
3. **Fallbacks must be observable**: any degrade/fallback path (like the legacy fallback above) must carry counters or alerting semantics — "fallback rate: 100%" has to be *visible*. **Silent degradation is the most dangerous failure mode there is here** (see §7, FP-1).

## 5. Quality Gates (per slice and per deployment)

**Gate A — before touching a contract surface**: walk the consumed read surfaces (one golden fixture each) row by row and ask "does this change the shape?"; if yes → notify the consumer first, then start work; update the golden fixture in the same batch.

**Gate B — slice Definition of Done** (missing any one of these means it is not done):
migration (+ a clean `alembic check`) / domain service (single write path) / API / MCP (if it is a read surface) /
DB tests (testcontainers) / ruff / **domain model updated in sync** / if a read API changed → fixture updated /
if a write was added → audit columns + idempotency key + reject-anonymous check.

**Gate C — before deploying**: the migration chain is linear with a single head; sweep the "watching" list of downstream consumers (is there a "deploy unlocks it" item that should be pushed?); **any new external surface (API / page / endpoint) passes the security checklist (§6)**; if there is a new operator step, it goes into the deployment runbook in the same batch.

**Gate D — after deploying**: have the operator steps (data loads, secrets) actually been run? Anything **not** run gets listed explicitly as "looks broken, but really it is just that X hasn't been run" — there is precedent: a data-load step was skipped, the PM checklist rendered empty, and it was misdiagnosed as a bug.

## 6. External Surface Security Checklist (mandatory for every new surface and every deploy)

> Where this lesson came from: an architectural decision that read surfaces are "open" was written with *agents*
> in mind, and the word *open* quietly changed meaning between the ADR and the deployment. A read API can be
> authenticated in intent and unauthenticated in code, and nothing in a test suite notices the difference.
> Governance review had been focused the whole time on the agent surface — MCP and gated writes — while **the
> ordinary HTTP surface was the one that needed the bearer check.** The rule below exists so that "who can call
> this?" is answered per surface, in code, before it ships — never inferred from an ADR's intent afterwards.

For every externally reachable surface, answer:
1. **Who can reach it?** (public internet / allowlist / private network)
2. **What identity does it require?** (session / token / JWS / deliberately anonymous — and deliberate anonymity must be written into an ADR)
3. **What does it emit? What is the most sensitive field in it?** (PII / credential metadata / presigned URLs / plant data)
4. **Enumeration risk**: if a single-record endpoint is "protected" by needing to know the id, and ids can be enumerated from a list endpoint, it is not protected at all.
5. **Asymmetry with web/admin**: if the web UI requires a login and the JSON API does not, that is a hole, not a layering strategy.

## 7. Failure Pattern Catalogue (scan every row at review time)

| # | Pattern | Instance | Countermeasure |
|---|---|---|---|
| FP-1 | **Silent degradation as a mask**: a fallback swallows a contract mismatch, and the system "looks like it's working" | A consumer's legacy fallback masked a `status_history` shape mismatch (§4) | Fallbacks carry counters; an abnormal fallback rate must surface (§4-3) |
| FP-2 | **Inventing at a contract's silent point**: a shape the docs never specified gets made up rather than asked about | The invented `{status, at}` entry shape (§4) | Silent point = explicit question; match versions against a golden fixture before wiring up (§4-2) |
| FP-3 | **Semantic drift of "open"**: the governance layer's "reads are open" gets amplified by deployment into "open to the public internet" | JSON APIs shipped with zero authentication | Security checklist (§6) folded into Gate C |
| FP-4 | **Offline estimate ≠ database truth** | An asset-relationship load estimated offline at 105 edges; the database, applying its own guards, actually bound 104 | Honest counting; trust the executed echo, and label estimates as estimates |
| FP-5 | **Built-but-blocked pile-up**: an integration surface is finished but its external dependency is unresolved, so it goes unverified for a long time | The agent gateway, credential vault, Jira and RFQ lines all sat finished-but-unverified behind external blockers | Keep a single source of truth for the blocker list; check it every session; if it hasn't moved in >4 weeks, consider demoting it |
| FP-6 | **Documentation entropy**: status notes stack up, old narratives are never cleanly overwritten, and the next session is misled | A status-banner tower in the project instructions; REQUIREMENTS §4 still carrying text that ADR-011 had already overturned | Compress status notes periodically; when a requirement is overturned, **edit the requirements document on the spot** — don't just note it in the ADR |
| FP-7 | **A prose registry with no machine check**: contract registration relies on prose plus human memory | The prose list of consumed read surfaces, before golden fixtures existed | Golden fixtures + CI (§4-1) |
| FP-8 | **Operator rituals never rehearsed**: a backup existing ≠ a backup you can restore | The restore drill stayed "optional" indefinitely | Promote it to a mandatory monthly run; an unverified backup is not a backup (the exact wording of ADR-013) |
| FP-9 | **Editable vocabulary vs allowlist consumers**: the provider lets an admin add enum values; a consumer's allowlist doesn't recognise the new value and drops it | `/admin/vocab` can add `hold_reason` values; the consumer enforces an allowlist | Vocabulary evolution is part of the contract surface; a consumer may treat "unknown value = I don't know", never "unknown value = discard the whole record" |

## 8. Ordering Iron Rules

**Always first**: if semantics are unclear, ask (guardrail #8); before touching a contract surface, pass Gate A; a write feature has reject-anonymous and idempotency assertions in its tests before anything else.  Before deploying, pass the Gate C security checklist.

**Always last**: performance optimisation (the dataset is <100MB; correctness first — this is written policy); abstraction and generalisation (don't abstract before the second instance appears); new user surfaces (not before the existing surfaces have been used by real users).

**Never in parallel**: two slices touching the same contract surface; a schema change and a large refactor in the same batch; subagents editing the same set of files simultaneously (standing rule: ≤2 concurrent, and sequential when they touch the same files).

## 9. Agent Collaboration Doctrine (by capability tier)

- **Lead (T1)**: writes the spec, reviews the diff, runs verification itself, commits, keeps documentation in sync. **Does not hand-write large volumes of code.**
- **Implementer (T2)**: receives a spec (which must contain: the list of target files, the guardrail clauses in play, the relevant domain-model section, and the verification commands); reports back in a fixed three-part format: **what changed (files + behaviour) / verification results (command + output) / residual risk**.
- **Anti-rework**: before starting, read the current state-of-play notes and the blocker list; do not restart a line of work that is stuck on a blocker.
- **Anti-context-drift**: every subagent prompt carries the 8 hard guardrails plus the domain-model section for that slice; **quote the original text, never paraphrase it**.
- **Verification cannot be subcontracted**: the lead re-runs the tests personally and does not take the implementer's "tests pass" claim on faith.

## 10. Six-Month Technical-Debt Early Warning (cheap now, expensive later)

1. **The web routes file bloating**: HTMX server-rendered routes accumulate fast; past roughly 1,500 lines, split into modules by tab (mechanical work, T2/T3).
2. **Three-language i18n drift**: new strings only get added to `en`. Run a periodic "keys aligned across all three catalogs" check (T3 can turn this into a script).
3. **No eMaint decommissioning plan**: the data freeze point during the dual-run period, the cutover date, making eMaint read-only — none of it is documented. This is an **adoption risk**, not a technical one, but in six months it will surface as technical debt in the form of "the two systems don't reconcile."
4. **Database roles**: the application should connect with a least-privilege role rather than an owner role. It is the kind of hardening item that stays on the list precisely because nothing breaks while it is missing.
5. **Bus factor of one**: a single person is the only operator, admin and deployer; at minimum, the deployment runbook must be followable by "someone else who is handed the credentials" (it is close to this today — keep the discipline).
