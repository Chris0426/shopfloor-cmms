"""Inventory 切片的 DB 整合測試(testcontainers-postgres)。

本機無 Docker 時自動 skip。驗證:載入、currency USD、enum、A3 canonical lookup(asset +
inventory union + STA1 CALIBRATOR 整併)、3 junction、孤兒邊跳過、below_reorder、idempotent。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.db import Base  # noqa: E402

# 註冊全切片 model,讓 create_all 見完整 FK 圖(inventory.stock_transaction FK→work_order、
# work_order→vendor→pm_schedule→task…),使本檔可單獨跑(比照 migrations/env.py)。
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.asset.loader import load as load_assets  # noqa: E402
from cmms.domain.attachment import models as _att_models  # noqa: E402, F401
from cmms.domain.contacts import models as _contacts_models  # noqa: E402, F401
from cmms.domain.failure_vocab import models as _failvocab_models  # noqa: E402, F401
from cmms.domain.identity import models as _identity_models  # noqa: E402, F401
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.inventory.loader import load as load_inv  # noqa: E402
from cmms.domain.inventory.service import InventoryService  # noqa: E402
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401

ASSET_ROWS = [
    {
        "compid": "EID-001",
        "comp_desc": "Cleaner",
        "assettype": "Production",
        "assetsubtp": "AP CLEANER",
        "department": "EQ",
        "line_no": "10K",
        "available": "Yes",
    },
    {
        "compid": "EID-002",
        "comp_desc": "Printer",
        "assettype": "Production",
        "assetsubtp": "APEX SORTER",
        "department": "EQ",
        "line_no": "10K",
        "available": "Yes",
    },
]


def _inv_row(item, **over):
    base = {
        "item": item,
        "asset_sub": "",
        "sf_desc": "",
        "vpartno": "",
        "descrip": "d",
        "location": "",
        "orderpt": "",
        "onhand": "",
        "cost": "",
        "lead_time": "",
        "obsol": "F",
        "stock": "T",
        "supplier": "",
        "weblink": "",
        "photo": "",
        "parnt_item": "",
        "child_item": "",
        "alt_item": "",
        "comment": "",
    }
    base.update(over)
    return base


INV_ROWS = [
    _inv_row(
        "ES001",
        asset_sub="APEX SORTER, STA1 CALIBRATOR",
        alt_item="ES002",
        child_item="ES002",
        onhand="5.000",
        orderpt="10.000",
        cost="1.50000",
    ),
    _inv_row("ES002", asset_sub="WIREBOND", obsol="T", onhand="100.000", orderpt="0.000"),
    _inv_row("EC003", asset_sub="", alt_item="NOPE999", stock="F"),  # alt 指向不存在 → 孤兒跳過
]


@pytest.fixture
async def session():
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            await load_assets(ASSET_ROWS, s)  # 前置:A3 子類型 union 需要 asset
            yield s
        await engine.dispose()


async def test_load_core_and_a3_lookup(session) -> None:
    res = await load_inv(INV_ROWS, session)
    assert res.items == 3
    assert res.item_categories == 2  # ES, EC
    # A3:canonical lookup = inventory ∪ asset(STA1 CALIBRATOR 整併入 CALIBRATOR STA1)
    assert res.asset_subtypes == 4
    codes = set((await session.execute(text("SELECT code FROM asset_subtype"))).scalars().all())
    # STA1 CALIBRATOR 已整併
    assert codes == {"AP CLEANER", "APEX SORTER", "CALIBRATOR STA1", "WIREBOND"}

    svc = InventoryService(session)
    i1 = await svc.get_item("ES001")
    assert i1 is not None
    assert i1.item_category == "ES" and i1.currency == "USD"
    assert i1.unit_cost == Decimal("1.50000")
    # 子類型 junction(canonical;回傳排序)
    assert await svc.get_applicable_subtypes("ES001") == ["APEX SORTER", "CALIBRATOR STA1"]


async def test_junctions_and_orphan_skip(session) -> None:
    res = await load_inv(INV_ROWS, session)
    svc = InventoryService(session)

    # 替代品:ES001→ES002(已載入)成立;EC003→NOPE999 孤兒跳過
    assert await svc.get_alternatives("ES001") == ["ES002"]
    assert await svc.get_alternatives("EC003") == []
    assert res.orphan_links_skipped >= 1

    # 套件:ES001 child ES002 → (ES001 parent, ES002 child)
    assert await svc.get_kit_children("ES001") == ["ES002"]


async def test_filters(session) -> None:
    await load_inv(INV_ROWS, session)
    svc = InventoryService(session)

    obsolete = await svc.list_items(is_obsolete=True, limit=1000)
    assert {i.item_code for i in obsolete} == {"ES002"}

    below = await svc.list_items(below_reorder=True, limit=1000)
    assert {i.item_code for i in below} == {"ES001"}  # onhand 5 < orderpt 10

    by_subtype = await svc.list_items(asset_subtype="APEX SORTER", limit=1000)
    assert {i.item_code for i in by_subtype} == {"ES001"}

    ec = await svc.list_items(item_category="EC", limit=1000)
    assert {i.item_code for i in ec} == {"EC003"}


async def test_load_is_idempotent(session) -> None:
    await load_inv(INV_ROWS, session)
    res2 = await load_inv(INV_ROWS, session)
    assert res2.items == 3
    svc = InventoryService(session)
    assert len(await svc.list_items(limit=1000)) == 3
    assert await svc.get_applicable_subtypes("ES001") == ["APEX SORTER", "CALIBRATOR STA1"]


# ---- 2026-07-03 批:品項主檔編輯 + 盤點調整(governed;web admin 面)----


async def test_update_item_fields(session) -> None:
    from cmms.audit import Actor
    from cmms.domain.inventory.service import InventoryError

    await load_inv(INV_ROWS, session)
    # supplier 欄限現有值(#7b):NORDIC 需為既存 organization.name
    await session.execute(text(
        "INSERT INTO organization (org_id, name, is_active) VALUES ('NORDIC', 'NORDIC', true)"
    ))
    # bin_location 受控詞彙(0028):B-07 需為既存 active storage_bin
    await session.execute(text(
        "INSERT INTO storage_bin (code, is_active) VALUES ('B-07', true)"
    ))
    await session.commit()
    admin = Actor.human("jordan.lee")
    svc = InventoryService(session)
    item = await svc.update_item(
        "ES001",
        actor=admin,
        name="新品名",
        description="新描述",
        vendor_part_no="VP-9",
        bin_location="B-07",
        reorder_point=Decimal("8"),
        reorder_quantity=Decimal("20"),
        lead_time_weeks=6,
        unit_cost=Decimal("12.5"),
        supplier="NORDIC",
        weblink=None,
        comment=None,
        is_stocked=True,
        is_obsolete=False,
    )
    assert item.name == "新品名" and item.reorder_quantity == Decimal("20")
    assert item.lead_time_weeks == 6 and item.updated_by == admin.value
    with pytest.raises(InventoryError):
        await svc.update_item(
            "NOPE", actor=admin, name=None, description=None, vendor_part_no=None,
            bin_location=None, reorder_point=None, reorder_quantity=None,
            lead_time_weeks=None, unit_cost=None, supplier=None, weblink=None,
            comment=None, is_stocked=True, is_obsolete=False,
        )


async def test_adjust_on_hand_posts_adjust_txn(session) -> None:
    from cmms.audit import Actor
    from cmms.domain.inventory.service import InventoryError
    from cmms.domain.work_order.loader import load as load_wo_lookups

    await load_inv(INV_ROWS, session)
    await load_wo_lookups([], session)  # 種 stock_txn_kind lookup(含 ADJUST)
    admin = Actor.human("jordan.lee")
    svc = InventoryService(session)

    # ES001 onhand 5 → 調成 3(delta −2,ADJUST 落帳、on_hand 連動)
    ok = await svc.adjust_on_hand(
        "ES001", new_quantity="3", reason="盤點少兩顆", actor=admin, idempotency_key="adj1"
    )
    assert ok is True
    item = await svc.get_item("ES001")
    assert item.quantity_on_hand == Decimal("3.000")
    row = (
        await session.execute(
            text("SELECT qty_delta, reason FROM stock_transaction WHERE kind='ADJUST'")
        )
    ).one()
    assert row.qty_delta == Decimal("-2.000") and row.reason == "盤點少兩顆"
    # 冪等:同 key 重送不再動
    again = await svc.adjust_on_hand(
        "ES001", new_quantity="99", reason="dup", actor=admin, idempotency_key="adj1"
    )
    assert again is False
    assert (await svc.get_item("ES001")).quantity_on_hand == Decimal("3.000")
    # 數量未變 → no-op;無事由 → 拒絕
    assert await svc.adjust_on_hand("ES001", new_quantity="3", reason="r", actor=admin) is False
    with pytest.raises(InventoryError):
        await svc.adjust_on_hand("ES001", new_quantity="9", reason="  ", actor=admin)


async def test_review_fix_update_item_guards_and_supplier_link(session) -> None:
    """review f14cf8d S6/W2:主檔數值不得為負(負 reorder_quantity 會流進 RFQ 報量);
    supplier_org_id 併入 update_item 同一交易(有值=驗後連結、空=清除、未提供=不動)。"""
    from cmms.audit import Actor
    from cmms.domain.inventory.service import InventoryError

    await load_inv(INV_ROWS, session)
    await session.execute(text(  # org_type 留 NULL(FK 到 org_type lookup,測試不需種)
        "INSERT INTO organization (org_id, name, is_active) VALUES ('CMB', 'CMB Corp', true)"
    ))
    await session.commit()  # 先落地:後續驗證失敗的 rollback 不能把種子一起洗掉
    admin = Actor.human("jordan.lee")
    svc = InventoryService(session)
    kw = dict(
        actor=admin, name=None, description=None, vendor_part_no=None, bin_location=None,
        reorder_point=None, lead_time_weeks=None, unit_cost=None, supplier=None,
        weblink=None, comment=None, is_stocked=True, is_obsolete=False,
    )
    with pytest.raises(InventoryError, match="negative"):
        await svc.update_item("ES001", reorder_quantity=Decimal("-12"), **kw)
    with pytest.raises(InventoryError, match="negative"):
        await svc.update_item("ES001", reorder_quantity=None,
                              **{**kw, "reorder_point": Decimal("-1")})
    # 有值 → 驗 org 存在後連結(同一交易);未知 org → 整筆拒(主檔欄位也不落)
    item = await svc.update_item("ES001", reorder_quantity=Decimal("20"),
                                 supplier_org_id="CMB", **kw)
    assert item.supplier_org_id == "CMB" and item.reorder_quantity == Decimal("20")
    with pytest.raises(InventoryError, match="not found"):
        await svc.update_item("ES001", reorder_quantity=Decimal("99"),
                              supplier_org_id="NOPE-ORG", **kw)
    assert (await svc.get_item("ES001")).reorder_quantity == Decimal("20")  # 原子:未部分寫入
    # 未提供 → 不動連結;空 → 清除(先前無任何 unlink 路徑,錯的 RFQ 收件者永遠拿不掉)
    await svc.update_item("ES001", reorder_quantity=Decimal("21"), **kw)
    assert (await svc.get_item("ES001")).supplier_org_id == "CMB"
    await svc.update_item("ES001", reorder_quantity=Decimal("21"),
                          supplier_org_id=None, **kw)
    assert (await svc.get_item("ES001")).supplier_org_id is None


async def test_review_fix_adjust_and_issue_reject_negative(session) -> None:
    """review f14cf8d C3/C4:盤點不得調成負數;直領數量必須為正(負數會反向加庫存)。"""
    from cmms.audit import Actor
    from cmms.domain.inventory.service import InventoryError

    await load_inv(INV_ROWS, session)
    admin = Actor.human("jordan.lee")
    svc = InventoryService(session)
    with pytest.raises(InventoryError, match="negative"):
        await svc.adjust_on_hand("ES001", new_quantity="-3", reason="typo", actor=admin)
    with pytest.raises(InventoryError, match="positive"):
        await svc.issue_to_asset(asset_id="EID-001", item_code="ES001",
                                 quantity="-2", actor=admin)
    with pytest.raises(InventoryError, match="positive"):
        await svc.issue_to_asset(asset_id="EID-001", item_code="ES001",
                                 quantity="0", actor=admin)


# ---- 2026-07-05 批 W1(Jordan #9):設備直領改量 / 取消(補償帳 + 決定性取消鍵)----


async def _first_issue_txn_id(session) -> int:
    return (
        await session.execute(
            text("SELECT txn_id FROM stock_transaction WHERE kind='ISSUE' ORDER BY txn_id LIMIT 1")
        )
    ).scalar_one()


async def test_direct_issue_amend_quantity(session) -> None:
    """改量 = 取消原帳(決定性鍵)+ 新量重開(同交易):net 差額連動;多次改量不疊帳。"""
    from cmms.audit import Actor
    from cmms.domain.inventory.service import InventoryError
    from cmms.domain.work_order.loader import load as load_wo_lookups

    await load_inv(INV_ROWS, session)
    await load_wo_lookups([], session)  # 種 stock_txn_kind(ISSUE/RETURN/…)
    actor = Actor.human("tester")
    svc = InventoryService(session)

    # ES002 on_hand 100 → 直領 10 → 90
    await svc.issue_to_asset(asset_id="EID-001", item_code="ES002", quantity="10",
                             actor=actor, idempotency_key="di1")
    txn1 = await _first_issue_txn_id(session)

    # 改成 15:RETURN +10(supersede)+ ISSUE -15 → on_hand 85
    ok = await svc.update_asset_issue_quantity(
        asset_id="EID-001", txn_id=txn1, new_quantity="15", actor=actor,
        idempotency_key="amend1",
    )
    assert ok is True
    assert (await svc.get_item("ES002")).quantity_on_hand == Decimal("85.000")
    # 原帳已 superseded:重送同 nonce → 冪等 False;再改 → 已取消守門
    assert await svc.update_asset_issue_quantity(
        asset_id="EID-001", txn_id=txn1, new_quantity="15", actor=actor,
        idempotency_key="amend1",
    ) is False
    assert (await svc.get_item("ES002")).quantity_on_hand == Decimal("85.000")
    with pytest.raises(InventoryError, match="superseded"):
        await svc.update_asset_issue_quantity(
            asset_id="EID-001", txn_id=txn1, new_quantity="8", actor=actor,
            idempotency_key="amend2",
        )
    assert txn1 in await svc.cancelled_asset_issue_ids([txn1])

    # 後續改量對「新的 live ISSUE 帳」操作:15 → 8(RETURN +15 + ISSUE -8)→ on_hand 92
    live = (
        await session.execute(text(
            "SELECT txn_id FROM stock_transaction WHERE kind='ISSUE' "
            "AND qty_delta = -15 ORDER BY txn_id DESC LIMIT 1"
        ))
    ).scalar_one()
    await svc.update_asset_issue_quantity(
        asset_id="EID-001", txn_id=live, new_quantity="8", actor=actor,
        idempotency_key="amend3",
    )
    assert (await svc.get_item("ES002")).quantity_on_hand == Decimal("92.000")

    # 守門:增量超過庫存拒(net 需求 > on_hand)、<=0 拒、數量未變 no-op、歸屬錯拒
    live2 = (
        await session.execute(text(
            "SELECT txn_id FROM stock_transaction WHERE kind='ISSUE' "
            "AND qty_delta = -8 ORDER BY txn_id DESC LIMIT 1"
        ))
    ).scalar_one()
    with pytest.raises(InventoryError, match="insufficient"):
        await svc.update_asset_issue_quantity(
            asset_id="EID-001", txn_id=live2, new_quantity="200", actor=actor)
    with pytest.raises(InventoryError, match="positive"):
        await svc.update_asset_issue_quantity(
            asset_id="EID-001", txn_id=live2, new_quantity="0", actor=actor)
    assert await svc.update_asset_issue_quantity(
        asset_id="EID-001", txn_id=live2, new_quantity="8", actor=actor) is False
    with pytest.raises(InventoryError, match="not found"):
        await svc.update_asset_issue_quantity(
            asset_id="EID-002", txn_id=live2, new_quantity="5", actor=actor)


async def test_direct_issue_cancel_idempotent(session) -> None:
    """取消 = RETURN 全數回庫(ledger 留兩筆);決定性冪等鍵 → 重複取消/雙擊永遠安全。"""
    from cmms.audit import Actor
    from cmms.domain.inventory.service import InventoryError
    from cmms.domain.work_order.loader import load as load_wo_lookups

    await load_inv(INV_ROWS, session)
    await load_wo_lookups([], session)
    actor = Actor.human("tester")
    svc = InventoryService(session)

    await svc.issue_to_asset(asset_id="EID-001", item_code="ES002", quantity="7",
                             actor=actor, idempotency_key="di2")  # 100 → 93
    txn = await _first_issue_txn_id(session)

    ok = await svc.cancel_asset_issue(asset_id="EID-001", txn_id=txn, actor=actor)
    assert ok is True
    assert (await svc.get_item("ES002")).quantity_on_hand == Decimal("100.000")  # 全數回庫
    # 重複取消 → 冪等 False(決定性鍵,無論呼叫端重送幾次)
    assert await svc.cancel_asset_issue(asset_id="EID-001", txn_id=txn, actor=actor) is False
    assert (await svc.get_item("ES002")).quantity_on_hand == Decimal("100.000")
    assert txn in await svc.cancelled_asset_issue_ids([txn])
    # 已取消帳不可再改量
    with pytest.raises(InventoryError, match="superseded"):
        await svc.update_asset_issue_quantity(
            asset_id="EID-001", txn_id=txn, new_quantity="3", actor=actor)
    # usage 讀取端含 RETURN 補償帳(誠實呈現)
    usage = await svc.list_asset_part_usage(["EID-001"])
    assert {t.kind for t in usage} == {"ISSUE", "RETURN"}
    # 歸屬錯 / 非 ISSUE 帳拒
    with pytest.raises(InventoryError, match="not found"):
        await svc.cancel_asset_issue(asset_id="EID-002", txn_id=txn, actor=actor)


async def test_backfill_direct_issue_not_cancellable_or_amendable(session) -> None:
    """安全(對抗式 verify F-2/F-3):回填的設備直領(missing-wo rescued-to-asset;BACKFILL_ACTOR、
    **從未扣 on_hand**)不可取消/改量 —— RETURN 反灌會憑空灌爆 on_hand。以顯式 source_actor 標記
    擋。"""
    from datetime import UTC, datetime

    from cmms.audit import Actor
    from cmms.domain.inventory.service import InventoryError
    from cmms.domain.work_order.loader import load as load_wo_lookups
    from cmms.domain.work_order.service import (
        BACKFILL_ACTOR,
        PartIssueOutcome,
        WorkOrderService,
    )

    await load_inv(INV_ROWS, session)
    await load_wo_lookups([], session)  # 種 stock_txn_kind lookups(ISSUE FK)
    wsvc = WorkOrderService(session)
    inv = InventoryService(session)
    # missing-wo(999999)+ 有效 asset → 掛設備救援(INSERTED_ASSET,charge_target=EID,不動 on_hand)
    async with wsvc.write(BACKFILL_ACTOR):
        outcome = await wsvc.backfill_part_issue(
            work_order_no=999999, item_code="ES002", quantity="4",
            occurred_at=datetime(2020, 1, 1, tzinfo=UTC), actor=BACKFILL_ACTOR,
            idempotency_key="bfa1", asset_id="EID-001",
        )
    assert outcome is PartIssueOutcome.INSERTED_ASSET
    before = (await inv.get_item("ES002")).quantity_on_hand
    txn = await _first_issue_txn_id(session)
    actor = Actor.human("tester")

    with pytest.raises(InventoryError, match="backfill"):
        await inv.cancel_asset_issue(asset_id="EID-001", txn_id=txn, actor=actor)
    with pytest.raises(InventoryError, match="backfill"):
        await inv.update_asset_issue_quantity(
            asset_id="EID-001", txn_id=txn, new_quantity="2", actor=actor)
    assert (await inv.get_item("ES002")).quantity_on_hand == before  # on_hand 未灌爆


# ---- 2026-07-05 批 W3(Jordan #7):適用機種編輯 / 反查套件 / 供應商欄限現有值 ----


async def _seed_admin_inv(session, user_id="admin1", role="admin"):
    from cmms.audit import Actor
    from cmms.domain.identity.service import IdentityService

    await IdentityService(session).create_user(
        user_id=user_id, username=user_id, display_name=user_id, password="pw-123456",
        org="Shopfloor", actor=Actor.human("bootstrap"), role=role,
    )
    return Actor.human(user_id)


async def test_set_applicable_subtypes_admin(session) -> None:
    """#7d:複選覆寫 junction(admin);未知子類型拒;engineer 拒。"""
    from cmms.domain.identity.service import AuthorizationError
    from cmms.domain.inventory.service import InventoryError

    await load_inv(INV_ROWS, session)
    admin = await _seed_admin_inv(session)
    svc = InventoryService(session)
    # ES001 起始 = CALIBRATOR STA1 + APEX SORTER;覆寫為 WIREBOND + AP CLEANER
    result = await svc.set_applicable_subtypes("ES001", ["WIREBOND", "AP CLEANER"], admin)
    assert result == ["AP CLEANER", "WIREBOND"]  # 回傳排序
    assert await svc.get_applicable_subtypes("ES001") == ["AP CLEANER", "WIREBOND"]
    # 去重 + 保留既有(idempotent 再覆寫同集合)
    assert await svc.set_applicable_subtypes("ES001", ["WIREBOND", "WIREBOND"], admin) == [
        "WIREBOND"
    ]
    assert await svc.get_applicable_subtypes("ES001") == ["WIREBOND"]
    # 清空
    assert await svc.set_applicable_subtypes("ES001", [], admin) == []
    assert await svc.get_applicable_subtypes("ES001") == []
    # 未知子類型 → 拒(整筆不落)
    with pytest.raises(InventoryError, match="not found"):
        await svc.set_applicable_subtypes("ES001", ["BOGUS"], admin)
    # engineer → 拒
    eng = await _seed_admin_inv(session, user_id="eng1", role="engineer")
    with pytest.raises(AuthorizationError):
        await svc.set_applicable_subtypes("ES001", ["WIREBOND"], eng)


