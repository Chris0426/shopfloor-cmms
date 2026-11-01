# Windows twin of scripts/seed_demo.sh — for seeding a demo instance WITHOUT Docker
# (you supply the PostgreSQL). The Docker path (`docker compose up --build`) already
# runs the bash version inside the container; you do not need this file for that.
#
#   $env:CMMS_DATABASE_URL = "postgresql+asyncpg://cmms:cmms@localhost:5432/cmms"
#   .\scripts\seed_demo.ps1
#
# Safe to re-run: the loaders upsert; accounts + live scenario are skipped once they exist
# (SEED_FORCE=1 runs them anyway) - except the film's fixed story (scripts/seed_demo_story.py:
# EID-97061, WO-96320/96321), which is idempotent and forward-only and runs every time.
#
# Env:
#   DEMO_STORY_UNTIL   optional, passed to seed_demo_story.py as --until
#                      (ISO, naive = Asia/Taipei); empty = the whole story
#
# Local-dev only: the ledger mode of seed_demo.sh (SEED_DIR, docker-compose.seed.yml) is not
# mirrored here; run `cmms seed-events` directly instead (docs/SYNTHETIC-INSTANCE.md).

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$DataDir = if ($env:DEMO_DATA_DIR) { $env:DEMO_DATA_DIR } else { "data/demo" }
$Py      = if ($env:PYTHON) { $env:PYTHON } else { ".venv\Scripts\python.exe" }

$AdminUser = "admin";        $AdminPass = "admin123"
$EngUser   = "jordan.lee";   $EngPass   = "demo1234"
$OpsUser   = "operator.a";  $OpsPass   = "demo1234"

function Cmms { & $Py -m cmms.cli.main @args }

if (-not $env:CMMS_DATABASE_URL) {
    Write-Host "CMMS_DATABASE_URL is not set (e.g. postgresql+asyncpg://cmms:cmms@localhost:5432/cmms)"
    exit 1
}

Write-Host "==> 1/5 database migrations"
if ($env:SKIP_MIGRATE -ne "1") { & $Py -m alembic upgrade head } else { Write-Host "    (skipped)" }

Write-Host "==> 2/5 generating synthetic CSVs into $DataDir"
& $Py scripts/generate_demo_data.py --out $DataDir

Write-Host "==> 3/5 loading data (dependency order)"
Cmms load-assets       "$DataDir/assets.csv"
Cmms load-tasks        "$DataDir/tasks.csv"
Cmms load-pm-schedules "$DataDir/scheduled_activity.csv"
Cmms load-inventory    "$DataDir/inventory.csv"
Cmms load-contacts     "$DataDir/contacts.csv"
Cmms load-work-orders  "$DataDir/work_orders.csv"
Cmms load-task-steps   "$DataDir/task_steps_parts.csv"
Cmms load-part-issues  "$DataDir/part_issues.csv"
Cmms load-efc-codes    "$DataDir/efc_codes.csv"
Cmms link-suppliers
Cmms backfill-external-links

Write-Host "==> 4/5 demo accounts"
$ErrorActionPreference = "Continue"
Cmms user-create $AdminUser --username $AdminUser --name "Demo Admin" --org plant `
     --role admin --emaint-assignee "Alice Fang" --password $AdminPass
$adminCreated = ($LASTEXITCODE -eq 0)

if ($adminCreated -or $env:SEED_FORCE -eq "1") {
    Cmms user-create $EngUser --username $EngUser --name "Jordan Lee" --org plant `
         --role engineer --emaint-assignee "Jordan Lee" --password $EngPass
    Cmms user-create $OpsUser --username $OpsUser --name "Zone A Operator" --org plant `
         --role operator --password $OpsPass

    # Machine owners (admin-only domain op; drives assignee auto-fill + the "Mine" filter)
    & $Py scripts/assign_demo_owners.py

    Write-Host "==> 5/5 live scenario (governed writes -> status history + downtime)"
    $ErrorActionPreference = "Stop"
    $woA = (Cmms wo-open EID-97001 REACTIVE --user $EngUser `
        --brief "Aligner-01 will not home on X axis after the morning restart").Split(" ")[2]
    Cmms wo-transition $woA start --user $EngUser
    Cmms wo-issue-part $woA EC900002 2 --user $EngUser
    Cmms wo-transition $woA complete --user $EngUser
    Write-Host "    WO ${woA}: COMPLETED, 2 x EC900002 issued"

    $woB = (Cmms wo-open EID-97004 REACTIVE --user $EngUser `
        --brief "Dispenser-01 tip clogged, no dispense on head 2").Split(" ")[2]
    Cmms wo-transition $woB start --user $EngUser
    Cmms wo-transition $woB hold --hold-reason WAITING_PARTS --user $EngUser
    Write-Host "    WO ${woB}: ON_HOLD (WAITING_PARTS)"

    $woC = (Cmms wo-open EID-97007 REACTIVE --user $OpsUser `
        --brief "Oven-01 temperature out of range, alarm at 07:40").Split(" ")[2]
    Write-Host "    WO ${woC}: OPEN (reported by the line operator)"

    Cmms pm-generate-due --limit 12

    # Two pending proposals waiting for an admin in /admin/proposals (gated writes)
    & $Py scripts/seed_demo_proposals.py
} else {
    Write-Host "    accounts already exist - skipping accounts + live scenario (SEED_FORCE=1 overrides)"
}

$ErrorActionPreference = "Stop"

# (f) the film's fixed story. Outside the sentinel on purpose: it is idempotent and
#     forward-only, so a re-run fills in whatever is missing. It must come after the PM
#     generation: the PM work orders auto-number from max+1, and the story pins 96320/96321.
$untilSuffix = if ($env:DEMO_STORY_UNTIL) { " --until $($env:DEMO_STORY_UNTIL)" } else { "" }
Write-Host "==> 5/5 (f) film story$untilSuffix"
$StoryArgs = @()
if ($env:DEMO_STORY_UNTIL) { $StoryArgs += @("--until", $env:DEMO_STORY_UNTIL) }
& $Py scripts/seed_demo_story.py @StoryArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "============================================================"
Write-Host "  Demo ready:  http://localhost:8000"
Write-Host "------------------------------------------------------------"
Write-Host "  admin       $AdminUser / $AdminPass      (full access, /admin)"
Write-Host "  engineer    $EngUser / $EngPass   (day-to-day maintenance)"
Write-Host "  operator    $OpsUser / $OpsPass   (may only report + cancel)"
Write-Host ""
Write-Host "  Read-API bearer token: demo-read-api-token-not-a-secret"
Write-Host '      curl.exe -H "Authorization: Bearer demo-read-api-token-not-a-secret" `'
Write-Host "           http://localhost:8000/work-orders"
Write-Host ""
Write-Host "  All data is synthetic. All credentials are throwaway."
Write-Host "============================================================"
