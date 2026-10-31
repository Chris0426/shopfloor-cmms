#!/usr/bin/env bash
# Seed a throwaway demo instance with SYNTHETIC data.
#
#   1. alembic upgrade head
#   2. generate the demo CSVs (scripts/generate_demo_data.py)
#   3. run every `cmms` loader in dependency order
#   4. create the demo accounts (admin / engineer / operator)
#   5. play a small live scenario so the timeline + downtime engine have something to show,
#      then the film's fixed story (scripts/seed_demo_story.py: EID-97061, WO-96320/96321)
#
# Safe to re-run: the loaders upsert, and steps 4-5 are skipped once the accounts exist
# (set SEED_FORCE=1 to run them anyway) - except the story, which is idempotent and
# forward-only and therefore runs every time. To start over: `docker compose down -v`.
#
# Ledger mode (parity instance, docker-compose.seed.yml): when SEED_DIR is set and
# non-empty the steps are instead
#   1. alembic upgrade head
#   2. the demo accounts (admin / jordan.lee / operator.a, so someone can log in)
#   3. cmms seed-events "$SEED_DIR" [--until "$SEED_UNTIL"] [--expect-world "$SEED_EXPECT_WORLD"]
#      (area map: env CMMS_SEED_AREA_MAP, read by the CLI itself)
# and the CLI's one-line JSON summary is the last line printed; its exit code is ours.
# No CSV generation, loaders, live scenario or PM generation. Re-runnable: seed-events
# resumes from seed_world, and a later SEED_UNTIL applies only the new events.
#
# Env:
#   CMMS_DATABASE_URL   required (compose sets it)
#   DEMO_DATA_DIR       where the CSVs are written  (default: data/demo)
#   SKIP_MIGRATE=1      do not run alembic
#   SEED_DIR            ledger directory -> ledger mode (see above)
#   SEED_UNTIL, SEED_EXPECT_WORLD   optional, passed to `cmms seed-events` when non-empty
#   DEMO_STORY_UNTIL    optional (non-ledger mode), passed to seed_demo_story.py as --until
#                       (ISO, naive = Asia/Taipei); empty = the whole story
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
OPS_USER=operator.a
OPS_PASS=demo1234

migrate() {
  if [ "${SKIP_MIGRATE:-0}" != "1" ]; then
    alembic upgrade head
  else
    echo "    (skipped)"
  fi
}

if [ -n "${SEED_DIR:-}" ]; then
  # ---- ledger mode: parity instance fed from a generator ledger ----
  echo "==> 1/3 database migrations"
  migrate

  echo "==> 2/3 demo accounts"
  # 帳號已存在時 user-create 會失敗;重跑屬正常,錯誤輸出吞掉、不中斷。
  "$CMMS" user-create "$ADMIN_USER" --username "$ADMIN_USER" --name "Demo Admin" \
      --org plant --role admin --emaint-assignee "Alice Fang" --password "$ADMIN_PASS" 2>/dev/null \
    || echo "    ${ADMIN_USER} already exists"
  "$CMMS" user-create "$ENG_USER" --username "$ENG_USER" --name "Jordan Lee" \
      --org plant --role engineer --emaint-assignee "Jordan Lee" --password "$ENG_PASS" 2>/dev/null \
    || echo "    ${ENG_USER} already exists"
  "$CMMS" user-create "$OPS_USER" --username "$OPS_USER" --name "Zone A Operator" \
      --org plant --role operator --password "$OPS_PASS" 2>/dev/null \
    || echo "    ${OPS_USER} already exists"

  echo "==> 3/3 cmms seed-events ${SEED_DIR}${SEED_UNTIL:+ --until ${SEED_UNTIL}}"
  SEED_ARGS=("$SEED_DIR")
  [ -n "${SEED_UNTIL:-}" ] && SEED_ARGS+=(--until "$SEED_UNTIL")
  [ -n "${SEED_EXPECT_WORLD:-}" ] && SEED_ARGS+=(--expect-world "$SEED_EXPECT_WORLD")
  # stdout = 一行 JSON 摘要(行級錯誤走 stderr);收起來最後再印,保證它是最後一行。
  rc=0
  SUMMARY="$("$CMMS" seed-events "${SEED_ARGS[@]}")" || rc=$?
  [ -n "$SUMMARY" ] && printf '%s\n' "$SUMMARY"
  exit "$rc"
fi

echo "==> 1/5 database migrations"
migrate

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
  "$CMMS" user-create "$OPS_USER" --username "$OPS_USER" --name "Zone A Operator" \
      --org plant --role operator --password "$OPS_PASS" || true

  # Machine owners (admin-only domain op; drives assignee auto-fill + the "Mine" filter)
  "$PY" scripts/assign_demo_owners.py

  echo "==> 5/5 live scenario (governed writes -> status history + downtime)"
  # (a) breakdown whose repair is finished and marked COMPLETED, with a part issued
  #     against it. The agent proposes to CLOSE it (COMPLETED -> CLOSED locks the
  #     downtime); an admin confirms in /admin/proposals. Leaving it COMPLETED (not
  #     IN_PROGRESS) is what makes that close proposal a legal transition.
  WO_A=$("$CMMS" wo-open EID-97001 REACTIVE --user "$ENG_USER" \
      --brief "Aligner-01 will not home on X axis after the morning restart" | awk '{print $3}')
  "$CMMS" wo-transition "$WO_A" start --user "$ENG_USER"
  "$CMMS" wo-issue-part "$WO_A" EC900002 2 --user "$ENG_USER"
  "$CMMS" wo-transition "$WO_A" complete --user "$ENG_USER"
  echo "    WO $WO_A: COMPLETED, 2 x EC900002 issued"

  # (b) breakdown parked while a spare is on order (hold reason drives the downtime rules)
  WO_B=$("$CMMS" wo-open EID-97004 REACTIVE --user "$ENG_USER" \
      --brief "Dispenser-01 tip clogged, no dispense on head 2" | awk '{print $3}')
  "$CMMS" wo-transition "$WO_B" start --user "$ENG_USER"
  "$CMMS" wo-transition "$WO_B" hold --hold-reason WAITING_PARTS --user "$ENG_USER"
  echo "    WO $WO_B: ON_HOLD (WAITING_PARTS)"

  # (c) brand-new report still sitting in the queue
  WO_C=$("$CMMS" wo-open EID-97007 REACTIVE --user "$OPS_USER" \
      --brief "Oven-01 temperature out of range, alarm at 07:40" | awk '{print $3}')
  echo "    WO $WO_C: OPEN (reported by the line operator)"

  # (d) let the PM scheduler generate work orders for everything already overdue
  "$CMMS" pm-generate-due --limit 12

  # (e) two pending proposals waiting for an admin in /admin/proposals (gated writes)
  "$PY" scripts/seed_demo_proposals.py
else
  echo "    accounts already exist - skipping accounts + live scenario (SEED_FORCE=1 overrides)"
fi

# (f) the film's fixed story. Outside the sentinel on purpose: it is idempotent and
#     forward-only, so a re-run fills in whatever is missing. It must come after (d):
#     the PM work orders auto-number from max+1, and the story pins 96320/96321.
echo "==> 5/5 (f) film story${DEMO_STORY_UNTIL:+ --until ${DEMO_STORY_UNTIL}}"
STORY_ARGS=()
[ -n "${DEMO_STORY_UNTIL:-}" ] && STORY_ARGS+=(--until "$DEMO_STORY_UNTIL")
"$PY" scripts/seed_demo_story.py ${STORY_ARGS[@]+"${STORY_ARGS[@]}"}

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