async def test_get_parent_kits_reverse(session) -> None:
    """#7e:反查哪些套件含本品(kit 邊有向,parent→child)。"""
    await load_inv(INV_ROWS, session)
    svc = InventoryService(session)
    # ES001 含 ES002(get_kit_children);反向:ES002 屬 ES001
    assert await svc.get_kit_children("ES001") == ["ES002"]
    assert await svc.get_parent_kits("ES002") == ["ES001"]
    assert await svc.get_parent_kits("ES001") == []  # 頂層,無所屬套件


async def test_list_all_asset_subtypes(session) -> None:
    await load_inv(INV_ROWS, session)
    svc = InventoryService(session)
    codes = [s.code for s in await svc.list_all_asset_subtypes()]
    assert set(codes) == {"AP CLEANER", "APEX SORTER", "CALIBRATOR STA1", "WIREBOND"}
    assert codes == sorted(codes)  # 排序


async def test_update_item_supplier_value_must_be_known(session) -> None:
    """#7b:supplier 文字須對得上既存 organization.name 或既存 supplier 文字值;查無 → 拒。"""
    from cmms.audit import Actor
    from cmms.domain.inventory.service import InventoryError

    await load_inv(INV_ROWS, session)
    await session.execute(text(
        "INSERT INTO organization (org_id, name, is_active) VALUES ('VELA', 'Vela Motors', true)"
    ))
    await session.commit()
    admin = Actor.human("jordan.lee")
    svc = InventoryService(session)
    kw = dict(
        actor=admin, name=None, description=None, vendor_part_no=None, bin_location=None,
        reorder_point=None, reorder_quantity=None, lead_time_weeks=None, unit_cost=None,
        weblink=None, comment=None, is_stocked=True, is_obsolete=False,
    )
    # 對上既存 org.name(不分大小寫)→ 通過
    item = await svc.update_item("ES001", supplier="vela motors", **kw)
    assert item.supplier == "vela motors"
    # 品項自身現值(既存 supplier 文字)→ 通過(改別的欄位不因自家 legacy 供應商炸)
    await svc.update_item("ES001", supplier="vela motors", **kw)
    # 對不上任何 org 或既存 supplier → 拒
    with pytest.raises(InventoryError, match="not a known"):
        await svc.update_item("ES001", supplier="Totally Bogus Vendor", **kw)
    # 空 → 允許(清除)
    cleared = await svc.update_item("ES001", supplier=None, **kw)
    assert cleared.supplier is None


