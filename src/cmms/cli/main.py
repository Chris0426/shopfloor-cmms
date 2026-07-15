"""CLI 入口(typer)。`cmms ...`。

目前只有 `version`;migration 用 alembic、各切片的維運子命令隨切片進來。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import typer

from cmms import __version__

app = typer.Typer(help="cmms 維運 CLI(thin client)。")


@app.command()
def version() -> None:
    """印出版本。"""
    typer.echo(__version__)


@app.command("user-create")
def user_create(
    user_id: str = typer.Argument(..., help="使用者 id(= human:<id>,如 jlee)"),
    username: str = typer.Option(..., "--username", "-u", help="登入帳號"),
    display_name: str = typer.Option(..., "--name", help="顯示名"),
    org: str = typer.Option(..., "--org", help="plant / contractor"),
    role: str = typer.Option("engineer", "--role", help="admin / engineer"),
    emaint_assignee: str = typer.Option(
        None,
        "--emaint-assignee",
        help="legacy 指派名(= 工單 assigned_person,如 'Alice Fang');供「我的」過濾",
    ),
    password: str = typer.Option(
        ..., prompt=True, hide_input=True, confirmation_prompt=True, help="密碼(argon2id)"
    ),
) -> None:
    """建 cmms 本地帳號(ADR-022;bootstrap 首位 admin 用 `--role admin`)。

    密碼互動輸入(prompt + 二次確認)、不入 shell 歷史。
    """
    from cmms.audit import Actor
    from cmms.db import get_sessionmaker
    from cmms.domain.identity.service import IdentityService

    async def _run() -> None:
        async with get_sessionmaker()() as session:
            uid = await IdentityService(session).create_user(
                user_id=user_id,
                username=username,
                display_name=display_name,
                password=password,
                org=org,
                role=role,
                emaint_assignee=emaint_assignee,
                actor=Actor.human("cli"),  # 操作員 bootstrap provenance
            )
        typer.echo(f"created user {uid} (role={role}, org={org})")

    asyncio.run(_run())


@app.command("user-set-assignee")
def user_set_assignee(
    user_id: str = typer.Argument(..., help="使用者 id(= human:<id>,如 jordan.lee)"),
    assignee: str = typer.Argument(
        ...,
        help='legacy 指派名(= 工單/PM assigned_person 確切字串,如 "Jordan Lee");空字串清除',
    ),
) -> None:
    """設使用者的 eMaint 指派名(ADR-022;供「我的工單/保養」過濾,Slice 2)。

    「我的」= work_order/pm_schedule.assigned_person == user_account.emaint_assignee。
    傳空字串清除(該用戶回監督者語意:Mine 空、切 All)。
    """
    from cmms.audit import Actor
    from cmms.db import get_sessionmaker
    from cmms.domain.identity.service import IdentityService

    async def _run() -> None:
        async with get_sessionmaker()() as session:
            await IdentityService(session).set_emaint_assignee(
                user_id, assignee=assignee, actor=Actor.human("cli")
            )
        cleared = not (assignee and assignee.strip())
        typer.echo(
            f"cleared {user_id} emaint_assignee"
            if cleared
            else f"set {user_id} emaint_assignee = {assignee.strip()!r}"
        )

    asyncio.run(_run())


@app.command("load-assets")
def load_assets(
    csv_path: Path = typer.Argument(..., exists=True, help="assets.csv 路徑"),
) -> None:
    """把 assets.csv 載入 DB(migration 資料輸入,idempotent)。"""
    from cmms.db import get_sessionmaker
    from cmms.domain.asset.loader import load, read_rows

    async def _run() -> None:
        rows = read_rows(csv_path)
        async with get_sessionmaker()() as session:
            result = await load(rows, session)
        typer.echo(
            f"loaded {result.assets} assets "
            f"({result.asset_types} types, {result.departments} depts, {result.lines} lines)"
        )

    asyncio.run(_run())


@app.command("load-tasks")
def load_tasks(
    csv_path: Path = typer.Argument(..., exists=True, help="tasks.csv 路徑"),
) -> None:
    """把 tasks.csv 載入 DB(migration 資料輸入,idempotent)。"""
    from cmms.db import get_sessionmaker
    from cmms.domain.task.loader import load, read_rows

    async def _run() -> None:
        rows = read_rows(csv_path)
        async with get_sessionmaker()() as session:
            result = await load(rows, session)
        typer.echo(f"loaded {result.tasks} tasks")

    asyncio.run(_run())


@app.command("load-pm-schedules")
def load_pm_schedules(
    csv_path: Path = typer.Argument(..., exists=True, help="scheduled_activity.csv 路徑"),
) -> None:
    """把 scheduled_activity.csv 載入 DB(idempotent;前置:先載入 assets + tasks)。

    載入後依 SA 引用同步 task.is_active(未引用→false,T3)。
    """
    from cmms.db import get_sessionmaker
    from cmms.domain.pm_schedule.loader import load, read_rows

    async def _run() -> None:
        rows = read_rows(csv_path)
        async with get_sessionmaker()() as session:
            result = await load(rows, session)
        typer.echo(
            f"loaded {result.pm_schedules} pm_schedules "
            f"({result.freq_units} freq_units, {result.vendors} vendors); "
            f"marked {result.idle_tasks_marked} idle tasks inactive"
        )

    asyncio.run(_run())


@app.command("load-work-orders")
def load_work_orders(
    csv_path: Path = typer.Argument(..., exists=True, help="work_orders.csv 路徑(latin-1)"),
) -> None:
    """把 work_orders.csv 載入 DB(idempotent;前置:先載入 assets)。

    miscreated=T(誤開)整列丟棄;狀態 O→OPEN/H→CLOSED;歷史 downtime 由開/關時間估算。
    """
    from cmms.db import get_sessionmaker
    from cmms.domain.work_order.loader import load, read_rows

    async def _run() -> None:
        rows = read_rows(csv_path)
        async with get_sessionmaker()() as session:
            result = await load(rows, session)
        typer.echo(
            f"loaded {result.work_orders} work_orders "
            f"(filtered {result.filtered_miscreated} miscreated; "
            f"{result.work_types} work_types, {result.wo_statuses} statuses, "
            f"{result.wo_hold_reasons} hold_reasons, {result.stock_txn_kinds} txn_kinds, "
            f"{result.vendors} vendors)"
        )

    asyncio.run(_run())


@app.command("wo-open")
def wo_open(
    asset_id: str = typer.Argument(..., help="設備 EID(如 EID-70002)"),
    work_type: str = typer.Argument(..., help="工單類型(如 REACTIVE / PM)"),
    user: str = typer.Option(..., "--user", help="操作者 id(記為 human:<id>)"),
    brief: str = typer.Option(None, "--brief", help="故障/工作簡述"),
) -> None:
    """開立工單(狀態 OPEN)。governed 寫入,記 source_actor=human:<user>。"""
    from cmms.audit import Actor
    from cmms.db import get_sessionmaker
    from cmms.domain.work_order.service import WorkOrderService

    async def _run() -> None:
        async with get_sessionmaker()() as session:
            wo = await WorkOrderService(session).open_work_order(
                asset_id=asset_id,
                work_type=work_type,
                actor=Actor.human(user),
                brief_description=brief,
            )
        typer.echo(f"opened work_order {wo.work_order_no} (OPEN)")

    asyncio.run(_run())


@app.command("wo-transition")
def wo_transition(
    work_order_no: int = typer.Argument(..., help="工單號"),
    action: str = typer.Argument(..., help="start|hold|resume|complete|close|void|cancel"),
    user: str = typer.Option(..., "--user", help="操作者 id(記為 human:<id>)"),
    hold_reason: str = typer.Option(None, "--hold-reason", help="hold 時必填(如 WAITING_PARTS)"),
) -> None:
    """工單狀態轉移(governed)。close 時系統依 status_history 精算 downtime。"""
    from cmms.audit import Actor
    from cmms.db import get_sessionmaker
    from cmms.domain.work_order.service import WorkOrderService

    async def _run() -> None:
        actor = Actor.human(user)
        async with get_sessionmaker()() as session:
            svc = WorkOrderService(session)
            if action == "hold":
                if not hold_reason:
                    raise typer.BadParameter("hold 需 --hold-reason")
                wo = await svc.hold_work(work_order_no, hold_reason, actor)
            else:
                ops = {
                    "start": svc.start_work,
                    "resume": svc.resume_work,
                    "complete": svc.complete_work,
                    "close": svc.close_work_order,
                    "void": svc.void_work_order,
                    "cancel": svc.cancel_reactive_report,
                }
                if action not in ops:
                    raise typer.BadParameter(f"未知 action: {action}")
                wo = await ops[action](work_order_no, actor)
        msg = f"work_order {wo.work_order_no} -> {wo.status}"
        if wo.status == "CLOSED":
            msg += f" (downtime {wo.downtime_minutes} min, estimated={wo.downtime_estimated})"
        typer.echo(msg)

    asyncio.run(_run())


@app.command("wo-issue-part")
def wo_issue_part(
    work_order_no: int = typer.Argument(..., help="工單號"),
    item_code: str = typer.Argument(..., help="品項 code(如 ES000701)"),
    quantity: str = typer.Argument(..., help="領用數量"),
    user: str = typer.Option(..., "--user", help="操作者 id(記為 human:<id>)"),
) -> None:
    """工單領料:記 work_order_part + 經 stock_transaction(ISSUE)扣 on_hand。"""
    from cmms.audit import Actor
    from cmms.db import get_sessionmaker
    from cmms.domain.work_order.service import WorkOrderService

    async def _run() -> None:
        async with get_sessionmaker()() as session:
            ok = await WorkOrderService(session).issue_part_to_work_order(
                work_order_no=work_order_no,
                item_code=item_code,
                quantity=quantity,
                actor=Actor.human(user),
            )
        typer.echo(
            f"issued {quantity} x {item_code} to wo {work_order_no}"
            if ok
            else "skipped (idempotent)"
        )

    asyncio.run(_run())


@app.command("issue-to-asset")
def issue_to_asset(
    asset_id: str = typer.Argument(..., help="設備 EID(如 EID-70004)"),
    item_code: str = typer.Argument(..., help="品項 code(如 ES000701)"),
    quantity: str = typer.Argument(..., help="領用數量"),
    user: str = typer.Option(..., "--user", help="操作者 id(記為 human:<id>)"),
    reason: str = typer.Option(None, "--reason", help="領料事由(稽核 provenance)"),
) -> None:
    """非工單直領(ADR-024):領料歸屬設備、扣 on_hand;不建 work_order_part。governed 寫入。"""
    from cmms.audit import Actor
    from cmms.db import get_sessionmaker
    from cmms.domain.inventory.service import InventoryService

    async def _run() -> None:
        async with get_sessionmaker()() as session:
            ok = await InventoryService(session).issue_to_asset(
                asset_id=asset_id,
                item_code=item_code,
                quantity=quantity,
                actor=Actor.human(user),
                reason=reason,
            )
        typer.echo(
            f"issued {quantity} x {item_code} to asset {asset_id}"
            if ok
            else "skipped (idempotent)"
        )

    asyncio.run(_run())


@app.command("pm-generate-due")
def pm_generate_due(
    as_of: datetime = typer.Option(
        None, "--as-of", formats=["%Y-%m-%d"], help="到期截止日 YYYY-MM-DD(預設廠區當地今天)"
    ),
    limit: int = typer.Option(None, "--limit", help="安全上限:本次最多處理幾筆到期 PM"),
    healthcheck_url: str = typer.Option(
        None,
        "--healthcheck-url",
        envvar="PM_SCHEDULER_HEALTHCHECK_URL",
        help="成功後 ping 此 URL(dead-man's-switch;沒 ping 到 → healthchecks.io 告警)",
    ),
) -> None:
    """自動排程器(ADR-021,unattended):為到期、未 suppress、週期性的 PM 批次生成工單。

    記 source_actor=scheduler;idempotent(同週期未結案不重複生成,重跑安全)。供排程器
    (Fly 排程 machine / cron / Windows 工作排程器)每日呼叫;單筆失敗隔離、不影響其餘。
    `--healthcheck-url`(或環境變數 PM_SCHEDULER_HEALTHCHECK_URL):成功跑完 ping 一次
    (比照 backup dead-man's-switch);ping 失敗被吞、絕不讓排程本身算失敗。
    """
    from cmms.audit import Actor
    from cmms.db import get_sessionmaker
    from cmms.domain.work_order.service import WorkOrderService

    async def _run() -> None:
        async with get_sessionmaker()() as session:
            results = await WorkOrderService(session).generate_due_pm_work_orders(
                actor=Actor.scheduler(),
                as_of=as_of.date() if as_of else None,
                limit=limit,
            )
        created = [r for r in results if r.created]
        existing = [r for r in results if not r.created and r.error is None]
        failed = [r for r in results if r.error is not None]
        typer.echo(
            f"due {len(results)}: {len(created)} generated, "
            f"{len(existing)} already-open, {len(failed)} failed"
        )
        for r in created:
            typer.echo(f"  + wo {r.work_order_no} (pm {r.pm_id})")
        for r in failed:
            typer.echo(f"  ⚠ pm {r.pm_id}: {r.error}")
        # Slice B:排程器開的 PM 工單無 web 活動 → 就地 flush 通知,否則 open 通知會無限等待。
        # flush 失敗不得讓 PM 生成算失敗(誠實 log 到 stderr)。另開 session(上面 with 已關)。
        try:
            from cmms.domain.notify.service import NotificationService

            async with get_sessionmaker()() as nsession:
                n = await NotificationService(nsession).flush_outbox(actor=Actor.scheduler())
            typer.echo(
                f"notify: {n['sent']} sent, {n['failed']} failed, "
                f"{n['skipped_unconfigured']} skipped"
            )
        except Exception as exc:  # noqa: BLE001 — flush 失敗不影響 PM 生成結果
            typer.echo(f"  ⚠ notify flush failed: {type(exc).__name__}: {exc}", err=True)

    asyncio.run(_run())
    # dead-man's-switch:run 未 raise = 排程今天有跑 → ping 心跳。ping 失敗絕不影響排程結果。
    if healthcheck_url:
        import contextlib
        import urllib.request

        with contextlib.suppress(Exception):
            urllib.request.urlopen(healthcheck_url, timeout=10)  # noqa: S310


@app.command("load-inventory")
def load_inventory(
    csv_path: Path = typer.Argument(..., exists=True, help="inventory.csv 路徑(cp1252)"),
) -> None:
    """把 inventory.csv 載入 DB(idempotent;前置:先載入 assets 供 A3 子類型 union)。

    畸形行(欄位數≠20,descrip 內未跳脫英吋符號 ")先自動修復;真正不可救才跳過(A3b)。
    """
    from cmms.db import get_sessionmaker
    from cmms.domain.inventory.loader import load, read_rows

    async def _run() -> None:
        valid, skipped, repaired = read_rows(csv_path)
        async with get_sessionmaker()() as session:
            result = await load(valid, session, skipped=skipped, repaired=repaired)
        typer.echo(
            f"loaded {result.items} inventory_items "
            f"({result.item_categories} categories, {result.asset_subtypes} asset_subtypes; "
            f"links: {result.asset_subtype_links} subtype / {result.alt_links} alt / "
            f"{result.kit_links} kit, {result.orphan_links_skipped} orphan-skipped)"
        )
        if result.repaired_rows:
            typer.echo(
                f"  ✓ repaired {result.repaired_rows} malformed rows (inch-quote): "
                f"{', '.join(result.repaired_items)}"
            )
        if result.skipped_rows:
            typer.echo(
                f"  ⚠ skipped {result.skipped_rows} unrecoverable rows: "
                f"{', '.join(result.skipped_items)}"
            )

    asyncio.run(_run())


@app.command("load-contacts")
def load_contacts(
    csv_path: Path = typer.Argument(..., exists=True, help="contacts.csv 路徑(latin-1)"),
) -> None:
    """把 contacts.csv 載入 DB(idempotent)。organization + person + 保守去重別名 + CMB 種子。"""
    from cmms.db import get_sessionmaker
    from cmms.domain.contacts.loader import load, read_rows

    async def _run() -> None:
        rows = read_rows(csv_path)
        async with get_sessionmaker()() as session:
            result = await load(rows, session)
        typer.echo(
            f"loaded {result.organizations} organizations "
            f"({result.seeded_orgs} seeded e.g. CMB), {result.persons} persons "
            f"({result.aliases} aliases merged); "
            f"{result.org_types} org_types, {result.contact_categories} categories"
        )

    asyncio.run(_run())


@app.command("load-media")
def load_media(
    media_dir: Path = typer.Argument(
        Path("data/media/inventory"),
        exists=True,
        file_okay=False,
        help="媒體資料夾(預設 data/media/inventory)",
    ),
    owner_type: str = typer.Option("inventory_item", "--owner-type", help="附件 owner 類型"),
) -> None:
    """掃媒體夾 → 上傳 R2 → 記 attachment 指標(idempotent;前置:先載入 inventory)。"""
    from cmms.db import get_sessionmaker
    from cmms.domain.attachment.loader import load

    async def _run() -> None:
        async with get_sessionmaker()() as session:
            r = await load(media_dir, session, owner_type=owner_type)
        typer.echo(
            f"scanned {r.scanned}: {r.created} created, {r.existing} existing, "
            f"{r.unmatched} unmatched, {r.unparseable} unparseable; {r.owners} owners"
        )
        if r.unmatched:
            typer.echo(f"  ⚠ unmatched (sample): {', '.join(r.unmatched_samples)}")

    asyncio.run(_run())


@app.command("load-relationships")
def load_relationships_cmd(
    csv_path: Path = typer.Argument(
        Path("data/raw/MES-dependent-equipment-export.csv"),
        exists=True,
        help="Analytics MES dependent-equipment 匯出 CSV(預設 data/raw/…)",
    ),
    include_nonproduction: bool = typer.Option(
        False,
        "--include-nonproduction",
        help="連非生產 IT 資產(少數非生產 IT 資產(如報表 DB))的邊一起載入(預設策展排除,D6)",
    ),
) -> None:
    """載入 ADR-018 資產組成圖真實邊(idempotent;前置:先載入 assets)。

    分類規則見 classify_dependent_equipment(同 child 多 parent → shared,否則 contains);
    端點不在 asset 主檔者依 Q6 跳過(不靜默建資產)。預期(DB-verified):bound 104
    (74 contains + 30 shared)、dropped 112(unknown-eid 104 + guard 1 + curated 7)。
    """
    from cmms.db import get_sessionmaker
    from cmms.domain.asset.loader import (
        CURATED_NONPRODUCTION_SKIP,
        load_relationships,
        read_dependent_equipment_rows,
    )

    async def _run() -> None:
        edges = read_dependent_equipment_rows(csv_path)
        skip = set() if include_nonproduction else set(CURATED_NONPRODUCTION_SKIP)
        async with get_sessionmaker()() as session:
            r = await load_relationships(edges, session, skip_asset_ids=skip)
        typer.echo(
            f"raw {r.raw_edges} → classified {r.classified} → bound {r.bound} "
            f"({r.contains_module} contains + {r.shared_dependency} shared); "
            f"dropped {r.dropped} (unknown-eid {r.skipped_unknown_eid}, "
            f"guard {r.skipped_guard}, curated {r.skipped_curated})"
        )

    asyncio.run(_run())


@app.command("load-part-issues")
def load_part_issues(
    csv_path: Path = typer.Argument(..., exists=True, help="part_issues.csv 路徑(cp1252)"),
) -> None:
    """歷史領料回填(idempotent;前置:先載入 assets + inventory + work_orders 資料)。"""
    from cmms.db import get_sessionmaker
    from cmms.domain.work_order.part_issue_backfill import load, read_rows

    async def _run() -> None:
        rows = read_rows(csv_path)
        async with get_sessionmaker()() as session:
            r = await load(rows, session)
        typer.echo(
            f"backfilled {r.inserted} to WO + {r.rescued_to_asset} rescued-to-asset "
            f"(read {r.rows_read}; {r.duplicates_skipped} duplicate-skip, "
            f"{r.missing_wo_skipped} missing-wo, {r.missing_item_skipped} missing-item, "
            f"{r.malformed_skipped} malformed)"
        )
        if r.rescued_asset_samples:
            typer.echo(f"  ↳ rescued to asset (sample): {', '.join(r.rescued_asset_samples)}")
        if r.missing_wo_samples:
            typer.echo(f"  ⚠ missing WO (sample): {', '.join(r.missing_wo_samples)}")
        if r.missing_item_samples:
            typer.echo(f"  ⚠ missing item (sample): {', '.join(r.missing_item_samples)}")

    asyncio.run(_run())


@app.command("load-mes-failmodes")
def load_mes_failmodes_cmd(
    csv_path: Path = typer.Argument(
        Path("data/raw/mes_failmode_seed.csv"),
        exists=True,
        help="詞彙來源方→cmms mfc 失效模式種子 CSV(預設 data/raw/…)",
    ),
) -> None:
    """載入 C2 mfc 失效模式詞彙(migration 0023;idempotent;additive-only)。

    自然鍵 (station, label);空 label 說明列跳過。預期:113 fail_flag + 3 triage、
    10 說明列跳過。
    """
    from cmms.db import get_sessionmaker
    from cmms.domain.failure_vocab.loader import load_mes_failmodes, read_mes_failmode_rows

    async def _run() -> None:
        rows = read_mes_failmode_rows(csv_path)
        async with get_sessionmaker()() as session:
            r = await load_mes_failmodes(rows, session)
        typer.echo(
            f"read {r.read} → loaded {r.loaded} "
            f"({r.fail_flags} fail_flag + {r.triage_categories} triage_category); "
            f"skipped {r.skipped_doc_rows} doc rows"
        )

    asyncio.run(_run())


@app.command("load-efc-codes")
def load_efc_codes_cmd(
    csv_path: Path = typer.Argument(
        Path("data/raw/efc_equipment_codes.csv"),
        exists=True,
        help="詞彙來源方→cmms efc 設備故障碼種子 CSV(預設 data/raw/…)",
    ),
) -> None:
    """載入 C2 efc 設備故障碼詞彙(migration 0023;idempotent;additive-only)。

    自然鍵 code;常數欄漂移守門(不符即 raise)。預期:107 碼。
    """
    from cmms.db import get_sessionmaker
    from cmms.domain.failure_vocab.loader import load_efc_codes, read_efc_rows

    async def _run() -> None:
        rows = read_efc_rows(csv_path)
        async with get_sessionmaker()() as session:
            r = await load_efc_codes(rows, session)
        typer.echo(f"read {r.read} → loaded {r.loaded} equipment failure codes")

    asyncio.run(_run())


@app.command("load-task-steps")
def load_task_steps(
    csv_path: Path = typer.Argument(
        Path("data/raw/task_steps_parts.csv"),
        exists=True,
        help="task_steps_parts.csv 路徑(cp1252;預設 data/raw/…)",
    ),
) -> None:
    """載入保養細項 task_step/task_part(migration 0016;idempotent;前置:先載 tasks + inventory)。"""
    from cmms.db import get_sessionmaker
    from cmms.domain.task.step_loader import load, read_rows

    async def _run() -> None:
        rows = read_rows(csv_path)
        async with get_sessionmaker()() as session:
            r = await load(rows, session)
        typer.echo(
            f"loaded {r.steps_loaded} steps + {r.parts_loaded} parts (read {r.rows_read}; "
            f"{r.missing_task_skipped} missing-task, {r.missing_item_skipped} missing-item, "
            f"{r.malformed_skipped} malformed)"
        )
        if r.missing_task_samples:
            typer.echo(f"  ⚠ missing task (sample): {', '.join(r.missing_task_samples)}")
        if r.missing_item_samples:
            typer.echo(f"  ⚠ missing item (sample): {', '.join(r.missing_item_samples)}")

    asyncio.run(_run())


@app.command("cred-set")
def cred_set(
    user_id: str = typer.Argument(..., help="使用者 id(= human:<id>)"),
    secret: str = typer.Option(
        ..., prompt=True, hide_input=True, help="外部憑證明文(如 Jira PAT;加密存、不入歷史)"
    ),
    system: str = typer.Option("jira", "--system", help="外部系統(受控;初版 jira)"),
    label: str = typer.Option(None, "--label", help="標籤"),
) -> None:
    """存/換發使用者外部憑證(ADR-022 §5;封套加密,明文不入庫)。需 CMMS_CREDENTIAL_MASTER_KEY。"""
    from cmms.audit import Actor
    from cmms.db import get_sessionmaker
    from cmms.domain.identity.vault import CredentialVault

    async def _run() -> None:
        async with get_sessionmaker()() as session:
            cid = await CredentialVault(session).store_credential(
                user_id=user_id, system=system, secret=secret, label=label,
                actor=Actor.human(user_id),
            )
        typer.echo(f"stored credential {cid} for {user_id} ({system})")

    asyncio.run(_run())


@app.command("cred-revoke")
def cred_revoke(
    user_id: str = typer.Argument(..., help="使用者 id"),
    system: str = typer.Option("jira", "--system"),
) -> None:
    """撤使用者某系統的現行憑證(即時失效)。"""
    from cmms.audit import Actor
    from cmms.db import get_sessionmaker
    from cmms.domain.identity.vault import CredentialVault

    async def _run() -> None:
        async with get_sessionmaker()() as session:
            ok = await CredentialVault(session).revoke(
                user_id=user_id, system=system, actor=Actor.human(user_id)
            )
        typer.echo("revoked" if ok else "nothing to revoke")

    asyncio.run(_run())


@app.command("jira-flush-outbox")
def jira_flush_outbox(
    limit: int = typer.Option(50, "--limit", help="本次最多處理幾列(pending + 可重試 failed)"),
) -> None:
    """兜底送出 note→Jira MRQ comment 佇列(ADR-020 決策 1 修訂;可 cron 定時跑)。

    平常「新增工作紀錄即自動同步」由 web 背景 flush 完成;本命令補送暫時失敗(Jira 逾時 / 主鑰
    當時未設等)的殘留列。逐列獨立、失敗誠實記錄(status/attempts/last_error);未配置 jira /
    無 PAT → 該列標 config-missing / pat-missing,不假成功。source_actor=scheduler。
    """
    from cmms.audit import Actor
    from cmms.db import get_sessionmaker
    from cmms.domain.jira_sync.service import JiraSyncService

    async def _run() -> None:
        async with get_sessionmaker()() as session:
            r = await JiraSyncService(session).flush_outbox(limit=limit, actor=Actor.scheduler())
        typer.echo(f"processed {r.processed}: {r.sent} sent, {r.failed} failed")
        for e in r.errors:
            typer.echo(f"  ⚠ {e}")

    asyncio.run(_run())


@app.command("notify-flush-outbox")
def notify_flush_outbox(
    limit: int = typer.Option(50, "--limit", help="本次最多處理幾列(pending + 可重試 failed)"),
) -> None:
    """兜底送出工單 open/close 通知佇列(Slice B;可 cron 定時跑)。

    平常「開單 / 結案即通知」由 web 背景 flush 完成;本命令補送暫時失敗(SMTP / Telegram 逾時等)
    或當時通道未配置(留 pending)的殘留列。逐列獨立、失敗誠實記錄;未配置通道 → 該列略過
    (留 pending,不燒 attempts)。source_actor=scheduler。
    """
    from cmms.audit import Actor
    from cmms.db import get_sessionmaker
    from cmms.domain.notify.service import NotificationService

    async def _run() -> None:
        async with get_sessionmaker()() as session:
            r = await NotificationService(session).flush_outbox(
                limit=limit, actor=Actor.scheduler()
            )
        typer.echo(
            f"{r['sent']} sent, {r['failed']} failed, "
            f"{r['skipped_unconfigured']} skipped (unconfigured)"
        )

    asyncio.run(_run())


@app.command("mcp-token")
def mcp_token(
    username: str = typer.Option(..., "--username", "-u", help="登入帳號(username)"),
    ttl_hours: float = typer.Option(
        None, "--ttl-hours", help="有效小時數(預設 config CMMS_MCP_PILOT_TOKEN_TTL_SECONDS=12h)"
    ),
    agent: str = typer.Option("codex", "--agent", help="agent 名(稽核 agent:<name>;試點=codex)"),
    scope: str = typer.Option("pilot", "--scope", help="scope 字串(受控;pilot=試點全工具)"),
) -> None:
    """為使用者鑄 per-user MCP token(agent 試點;ADR-020 決策 5)。

    agent(Codex / gateway)以 `Authorization: Bearer <token>` 連 `/mcp`;寫入仍走
    提案→admin confirm(ADR-027 agent 憲法)。**token 明文只印一次(stdout)、絕不落
    log/稽核**;撤銷:`cmms mcp-token-revoke --username <u>`(即時失效)。
    """
    from cmms.db import get_sessionmaker
    from cmms.domain.identity.service import IdentityService

    async def _run() -> None:
        async with get_sessionmaker()() as session:
            token, expires = await IdentityService(session).mint_scoped_token_for_user(
                username=username,
                agent=agent,
                scope=scope,
                ttl_seconds=int(ttl_hours * 3600) if ttl_hours is not None else None,
            )
        typer.echo(f"token: {token}")
        typer.echo(f"expires_at: {expires.isoformat()}")
        typer.echo("(明文只顯示這一次;外洩即撤:cmms mcp-token-revoke --username " f"{username})")

    asyncio.run(_run())


@app.command("mcp-token-revoke")
def mcp_token_revoke(
    username: str = typer.Option(..., "--username", "-u", help="登入帳號(username)"),
) -> None:
    """撤銷使用者**全部**現行 MCP token(設 revoked_at,即時失效;冪等)。"""
    from cmms.db import get_sessionmaker
    from cmms.domain.identity.service import IdentityService

    async def _run() -> None:
        async with get_sessionmaker()() as session:
            n = await IdentityService(session).revoke_scoped_tokens_for_user(username=username)
        typer.echo(f"revoked {n} active mcp token(s) for {username}")

    asyncio.run(_run())


@app.command("backfill-external-links")
def backfill_external_links() -> None:
    """一次性:legacy external_ref 的 MRQ-xxxx → work_order_external_link(referenced;冪等)。"""
    from cmms.audit import Actor
    from cmms.db import get_sessionmaker
    from cmms.domain.work_order.service import WorkOrderService

    async def _run() -> None:
        async with get_sessionmaker()() as session:
            n = await WorkOrderService(session).backfill_legacy_mrq_links(Actor.human("migration"))
        typer.echo(f"backfilled {n} external links (external_ref MRQ-xxxx → referenced)")

    asyncio.run(_run())


@app.command("load-orderqty")
def load_orderqty(
    csv_path: Path = typer.Argument(..., exists=True, help="orderqty.csv 路徑(欄:item,orderqty)"),
) -> None:
    """回填再訂購量 reorder_quantity(ADR-026;idempotent;前置:先載 inventory)。"""
    import csv

    from cmms.audit import Actor
    from cmms.db import get_sessionmaker
    from cmms.domain.inventory.service import InventoryService

    async def _run() -> None:
        with csv_path.open(encoding="cp1252", newline="") as fh:
            rows = list(csv.DictReader(fh))
        set_n = miss = 0
        async with get_sessionmaker()() as session:
            svc = InventoryService(session)
            for r in rows:
                item = (r.get("item") or "").strip()
                qty = (r.get("orderqty") or "").strip()
                if not item or not qty:
                    continue
                if await svc.set_reorder_quantity(item, qty, Actor.human("migration")):
                    set_n += 1
                else:
                    miss += 1
        typer.echo(f"set reorder_quantity on {set_n} items ({miss} missing-item)")

    asyncio.run(_run())


@app.command("link-suppliers")
def link_suppliers() -> None:
    """依 organization.name 自動連結品項 supplier_org_id(ADR-026)。前置:inventory+contacts。"""
    from cmms.audit import Actor
    from cmms.db import get_sessionmaker
    from cmms.domain.inventory.service import InventoryService

    async def _run() -> None:
        async with get_sessionmaker()() as session:
            linked, unmatched = await InventoryService(session).autolink_suppliers(
                Actor.human("migration")
            )
        typer.echo(f"linked {linked} items ({unmatched} unmatched → RFQ-ineligible)")

    asyncio.run(_run())


@app.command("efc-workorder-crosscheck")
def efc_workorder_crosscheck_cmd(
    events_csv: Path = typer.Argument(
        Path("data/raw/efc_events_top20_60d.csv"),
        exists=True,
        help="詞彙來源方→cmms efc 事件 CSV(efc_code,eid,event_timestamp;utf-8-sig)",
    ),
    out: Path = typer.Option(..., "--out", help="輸出種子 JSON 路徑"),
    buffer_days: int = typer.Option(1, "--buffer-days", help="日級窗緩衝(預設 1 天)"),
    as_of: str = typer.Option(
        None, "--as-of", help="OPEN 工單窗尾時刻(ISO;預設執行當下 UTC。指定以利重現)"
    ),
) -> None:
    """產生 efc 碼 × REACTIVE 工單交叉比對種子(下游交付;唯讀;無 migration)。

    比對事件時戳 ∈ 該 EID REACTIVE 非取消/作廢工單活躍窗 ±日級緩衝。逐碼吐
    n_events_total_checked / n_events_matched / ratio / verdict_hint(提示,非權威)。
    §24 紅線:只用工單存在 + 時間窗欄,永不碰人員欄。
    """
    import json

    from cmms.db import get_sessionmaker
    from cmms.domain.failure_vocab.crosscheck import generate_seed, read_efc_events

    now = datetime.now(UTC)
    as_of_dt = datetime.fromisoformat(as_of).astimezone(UTC) if as_of else now

    async def _run() -> dict:
        events_read = read_efc_events(events_csv)
        async with get_sessionmaker()() as session:
            return await generate_seed(
                session,
                events_read,
                events_file=events_csv.name,
                buffer_days=buffer_days,
                as_of=as_of_dt,
                generated_at=now,
            )

    seed = asyncio.run(_run())
    out.write_text(json.dumps(seed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    typer.echo(f"{'code':<40} {'checked':>8} {'matched':>8} {'unknown':>8} {'ratio':>8}  verdict")
    for c in seed["codes"]:
        typer.echo(
            f"{c['code']:<40} {c['n_events_total_checked']:>8} {c['n_events_matched']:>8} "
            f"{c['n_events_unknown_eid']:>8} {c['ratio']:>8.4f}  {c['verdict_hint']}"
        )
    skipped = seed["method"]["n_rows_skipped"]
    typer.echo(f"→ {len(seed['codes'])} codes; {skipped} rows skipped; wrote {out}")


if __name__ == "__main__":
    app()
