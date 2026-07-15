# CLAUDE.md

Working instructions for AI agents (and humans) contributing to this repo. Loaded automatically by
Claude Code at session start. Keep it short — the detail lives in `docs/`.

## What this is

`shopfloor-cmms` — a maintenance management system (CMMS) for a manufacturing plant, rebuilt from a
legacy commercial product. It is the system of record for equipment maintenance: assets, work
orders, preventive maintenance, spare parts, suppliers. It exposes an MCP server so that LLM agents
can operate it — under the governance boundary described below.

## Read these before changing anything

1. `docs/ARCHITECTURE.md` — 27 ADRs. The decisions and their consequences.
2. `docs/domain-model/` — the entity models. **This is a living document**: change the schema,
   change the doc in the same commit.
3. `docs/EXECUTION-PLAYBOOK.md` — quality gates. Walk §5 before shipping any slice or any change to
   an external contract.

## Hard guardrails — violating one of these is a bug, not a style preference

1. **Single write path.** Every database mutation goes through a domain service. MCP tools, CLI
   commands, web routes and pipelines do **not** issue raw SQL or naked CRUD.
2. **MCP tools are domain operations.** `close_work_order`, `schedule_pm`. Never `run_sql` or
   `update_table`. If a tool can express an unbounded write, it is the wrong tool.
3. **Read/write separation.** Write operations support dry-run and confirmation. Reads are open.
4. **Audit everything.** Every mutation records who / when / what / why, plus a `source_actor`
   (`human:<id>` / `agent:<name>` / `mes-pipeline` / `scheduler`).
5. **Idempotency.** Anything an agent or a pipeline can trigger carries an idempotency key.
6. **Infrastructure as code.** Deployment config lives in git. AI writes the infrastructure code;
   AI does not act as the production operator.
7. **Never invent data semantics.** If a legacy field's meaning is unclear, it goes in the open
   questions and gets asked. It does not get a plausible guess.

## Method

- **Vertical slices.** One entity or workflow at a time, end to end: migration + domain service +
  API + MCP tool + tests. Not horizontal layers.
- Tests: unit tests for pure transforms (always run), integration tests against a real PostgreSQL
  via testcontainers (skipped when Docker is absent).
- Lint with `ruff`. Both must be green before a commit.

## Conventions

- Code identifiers and commit messages: English.
- Documentation: English. Inline comments and docstrings: Traditional Chinese (the working language
  of the plant — see the note in the README).
- Cost-effectiveness over cleverness. The plant does not care how elegant it is.