# ---- 建新備品(admin-only;create_item)----


async def test_create_item_admin_ok(session) -> None:
    """admin 建新料號(全欄落庫 + 稽核;on_hand 不設、currency 預設 USD、旗標預設)。"""
    admin = await _seed_admin_inv(session)
    await session.execute(text(  # bin_location 受控詞彙(0028):A1 需為既存 active storage_bin
        "INSERT INTO storage_bin (code, is_active) VALUES ('A1', true)"
    ))
    await session.commit()
    svc = InventoryService(session)
    item = await svc.create_item(
        "NEW-001", actor=admin, name="Widget", description="a widget",
        vendor_part_no="VP-1", bin_location="A1",
        reorder_point=Decimal("5"), reorder_quantity=Decimal("10"),
        lead_time_weeks=3, unit_cost=Decimal("1.25"),
        weblink="http://x", comment="c",
    )
    assert item.item_code == "NEW-001"
    assert item.name == "Widget" and item.description == "a widget"
    assert item.vendor_part_no == "VP-1" and item.bin_location == "A1"
    assert item.reorder_point == Decimal("5") and item.reorder_quantity == Decimal("10")
    assert item.lead_time_weeks == 3 and item.unit_cost == Decimal("1.25")
    assert item.quantity_on_hand is None          # 不裸設在庫(ADR-005)
    assert item.currency == "USD"                 # server_default
    assert item.is_stocked is True and item.is_obsolete is False
    assert item.created_by == "human:admin1"
    assert item.updated_by == "human:admin1"
    assert item.source_actor == "human:admin1"
    fetched = await svc.get_item("NEW-001")
    assert fetched is not None and fetched.name == "Widget"


