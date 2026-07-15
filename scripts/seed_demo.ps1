# Windows twin of scripts/seed_demo.sh — for seeding a demo instance WITHOUT Docker
# (you supply the PostgreSQL). The Docker path (`docker compose up --build`) already
# runs the bash version inside the container; you do not need this file for that.
#
#   $env:CMMS_DATABASE_URL = "postgresql+asyncpg://cmms:cmms@localhost:5432/cmms"
#   .\scripts\seed_demo.ps1
#
# Safe to re-run: the loaders upsert; accounts + live scenario are skipped once they exist.

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$DataDir = if ($env:DEMO_DATA_DIR) { $env:DEMO_DATA_DIR } else { "data/demo" }
$Py      = if ($env:PYTHON) { $env:PYTHON } else { ".venv\Scripts\python.exe" }

$AdminUser = "admin";        $AdminPass = "admin123"
$EngUser   = "jordan.lee";   $EngPass   = "demo1234"
$OpsUser   = "operator.1k";  $OpsPass   = "demo1234"

function Cmms { & $Py -m cmms.cli.main @args }

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
    Cmms user-create $OpsUser --username $OpsUser --name "Line 1K Operator" --org plant `
         --role operator --password $OpsPass

    # Machine owners (admin-only domain op; drives assignee auto-fill + the "Mine" filter)
    & $Py scripts/assign_demo_owners.py

    Write-Host "==> 5/5 live scenario (governed writes -> status history + downtime)"
    $ErrorActionPreference = "Stop"
    $woA = (Cmms wo-open EID-10001 REACTIVE --user $EngUser `
        --brief "Aligner-01 will not home on X axis after the morning restart").Split(" ")[2]
    Cmms wo-transition $woA start --user $EngUser
    Cmms wo-issue-part $woA EC000002 2 --user $EngUser

    $woB = (Cmms wo-open EID-10004 REACTIVE --user $EngUser `
        --brief "Dispenser-01 nozzle clogged, no dispense on head 2").Split(" ")[2]
    Cmms wo-transition $woB start --user $EngUser
    Cmms wo-transition $woB hold --hold-reason WAITING_PARTS --user $EngUser

    Cmms wo-open EID-10007 REACTIVE --user $OpsUser `
        --brief "Oven-01 temperature out of range, alarm at 07:40"

    Cmms pm-generate-due --limit 12

    # Two pending proposals waiting for an admin in /admin/proposals (gated writes)
    & $Py scripts/seed_demo_proposals.py
} else {
    Write-Host "    accounts already exist - skipping accounts + live scenario (SEED_FORCE=1 overrides)"
}

$ErrorActionPreference = "Stop"
Write-Host ""
Write-Host "============================================================"
Write-Host "  Demo ready:  http://localhost:8000"
Write-Host "------------------------------------------------------------"
Write-Host "  admin      $AdminUser / $AdminPass"
Write-Host "  engineer   $EngUser / $EngPass"
Write-Host "  operator   $OpsUser / $OpsPass"
Write-Host ""
Write-Host "  All data is synthetic. All credentials are throwaway."
Write-Host "============================================================"
