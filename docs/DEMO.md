# Running the demo

A throwaway instance with **synthetic data**: 60 machines, 120 spare parts, 80 PM
schedules, ~300 work orders spread over the last 18 months, and a few live ones you can
act on. No real company, person, machine or part appears anywhere.

## Bring it up

You need Docker. Then:

```bash
docker compose up
```

That starts PostgreSQL, applies every migration, generates the synthetic CSVs, runs the
loaders, creates the accounts, and plays a short scenario so the timeline has something in
it. The seeder prints the credentials when it finishes; the app is on
**<http://localhost:8000>**.

| account | password | role |
|---|---|---|
| `admin` | `admin123` | admin — everything, plus `/admin` |
| `jordan.lee` | `demo1234` | engineer — the day-to-day persona |
| `operator.1k` | `demo1234` | operator — may only report and cancel |

Throwaway credentials for a throwaway database. Reset any time with:

```bash
docker compose down -v && docker compose up
```

## The five-minute tour

### 1. File a breakdown (mobile)

Log in as **`operator.1k`** and narrow the browser to a phone width (or use the device
toolbar in devtools) — the console switches to a bottom tab bar.

Tap **Report** → pick a machine (type `10012`, the EID autocomplete finds
`EID-10012`) → describe the fault → submit. Note what you did *not* have to do: the
assignee filled itself in from the machine's owner. The work order lands in the queue
immediately.

Now try to close it. You can't: an operator reports and cancels their own mistakes, and
that is all — they are not the ones who decide a machine is fixed. That rule lives in the
domain service, not in the template, so the CLI and the agent can't do it either.

### 2. Work the queue (engineer)

Log in as **`jordan.lee`**. The queue opens on **Mine / active** — the still-open work
assigned to him (roughly half of the live queue; switch to **All** to see the rest). Two
seeded ones are worth opening:

- **WO 20301** — the repair is finished and marked **completed**, with `2 × EC000002`
  issued against it. The part issue moved stock: the item's on-hand went down, and there
  is a `stock_transaction` behind it. It is waiting to be formally closed — which is
  exactly the close the agent proposes in step 3.
- **WO 20302** — on hold, reason **waiting for parts**. The hold reason is not cosmetic:
  it decides whether the clock keeps running. Waiting for a part means the machine is
  down; waiting for a *production window* means the machine is still making product, and
  that time is not downtime. This is the distinction the legacy system got wrong.

Open any closed work order to see the reconstructed timeline, the diagnosis and the
calculated downtime.

### 3. The gated write (the point of the whole thing)

Log in as **`admin`** and open **`/admin/proposals`**. Two writes are waiting:

| proposed by | operation |
|---|---|
| `agent:assistant` | `close_work_order` on WO 20301 |
| `human:jordan.lee` | `void_work_order` on WO 20303 |

Neither has happened. The agent read the work order, decided it looked finished, and got
as far as a **proposal with a dry-run diff** — it cannot execute, and it cannot confirm
its own proposal (identity is bound to the transport, not to a parameter it supplies).
The engineer's void request is the same shape: voiding is an admin's call.

Hit **Confirm** on one. *Now* it happens — and the confirmation re-validates from scratch
rather than trusting the stored payload.

### 4. The audit trail

**`/admin/audit`** — every mutation, with who did it and what kind of actor they were:
`human:jordan.lee`, `human:operator.1k`, `scheduler` (the PM generator opened 12 work
orders on its own), `human:migration` (the loaders). Then go back to the work order you
just confirmed: the confirmation is recorded against *you*, the admin, not against the
agent that asked for it.

### 5. Worth a look

- **`/app/pm`** — preventive maintenance calendar. Overdue schedules are visible; the
  scheduler already generated work orders for 12 of them at seed time. Open a PM task to
  see its step-by-step procedure and the parts each step consumes.
- **`/app/inventory`** — spare parts: low-stock filter, applicable machine types,
  alternates and kits, controlled storage bins. As admin, an RFQ can be sent to a supplier
  (SMTP is not configured, so it will tell you so instead of pretending to send).
- **`/app/equipment`** — the machine list, its owners, and the work-order history per machine.
- **`/app/export`** — CSV export with a live row-count preview.
- **Language** — the console is trilingual: EN / 中文 / Tiếng Việt, switchable per user.

### 6. The read API

The JSON contracts that downstream consumers use are behind a static bearer token — not
because the demo needs it, but because turning it off is exactly the kind of shortcut that
ends up in production:

```bash
curl http://localhost:8000/work-orders                       # 401
curl -H "Authorization: Bearer demo-read-api-token-not-a-secret" \
     http://localhost:8000/work-orders                       # 200
```

## What is deliberately switched off

The demo configures no LLM gateway, no Telegram bot, no Jira, no SMTP and no object
storage. Every one of those features is expected to be *honest* about it rather than
degrade quietly:

- the in-app assistant says **"Assistant isn't enabled yet."** instead of guessing;
- the Telegram webhook returns **503** rather than accepting unauthenticated updates;
- Jira forwarding marks its outbox rows `config-missing` rather than reporting success;
- photo uploads go to an in-memory store instead of a real bucket.

Nothing was weakened to make the demo run. The two secrets it *does* need (the read-API
token and the credential-vault master key) are throwaway values in `docker-compose.yml`,
so the fail-closed checks stay on.

## The data

`scripts/generate_demo_data.py` writes the CSVs in exactly the shape the production
loaders expect — same columns, same legacy encodings (`latin-1`, `cp1252`), same
`MM/DD/YY` dates, same `VENDOR (Person)` assignment strings. It is seeded with a fixed
constant, so everyone gets the same database.

Reproducing the pipeline by hand, or against your own PostgreSQL:

```bash
python scripts/generate_demo_data.py --out data/demo     # write the CSVs
./scripts/seed_demo.sh                                   # migrate + load + accounts
.\scripts\seed_demo.ps1                                  # same, on Windows
```

Re-running the seed is safe: the loaders upsert, and the accounts + live scenario are
skipped once they exist (`SEED_FORCE=1` overrides).

To poke at it directly:

```bash
docker compose exec app cmms --help                # the operator CLI
docker compose exec db psql -U cmms -d cmms        # the database
```