async def test_create_item_normalizes_code(session) -> None:
    """item_code strip + upper:'  pn-abc1 ' → 'PN-ABC1';空文字欄 → None。"""
    admin = await _seed_admin_inv(session)
    svc = InventoryService(session)
    item = await svc.create_item("  pn-abc1 ", actor=admin, name="  Bolt  ", weblink="")
    assert item.item_code == "PN-ABC1"
    assert item.name == "Bolt"                    # strip
    assert item.weblink is None                   # 空字串 → None
    assert await svc.get_item("PN-ABC1") is not None


async def test_create_item_duplicate_rejected(session) -> None:
    """既有料號(不分大小寫)→ InventoryError(create 非 upsert)。"""
    from cmms.domain.inventory.service import InventoryError

    await load_inv(INV_ROWS, session)             # 種 ES001
    admin = await _seed_admin_inv(session)
    svc = InventoryService(session)
    with pytest.raises(InventoryError, match="already exists"):
        await svc.create_item("es001", actor=admin, name="dup")
    assert (await svc.get_item("ES001")).description == "d"  # 原資料未被覆蓋


async def test_create_item_bad_code_rejected(session) -> None:
    """空 / 含空白 / 含路徑破壞字元的 item_code → InventoryError;未落庫。"""
    from cmms.domain.inventory.service import InventoryError

    admin = await _seed_admin_inv(session)
    svc = InventoryService(session)
    for bad in ("", "   ", "A B", "A/B", "A#B", "A%B"):
        with pytest.raises(InventoryError):
            await svc.create_item(bad, actor=admin, name="x")


