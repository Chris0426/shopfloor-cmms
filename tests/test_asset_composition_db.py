"""ADR-018 資產組成圖的 DB 整合測試(testcontainers-postgres)。

本機無 Docker 時自動 skip。驗證:tblDep 分類載入(含 Q6 未知 EID 跳過)、idempotent、
contains_module 樹後代 + parent_asset_id 快取、單親 / 成環守門、shared_dependency N:M 不捲入
rollup、WO rollup(自身 + 後代模組)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.asset import models as _models  # noqa: E402, F401
from cmms.domain.asset.loader import (  # noqa: E402
    CURATED_NONPRODUCTION_SKIP,
    load,
    load_relationships,
    read_dependent_equipment_rows,
    read_rows,
)
from cmms.domain.asset.service import AssetError, AssetService, UnknownAssetError  # noqa: E402

# 單獨跑本檔時須讓 Base.metadata 收齊有 FK 牽連的表(rollup 用 work_order;work_order→vendor
# →pm_schedule→task→… 一路相依),否則 create_all 會 NoReferencedTableError。比照
# migrations/env.py 註冊全部切片 model(全套件跑時由其他 _db 測試檔順帶註冊,單跑本檔則靠這裡)。
from cmms.domain.attachment import models as _attachment_models  # noqa: E402, F401
from cmms.domain.contacts import models as _contacts_models  # noqa: E402, F401
from cmms.domain.inventory import models as _inventory_models  # noqa: E402, F401
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401

_DATA_RAW = Path(__file__).resolve().parents[1] / "data" / "raw"
_REAL_ASSETS_CSV = _DATA_RAW / "assets.csv"
_REAL_DEP_CSV = _DATA_RAW / "MES-dependent-equipment-export.csv"


def _row(eid: str, desc: str, atype: str = "Production") -> dict[str, str]:
    return {
        "compid": eid,
        "comp_desc": desc,
        "assettype": atype,
        "assetsubtp": "",
        "available": "Yes",
        "department": "EQ",
        "line_no": "Wet Loop",
        "model_no": "",
        "serial_no": "",
    }


# 機台 M ⊃ 模組 1/2;模組 1 ⊃ 子模組 SUB;Aligner(CLK)共用服務 A/B/C;X 無關。
ASSETS = [
    _row("EID-M", "STA1 Gen2 machine"),
    _row("EID-1", "Module 1"),
    _row("EID-2", "Module 2"),
    _row("EID-SUB", "Sub-module under Module 1"),
    _row("EID-CLK", "Aligner shared test head", "Support"),
    _row("EID-A", "ASMB E-test A"),
    _row("EID-B", "ASMB E-test B"),
    _row("EID-C", "PROBER C"),
    _row("EID-X", "Unrelated machine"),
]

# (parent, child) — 比照 dependent-equipment export raw 形狀
EDGES = [
    ("EID-M", "EID-1"),  # contains
    ("EID-M", "EID-2"),  # contains
    ("EID-1", "EID-SUB"),  # nested contains
    ("EID-A", "EID-CLK"),  # CLK 多 parent → shared
    ("EID-B", "EID-CLK"),
    ("EID-C", "EID-CLK"),
    ("EID-M", "EID-GHOST"),  # 未知 EID(不在主檔)→ Q6 skip
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
            await load(ASSETS, s)
            # 種子 relationship_type lookup:create_all 不跑 migration 的 bulk_insert,
            # 直接 link_* 的測試需 FK 目標存在(比照 migration 0009 / loader 種子)。
            svc = AssetService(s)
            async with svc.write(Actor.human("fixture")):
                for code, label in (
                    ("contains_module", "機台內含模組(containment,1:N 樹)"),
                    ("shared_dependency", "共用資源服務機台(N:M 圖)"),
                ):
                    await svc.upsert_relationship_type(code, label)
            yield s
        await engine.dispose()


async def test_classify_load_descendants_and_parent_cache(session) -> None:
    r1 = await load_relationships(EDGES, session)
    assert (r1.contains_module, r1.shared_dependency) == (3, 3)  # M⊃1,M⊃2,1⊃SUB ; CLK→A/B/C
    assert r1.skipped_unknown_eid == 1  # EID-GHOST
    assert r1.skipped_guard == 0 and r1.skipped_curated == 0

    svc = AssetService(session)
    # 樹後代:M → {1, 2, SUB}(含巢狀);shared_dependency 不算後代
    assert set(await svc.get_contained_descendants("EID-M")) == {"EID-1", "EID-2", "EID-SUB"}
    assert set(await svc.get_contained_descendants("EID-1")) == {"EID-SUB"}
    assert await svc.get_contained_descendants("EID-CLK") == []  # 共用資源無「後代」

    # parent_asset_id 單親快取落地
    assert (await svc.get_asset("EID-1")).parent_asset_id == "EID-M"
    assert (await svc.get_asset("EID-SUB")).parent_asset_id == "EID-1"
    assert (await svc.get_asset("EID-A")).parent_asset_id is None  # shared 不寫快取

    # idempotent 重跑:現行邊不重複
    r2 = await load_relationships(EDGES, session)
    assert (r2.contains_module, r2.shared_dependency) == (3, 3)
    rels_M = await svc.list_relationships("EID-M", relationship_type="contains_module")
    assert len(rels_M) == 2  # 仍只 2 條(M⊃1, M⊃2)


async def test_single_parent_and_cycle_guards(session) -> None:
    svc = AssetService(session)
    async with svc.write(Actor.human("curator")):
        await svc.link_containment("EID-M", "EID-1", Actor.human("curator"))
        # 單親:模組 1 已屬 M,再掛到 X → 拒
        with pytest.raises(AssetError):
            await svc.link_containment("EID-X", "EID-1", Actor.human("curator"))
        # 成環:M ⊃ 1 後,1 ⊃ M → 拒
        with pytest.raises(AssetError):
            await svc.link_containment("EID-1", "EID-M", Actor.human("curator"))
        # 未知 EID → UnknownAssetError(AssetError 子類)
        with pytest.raises(UnknownAssetError):
            await svc.link_containment("EID-M", "EID-GHOST", Actor.human("curator"))


async def test_curated_skip_excludes_edges(session) -> None:
    # 模擬 下游交付 的非生產 IT 資產過濾:skip_asset_ids 排除 CLK 的共用邊
    r = await load_relationships(EDGES, session, skip_asset_ids={"EID-CLK"})
    assert r.shared_dependency == 0  # CLK 三條全被策展排除
    assert r.skipped_curated == 3
    assert r.contains_module == 3  # 含模組關係不受影響


async def test_rollup_includes_descendants_excludes_shared_and_unrelated(session) -> None:
    await load_relationships(EDGES, session)
    # 種 work_type / wo_status lookup + 幾張 WO(機台/模組/共用/無關各一)
    await session.execute(
        text("INSERT INTO work_type (code, label) VALUES ('REACTIVE','reactive')")
    )
    await session.execute(text("INSERT INTO wo_status (code, label) VALUES ('OPEN','open')"))
    for no, eid in [(1, "EID-M"), (2, "EID-1"), (3, "EID-SUB"), (4, "EID-A"), (5, "EID-X")]:
        await session.execute(
            text(
                "INSERT INTO work_order (work_order_no, asset_id, work_type, status, opened_date) "
                "VALUES (:n, :e, 'REACTIVE', 'OPEN', '2026-01-01')"
            ),
            {"n": no, "e": eid},
        )
    await session.commit()

    wos = await AssetService(session).rollup_work_orders("EID-M")
    got = {w.work_order_no for w in wos}
    # 自身(1)+ 後代模組 1(2)+ 巢狀 SUB(3);不含 shared CLK 服務的 A(4)、無關 X(5)
    assert got == {1, 2, 3}


async def test_transitive_containment_cycle_guard(session) -> None:
    # 多跳成環:M⊃1⊃SUB 後,SUB⊃M 須由 _would_create_cycle 的多跳祖先走查擋下(非單跳)。
    svc = AssetService(session)
    async with svc.write(Actor.human("curator")):
        await svc.link_containment("EID-M", "EID-1", Actor.human("curator"))
        await svc.link_containment("EID-1", "EID-SUB", Actor.human("curator"))
        with pytest.raises(AssetError):
            await svc.link_containment("EID-SUB", "EID-M", Actor.human("curator"))


@pytest.mark.skipif(
    not (_REAL_ASSETS_CSV.exists() and _REAL_DEP_CSV.exists()),
    reason="real plant data is not shipped in the public repo",
)
async def test_real_data_reconciliation() -> None:
    """落 DB 對帳:真實 assets.csv(687)+ Analytics 邊匯出 → 綁 **104**(74 contains + 30 shared)、
    dropped **112**(unknown-eid 104 + guard 1 + curated 7)。D6 策展排除少數非生產 IT 資產。

    ★ 此為**真 DB 載入**的權威數,修正 2026-06-27 offline 估計(105/111)—— offline 純函式
    無法重放 service 的成環/單親守門。差 1 = 下游交付 預警的雙向邊(Curer9↔Aligner9,
    EID-70005↔EID-70006):第一向綁成 containment,反向觸發成環守門 → 計入 guard。
    classify 階段(151/65)不變,見 test_asset_composition_transform。
    本機無 Docker 自動 skip;CI 有 Docker 會跑。部署載入走同一條路徑(CLI 包它)。
    """
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            await load(read_rows(_DATA_RAW / "assets.csv"), s)
            edges = read_dependent_equipment_rows(_DATA_RAW / "MES-dependent-equipment-export.csv")
            r = await load_relationships(edges, s, skip_asset_ids=set(CURATED_NONPRODUCTION_SKIP))
            # idempotent:重跑數字不變、不重複現行邊
            r2 = await load_relationships(edges, s, skip_asset_ids=set(CURATED_NONPRODUCTION_SKIP))
        await engine.dispose()

    assert r.raw_edges == 502
    assert r.classified == 216
    assert (r.contains_module, r.shared_dependency) == (74, 30)
    assert r.bound == 104
    assert (r.skipped_unknown_eid, r.skipped_guard, r.skipped_curated) == (104, 1, 7)
    assert r.dropped == 112
    assert r.bound + r.dropped == r.classified  # 守恆:每條分類邊都有去處
    # 重跑一致(idempotent)
    assert (r2.contains_module, r2.shared_dependency) == (74, 30)


async def test_unlink_softdeletes_clears_cache_and_enables_reparent(session) -> None:
    svc = AssetService(session)
    async with svc.write(Actor.human("curator")):
        await svc.link_containment("EID-M", "EID-1", Actor.human("curator"))
    assert (await svc.get_asset("EID-1")).parent_asset_id == "EID-M"
    rel_id = (await svc.list_relationships("EID-1", relationship_type="contains_module"))[0].id

    # soft-unlink:valid_to 落、現行邊不再列出、單親快取清空(保留歷史)。
    async with svc.write(Actor.human("curator")):
        await svc.unlink_relationship(rel_id, Actor.human("curator"))
    assert (await svc.get_asset("EID-1")).parent_asset_id is None
    assert await svc.list_relationships("EID-1", relationship_type="contains_module") == []
    closed = await svc.list_relationships(
        "EID-1", relationship_type="contains_module", active_only=False
    )
    assert len(closed) == 1 and closed[0].valid_to is not None

    # re-parent escape hatch:解除後可改掛 X(單親守門此時放行)。
    async with svc.write(Actor.human("curator")):
        await svc.link_containment("EID-X", "EID-1", Actor.human("curator"))
    assert (await svc.get_asset("EID-1")).parent_asset_id == "EID-X"

    # idempotent:對已關閉的 rel 再 unlink = no-op(不擲例外、不動 X 快取)。
    async with svc.write(Actor.human("curator")):
        await svc.unlink_relationship(rel_id, Actor.human("curator"))
    assert (await svc.get_asset("EID-1")).parent_asset_id == "EID-X"
