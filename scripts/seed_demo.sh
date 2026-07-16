#!/usr/bin/env bash
# Seed a throwaway demo instance with SYNTHETIC data.
#
#   1. alembic upgrade head
#   2. generate the demo CSVs (scripts/generate_demo_data.py)
#   3. run every `cmms` loader in dependency order
#   4. create the demo accounts (admin / engineer / operator)
#   5. play a small live scenario so the timeline + downtime engine have something to show
#
# Safe to re-run: the loaders upsert, and steps 4-5 are skipped once the accounts exist
# (set SEED_FORCE=1 to run them anyway). To start over: `docker compose down -v`.
#
# Env:
#   CMMS_DATABASE_URL   required (compose sets it)
#   DEMO_DATA_DIR       where the CSVs are written  (default: data/demo)
#   SKIP_MIGRATE=1      do not run alembic
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DATA_DIR="${DEMO_DATA_DIR:-data/demo}"
PY="${PYTHON:-python}"
CMMS="${CMMS_BIN:-cmms}"

ADMIN_USER=admin
ADMIN_PASS=admin123
ENG_USER=jordan.lee
ENG_PASS=demo1234
OPS_USER=operator.1k
OPS_PASS=demo1234

echo "==> 1/5 database migrations"
if [ "${SKIP_MIGRATE:-0}" != "1" ]; then
  alembic upgrade head
else
  echo "    (skipped)"
fi

echo "==> 2/5 generating synthetic CSVs into ${DATA_DIR}"
"$PY" scripts/generate_demo_data.py --out "$DATA_DIR"

echo "==> 3/5 loading data (dependency order)"
"$CMMS" load-assets        "${DATA_DIR}/assets.csv"
"$CMMS" load-tasks         "${DATA_DIR}/tasks.csv"
"$CMMS" load-pm-schedules  "${DATA_DIR}/scheduled_activity.csv"   # needs assets + tasks
"$CMMS" load-inventory     "${DATA_DIR}/inventory.csv"            # needs assets (subtype union)
"$CMMS" load-contacts      "${DATA_DIR}/contacts.csv"
"$CMMS" load-work-orders   "${DATA_DIR}/work_orders.csv"          # needs assets
"$CMMS" load-task-steps    "${DATA_DIR}/task_steps_parts.csv"     # needs tasks + inventory
"$CMMS" load-part-issues   "${DATA_DIR}/part_issues.csv"          # needs assets+inventory+WOs
"$CMMS" load-efc-codes     "${DATA_DIR}/efc_codes.csv"            # failure-code vocabulary
"$CMMS" link-suppliers                                            # inventory.supplier -> org
"$CMMS" backfill-external-links                                   # legacy MRQ-#### refs

echo "==> 4/5 demo accounts"
# The admin account doubles as the "have we already seeded?" sentinel: if it exists,
# creating it fails and we skip the accounts + the live scenario (which is not
# idempotent). Its error output is noise on a re-run, so it is swallowed here.
if "$CMMS" user-create "$ADMIN_USER" --username "$ADMIN_USER" --name "Demo Admin" \
      --org plant --role admin --emaint-assignee "Alice Fang" --password "$ADMIN_PASS" \
      2>/dev/null \
   || [ "${SEED_FORCE:-0}" = "1" ]; then

  "$CMMS" user-create "$ENG_USER" --username "$ENG_USER" --name "Jordan Lee" \
      --org plant --role engineer --emaint-assignee "Jordan Lee" --password "$ENG_PASS" || true
  "$CMMS" user-create "$OPS_USER" --username "$OPS_USER" --name "Line 1K Operator" \
      --org plant --role operator --password "$OPS_PASS" || true

  # Machine owners (admin-only domain op; drives assignee auto-fill + the "Mine" filter)
  "$PY" scripts/assign_demo_owners.py

  echo "==> 5/5 live scenario (governed writes -> status history + downtime)"
  # (a) breakdown whose repair is finished and marked COMPLETED, with a part issued
  #     against it. The agent proposes to CLOSE it (COMPLETED -> CLOSED locks the
  #     downtime); an admin confirms in /admin/proposals. Leaving it COMPLETED (not
  #     IN_PROGRESS) is what makes that close proposal a legal transition.
  WO_A=$("$CMMS" wo-open EID-10001 REACTIVE --user "$ENG_USER" \
      --brief "Aligner-01 will not home on X axis after the morning restart" | awk '{print $3}')
  "$CMMS" wo-transition "$WO_A" start --user "$ENG_USER"
  "$CMMS" wo-issue-part "$WO_A" EC000002 2 --user "$ENG_USER"
  "$CMMS" wo-transition "$WO_A" complete --user "$ENG_USER"
  echo "    WO $WO_A: COMPLETED, 2 x EC000002 issued"

  # (b) breakdown parked while a spare is on order (hold reason drives the downtime rules)
  WO_B=$("$CMMS" wo-open EID-10004 REACTIVE --user "$ENG_USER" \
      --brief "Dispenser-01 nozzle clogged, no dispense on head 2" | awk '{print $3}')
  "$CMMS" wo-transition "$WO_B" start --user "$ENG_USER"
  "$CMMS" wo-transition "$WO_B" hold --hold-reason WAITING_PARTS --user "$ENG_USER"
  echo "    WO $WO_B: ON_HOLD (WAITING_PARTS)"

  # (c) brand-new report still sitting in the queue
  WO_C=$("$CMMS" wo-open EID-10007 REACTIVE --user "$OPS_USER" \
      --brief "Oven-01 temperature out of range, alarm at 07:40" | awk '{print $3}')
  echo "    WO $WO_C: OPEN (reported by the line operator)"

  # (d) let the PM scheduler generate work orders for everything already overdue
  "$CMMS" pm-generate-due --limit 12

  # (e) two pending proposals waiting for an admin in /admin/proposals (gated writes)
  "$PY" scripts/seed_demo_proposals.py
else
  echo "    accounts already exist - skipping accounts + live scenario (SEED_FORCE=1 overrides)"
fi

cat <<EOF

============================================================
  Demo ready:  http://localhost:8000
------------------------------------------------------------
  admin       ${ADMIN_USER} / ${ADMIN_PASS}      (full access, /admin)
  engineer    ${ENG_USER} / ${ENG_PASS}   (day-to-day maintenance)
  operator    ${OPS_USER} / ${OPS_PASS}   (may only report + cancel)

  Read-API bearer token: demo-read-api-token-not-a-secret
      curl -H "Authorization: Bearer demo-read-api-token-not-a-secret" \\
           http://localhost:8000/work-orders

  All data is synthetic. All credentials are throwaway.
============================================================
EOF