async def test_create_item_validations(session) -> None:
    """必填 name、數值非負、supplier/org 須既存(#7b);全數擋、不落庫。"""
    from cmms.domain.inventory.service import InventoryError

    await session.execute(text(
        "INSERT INTO organization (org_id, name, is_active) VALUES ('VELA', 'Vela Motors', true)"
    ))
    await session.commit()
    admin = await _seed_admin_inv(session)
    svc = InventoryService(session)
    with pytest.raises(InventoryError):                       # 空 name
        await svc.create_item("V-1", actor=admin, name="  ")
    with pytest.raises(InventoryError, match="negative"):     # 負 reorder_point
        await svc.create_item("V-1", actor=admin, name="x", reorder_point=Decimal("-1"))
    with pytest.raises(InventoryError, match="not found"):    # 未知 org
        await svc.create_item("V-1", actor=admin, name="x", supplier_org_id="NOPE")
    with pytest.raises(InventoryError, match="not a known"):  # 未知自由文字供應商
        await svc.create_item("V-1", actor=admin, name="x", supplier="Bogus Vendor")
    assert await svc.get_item("V-1") is None                  # 全數未落庫
    # 對得上既存 org.name(不分大小寫)→ 通過 + 連動 org_id 驗證存在
    ok = await svc.create_item("V-2", actor=admin, name="x", supplier="vela motors",
                               supplier_org_id="VELA")
    assert ok.supplier == "vela motors" and ok.supplier_org_id == "VELA"


async def test_create_item_requires_admin(session) -> None:
    """engineer 呼叫 → AuthorizationError(domain 強制);未落庫。"""
    from cmms.domain.identity.service import AuthorizationError

    eng = await _seed_admin_inv(session, user_id="eng1", role="engineer")
    svc = InventoryService(session)
    with pytest.raises(AuthorizationError):
        await svc.create_item("ENG-1", actor=eng, name="x")
    assert await svc.get_item("ENG-1") is None


# ---- storage_bin 受控詞彙(add / toggle)+ bin_location 寫入驗證 ----


def _mig_seed_codes():
    """載入 migration 0028 取 seed 常數(create_all 建空表,seed 只由 alembic 送達)。"""
    import importlib.util
    from pathlib import Path

    p = (
        Path(__file__).resolve().parents[1]
        / "migrations" / "versions" / "20260710_0028_storage_bin.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0028_storage_bin", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m._SEED_CODES


def test_storage_bin_migration_seed_count() -> None:
    """migration seed = 一組相異代號(無重複、保留大小寫)。"""
    codes = _mig_seed_codes()
    assert len(codes) == len(set(codes))     # 無重複
    assert len(codes) >= 20                   # 合理規模
    for spot in ("01A", "Drawer"):
        assert spot in codes


async def _seed_bins(session, *pairs) -> None:
    """插入 storage_bin(pairs=(code, is_active));create_all 建空表,測試自種。"""
    for code, active in pairs:
        await session.execute(
            text("INSERT INTO storage_bin (code, is_active) VALUES (:c, :a)"),
            {"c": code, "a": active},
        )
    await session.commit()


async def test_add_storage_bin(session) -> None:
    """admin 新增儲位:strip / 大小寫不敏感查重 / 壞格式拒 / engineer 拒。"""
    from cmms.domain.identity.service import AuthorizationError
    from cmms.domain.inventory.service import InventoryError

    admin = await _seed_admin_inv(session)
    svc = InventoryService(session)
    row = await svc.add_storage_bin("  42A ", actor=admin)      # strip
    assert row.code == "42A" and row.is_active is True
    with pytest.raises(InventoryError, match="already exists"):  # 大小寫不敏感查重(active)
        await svc.add_storage_bin("42a", actor=admin)
    for bad in ("", "  ", "A B", "A/B", "toolongtoolongtoolong1"):
        with pytest.raises(InventoryError):                     # 壞格式
            await svc.add_storage_bin(bad, actor=admin)
    eng = await _seed_admin_inv(session, user_id="eng1", role="engineer")
    with pytest.raises(AuthorizationError):                     # engineer 拒
        await svc.add_storage_bin("99Z", actor=eng)


async def test_add_storage_bin_inactive_collision(session) -> None:
    """撞已停用者 → 誠實訊息(提示到 /admin/vocab 重新啟用,不靜默復活)。"""
    from cmms.domain.inventory.service import InventoryError

    admin = await _seed_admin_inv(session)
    await _seed_bins(session, ("OLD1", False))
    svc = InventoryService(session)
    with pytest.raises(InventoryError, match="deactivated"):
        await svc.add_storage_bin("old1", actor=admin)


async def test_set_storage_bin_active(session) -> None:
    """啟停 toggle:active 清單過濾 / 不存在拒 / engineer 拒。"""
    from cmms.domain.identity.service import AuthorizationError
    from cmms.domain.inventory.service import InventoryError

    admin = await _seed_admin_inv(session)
    await _seed_bins(session, ("02A", True))
    svc = InventoryService(session)
    row = await svc.set_storage_bin_active("02A", is_active=False, actor=admin)
    assert row.is_active is False
    assert [b.code for b in await svc.list_storage_bins()] == []              # active-only 清單
    assert [b.code for b in await svc.list_storage_bins(include_inactive=True)] == ["02A"]
    with pytest.raises(InventoryError, match="not found"):
        await svc.set_storage_bin_active("NOPE", is_active=True, actor=admin)
    eng = await _seed_admin_inv(session, user_id="eng1", role="engineer")
    with pytest.raises(AuthorizationError):
        await svc.set_storage_bin_active("02A", is_active=True, actor=eng)


async def test_create_item_bin_validation(session) -> None:
    """create_item:合法 bin(小寫→canonical)/ 空→None / 非成員拒 / inactive 拒。"""
    from cmms.domain.inventory.service import InventoryError

    admin = await _seed_admin_inv(session)
    await _seed_bins(session, ("02A", True), ("Drawer", True), ("OLD1", False))
    svc = InventoryService(session)
    item = await svc.create_item("B-1", actor=admin, name="x", bin_location="drawer")
    assert item.bin_location == "Drawer"                        # 正規化大小寫
    item2 = await svc.create_item("B-2", actor=admin, name="x", bin_location="  ")
    assert item2.bin_location is None                           # 空 → None
    with pytest.raises(InventoryError, match="not a registered storage bin"):
        await svc.create_item("B-3", actor=admin, name="x", bin_location="ZZZ")
    assert await svc.get_item("B-3") is None                    # 未落庫
    with pytest.raises(InventoryError, match="not a registered"):  # inactive 非 active
        await svc.create_item("B-4", actor=admin, name="x", bin_location="OLD1")


async def test_update_item_bin_validation_and_legacy_passthrough(session) -> None:
    """update_item:合法 bin 正規化 / 非法拒 / legacy 非成員現值原樣放行(不擋無關編輯)。"""
    from cmms.audit import Actor
    from cmms.domain.inventory.service import InventoryError

    await load_inv(INV_ROWS, session)
    await _seed_bins(session, ("02A", True))
    admin = Actor.human("jordan.lee")
    svc = InventoryService(session)
    kw = dict(
        actor=admin, name="n", description=None, vendor_part_no=None,
        reorder_point=None, reorder_quantity=None, lead_time_weeks=None, unit_cost=None,
        supplier=None, weblink=None, comment=None, is_stocked=True, is_obsolete=False,
    )
    it = await svc.update_item("ES001", bin_location="02a", **kw)   # 合法(正規化)
    assert it.bin_location == "02A"
    with pytest.raises(InventoryError, match="not a registered"):   # 非法 bin
        await svc.update_item("ES001", bin_location="ZZZ", **kw)
    # legacy 髒值:直接 model insert 一筆帶裸 "08" 的品項
    await session.execute(text(
        "INSERT INTO inventory_item (item_code, bin_location, is_stocked, is_obsolete, currency) "
        "VALUES ('DIRTY', '08', true, false, 'USD')"
    ))
    await session.commit()
    it2 = await svc.update_item("DIRTY", bin_location="08", **kw)   # 現值原樣放行(改別欄)
    assert it2.bin_location == "08" and it2.name == "n"
    with pytest.raises(InventoryError, match="not a registered"):   # 改成另一非法值 → 拒
        await svc.update_item("DIRTY", bin_location="09", **kw)
