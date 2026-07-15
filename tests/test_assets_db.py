"""Asset 切片的 DB 整合測試(testcontainers-postgres)。

本機無 Docker 時自動 skip;CI 有 postgres service / Docker 時執行。
驗證:載入器 idempotent、讀取、產線正規化落地、身分解析(ADR-015)。
"""

from __future__ import annotations

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.asset import models as _models  # noqa: E402, F401
from cmms.domain.asset.loader import load  # noqa: E402
from cmms.domain.asset.service import (  # noqa: E402
    AssetError,
    AssetService,
    UnknownAssetError,
)
from cmms.domain.identity.service import (  # noqa: E402
    AuthorizationError,
    IdentityService,
)

ROWS = [
    {
        "compid": "EID-001",
        "comp_desc": "Cleaner #1",
        "assettype": "Production",
        "assetsubtp": "AP CLEANER",
        "available": "No",
        "department": "EQ",
        "line_no": "Wet Loop",
        "model_no": "M1",
        "serial_no": "S1",
    },
    {
        "compid": "EID-002",
        "comp_desc": "Cleaner #2",
        "assettype": "Production",
        "assetsubtp": "AP CLEANER",
        "available": "Yes",
        "department": "EQ",
        "line_no": "Wet loop",
        "model_no": "M2",
        "serial_no": "S2",  # 大小寫變體
    },
    {
        "compid": "EID-003",
        "comp_desc": "Caliper",
        "assettype": "Meter",
        "assetsubtp": "",
        "available": "Yes",
        "department": "QA",
        "line_no": "IQC",
        "model_no": "",
        "serial_no": "",
    },
]


@pytest.fixture
async def session():
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # 種子 namespace(正式環境由 migration 種;此處 create_all 不含種子)
            await conn.exec_driver_sql(
                "INSERT INTO external_id_namespace (code, label) VALUES "
                "('mes_equipment','MES equipment id'),('layer_b_sensor','sensor')"
            )
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            yield s
        await engine.dispose()


async def test_load_is_idempotent_and_normalizes(session) -> None:
    r1 = await load(ROWS, session)
    assert r1.assets == 3
    assert r1.departments == 2  # EQ, QA
    assert r1.lines == 2  # Wet Loop, IQC(兩個 Wet* 收斂成一)

    # 再跑一次不應重複
    r2 = await load(ROWS, session)
    assert r2.assets == 3
    svc = AssetService(session)
    assert len(await svc.list_assets(limit=1000)) == 3

    # 產線正規化落地:兩筆變體都成 "Wet Loop"
    wet = await svc.list_assets(line="Wet Loop", limit=1000)
    assert {a.asset_id for a in wet} == {"EID-001", "EID-002"}


async def test_reads_and_audit(session) -> None:
    await load(ROWS, session)
    svc = AssetService(session)

    a = await svc.get_asset("EID-001")
    assert a is not None
    assert a.available_for_service is False
    assert a.is_active is True  # server_default
    assert a.source_actor == "human:migration"  # 稽核(ADR-005)

    meters = await svc.list_assets(asset_type="Meter", limit=1000)
    assert [m.asset_id for m in meters] == ["EID-003"]


async def test_list_assets_search(session) -> None:
    """設備查詢自由文字(Slice 3):EID / 描述 ilike。"""
    await load(ROWS, session)
    svc = AssetService(session)
    # 描述 ilike(大小寫不敏感)
    by_desc = await svc.list_assets(search="cleaner", limit=1000)
    assert {a.asset_id for a in by_desc} == {"EID-001", "EID-002"}
    # EID ilike
    by_eid = await svc.list_assets(search="eid-003", limit=1000)
    assert [a.asset_id for a in by_eid] == ["EID-003"]
    # 無命中 → 空
    assert await svc.list_assets(search="zzz-none", limit=1000) == []


async def test_identity_resolution(session) -> None:
    await load(ROWS, session)
    svc = AssetService(session)

    async with svc.write(Actor.agent("analytics")):
        await svc.register_external_id(
            "EID-001", "mes_equipment", "EID-001", Actor.agent("analytics")
        )
        await svc.register_external_id(
            "EID-001", "layer_b_sensor", "CT-77", Actor.agent("analytics")
        )

    resolved = await svc.resolve_by_external_id("layer_b_sensor", "CT-77")
    assert resolved is not None and resolved.asset_id == "EID-001"

    ext = await svc.list_external_ids("EID-001")
    assert {(e.namespace, e.external_id) for e in ext} == {
        ("mes_equipment", "EID-001"),
        ("layer_b_sensor", "CT-77"),
    }


async def _seed_admin(session, user_id: str = "admin1", role: str = "admin") -> Actor:
    """種一個 active 帳號(啟停旗標的 domain 角色守門需要;比照 test_work_orders_db)。"""
    await IdentityService(session).create_user(
        user_id=user_id, username=user_id, display_name=user_id, password="pw-123456",
        org="Shopfloor", actor=Actor.human("bootstrap"), role=role,
    )
    return Actor.human(user_id)


async def test_set_asset_flags_admin(session) -> None:
    """admin 啟停 available_for_service / is_active(治理寫入 + 稽核);冪等同值 no-op。"""
    await load(ROWS, session)
    admin = await _seed_admin(session)
    svc = AssetService(session)
    # EID-001 available="No" → available_for_service=False;admin 設為 True
    a = await svc.set_available_for_service("EID-001", True, actor=admin)
    assert a.available_for_service is True
    assert a.updated_by == "human:admin1"            # 稽核欄(ADR-005)
    # 冪等:同值 no-op
    await svc.set_available_for_service("EID-001", True, actor=admin)
    assert (await svc.get_asset("EID-001")).available_for_service is True
    # is_active 切換(server_default True → 停用)
    await svc.set_asset_active("EID-001", False, actor=admin)
    assert (await svc.get_asset("EID-001")).is_active is False


async def test_set_asset_flags_requires_admin(session) -> None:
    """engineer 呼叫 → AuthorizationError(domain 強制);未變更。"""
    await load(ROWS, session)
    await _seed_admin(session, user_id="eng1", role="engineer")
    svc = AssetService(session)
    with pytest.raises(AuthorizationError):
        await svc.set_asset_active("EID-001", False, actor=Actor.human("eng1"))
    with pytest.raises(AuthorizationError):
        await svc.set_available_for_service("EID-001", False, actor=Actor.human("eng1"))
    assert (await svc.get_asset("EID-001")).is_active is True


async def test_set_asset_active_unknown(session) -> None:
    await _seed_admin(session)
    svc = AssetService(session)
    with pytest.raises(UnknownAssetError):
        await svc.set_asset_active("EID-NOPE", False, actor=Actor.human("admin1"))


# ---- 主檔編輯(admin-only;update_asset)----

def _edit_kwargs(**overrides):
    """update_asset 全欄預設(比照 EID-001 現值),測試以 overrides 覆蓋。"""
    base = dict(
        description="Cleaner #1", asset_type="Production", asset_subtype="AP CLEANER",
        department="EQ", line="Wet Loop", site="PLANT-1", model_no="M1", serial_no="S1",
        manufacturer=None, host_name=None, asset_ref=None, product=None, weblink=None,
        comments=None, process_segment_class=None,
    )
    base.update(overrides)
    return base


async def test_update_asset_admin_edits_fields(session) -> None:
    """admin 可改各欄(落庫 + 稽核欄);D6c 顯示名更正情境(comp_desc 修正)。"""
    await load(ROWS, session)
    admin = await _seed_admin(session)
    svc = AssetService(session)
    a = await svc.update_asset(
        "EID-001", actor=admin,
        **_edit_kwargs(
            description="Aligner46", asset_subtype="WIREBOND", manufacturer="Kestrel Systems",
            model_no="BX-820", host_name="host-01", asset_ref="CN-9", product="P1",
            weblink="http://x", comments="note", process_segment_class="SEG",
        ),
    )
    assert a.description == "Aligner46"
    assert a.asset_subtype == "WIREBOND"
    assert a.manufacturer == "Kestrel Systems"
    assert a.model_no == "BX-820"
    assert a.host_name == "host-01"
    assert a.asset_ref == "CN-9"
    assert a.product == "P1"
    assert a.weblink == "http://x"
    assert a.comments == "note"
    assert a.process_segment_class == "SEG"
    assert a.updated_by == "human:admin1"          # 稽核(ADR-005)
    assert a.source_actor == "human:admin1"
    # 空字串欄 → None(統一空值)
    a2 = await svc.update_asset("EID-001", actor=admin, **_edit_kwargs(model_no="", serial_no=""))
    assert a2.model_no is None and a2.serial_no is None


async def test_update_asset_requires_admin(session) -> None:
    await load(ROWS, session)
    await _seed_admin(session, user_id="eng1", role="engineer")
    svc = AssetService(session)
    with pytest.raises(AuthorizationError):
        await svc.update_asset("EID-001", actor=Actor.human("eng1"), **_edit_kwargs())
    assert (await svc.get_asset("EID-001")).description == "Cleaner #1"  # 未變


async def test_update_asset_unknown_asset(session) -> None:
    await load(ROWS, session)
    admin = await _seed_admin(session)
    svc = AssetService(session)
    with pytest.raises(UnknownAssetError):
        await svc.update_asset("EID-NOPE", actor=admin, **_edit_kwargs())


async def test_update_asset_empty_description_rejected(session) -> None:
    await load(ROWS, session)
    admin = await _seed_admin(session)
    svc = AssetService(session)
    with pytest.raises(AssetError):
        await svc.update_asset("EID-001", actor=admin, **_edit_kwargs(description="   "))


async def test_update_asset_unknown_lookup_rejected(session) -> None:
    """asset_type / department / line 給不存在的 lookup code → AssetError(不靜默鑄詞彙)。"""
    await load(ROWS, session)
    admin = await _seed_admin(session)
    svc = AssetService(session)
    with pytest.raises(AssetError):
        await svc.update_asset("EID-001", actor=admin, **_edit_kwargs(asset_type="Nope"))
    with pytest.raises(AssetError):
        await svc.update_asset("EID-001", actor=admin, **_edit_kwargs(department="ZZ"))
    with pytest.raises(AssetError):
        await svc.update_asset("EID-001", actor=admin, **_edit_kwargs(line="No Such Line"))
    # department / line 清空(None)合法
    a = await svc.update_asset("EID-001", actor=admin, **_edit_kwargs(department="", line=""))
    assert a.department is None and a.line is None


async def test_update_asset_noop_preserves_audit(session) -> None:
    """內容全未變 → no-op,不動稽核欄(updated_by 保持 loader 值)。"""
    await load(ROWS, session)
    admin = await _seed_admin(session)
    svc = AssetService(session)
    before = await svc.get_asset("EID-001")
    prior_updated_by = before.updated_by
    a = await svc.update_asset("EID-001", actor=admin, **_edit_kwargs())
    assert a.updated_by == prior_updated_by        # no-op:稽核未被 admin 蓋寫


# ---- 建新資產(admin-only;create_asset,內部規格)----


async def test_create_asset_admin_ok(session) -> None:
    """admin 建新 EID(全欄落庫 + 稽核 + 旗標 model 預設 True/True)。"""
    await load(ROWS, session)                       # 種 lookup(Production / EQ / Wet Loop)
    admin = await _seed_admin(session)
    svc = AssetService(session)
    a = await svc.create_asset(
        "EID-90001", actor=admin,
        description="P2 arm", asset_type="Production", asset_subtype="ROBOT",
        department="EQ", line="Wet Loop", site="PLANT-1", model_no="ARM-1",
        serial_no="SN-1", manufacturer="Norvex Robotics", host_name="h1", asset_ref="CN1",
        product="P2", weblink="http://a", comments="new arm",
        process_segment_class="SEG",
    )
    assert a.asset_id == "EID-90001"
    assert a.description == "P2 arm"
    assert a.asset_subtype == "ROBOT"
    assert a.department == "EQ" and a.line == "Wet Loop"
    assert a.model_no == "ARM-1" and a.serial_no == "SN-1"
    assert a.manufacturer == "Norvex Robotics" and a.host_name == "h1"
    assert a.asset_ref == "CN1" and a.product == "P2"
    assert a.weblink == "http://a" and a.comments == "new arm"
    assert a.process_segment_class == "SEG"
    # 稽核(ADR-005)
    assert a.created_by == "human:admin1"
    assert a.updated_by == "human:admin1"
    assert a.source_actor == "human:admin1"
    # 旗標 model 預設(未在 create 開放設定)
    assert a.is_active is True
    assert a.available_for_service is True
    # 確實落庫(fetch by PK)
    fetched = await svc.get_asset("EID-90001")
    assert fetched is not None and fetched.description == "P2 arm"


async def test_create_asset_normalizes_eid(session) -> None:
    """asset_id strip + upper:'  eid-12345  ' → 'EID-12345'。空文字欄 → None。"""
    await load(ROWS, session)
    admin = await _seed_admin(session)
    svc = AssetService(session)
    a = await svc.create_asset(
        "  eid-12345  ", actor=admin, description="lower eid",
        asset_type="Meter", site="PLANT-1", model_no="",
    )
    assert a.asset_id == "EID-12345"
    assert a.model_no is None                       # 空字串 → None
    assert await svc.get_asset("EID-12345") is not None


async def test_create_asset_duplicate_rejected(session) -> None:
    """既有 EID → AssetError(create 非 upsert,絕不靜默覆蓋)。"""
    await load(ROWS, session)
    admin = await _seed_admin(session)
    svc = AssetService(session)
    with pytest.raises(AssetError):
        await svc.create_asset(
            "EID-001", actor=admin, description="dup", asset_type="Production", site="PLANT-1",
        )
    # 原資料未被覆蓋
    assert (await svc.get_asset("EID-001")).description == "Cleaner #1"


async def test_create_asset_bad_format_rejected(session) -> None:
    """asset_id 不合 EID-xxxxx(5 位)→ AssetError。"""
    await load(ROWS, session)
    admin = await _seed_admin(session)
    svc = AssetService(session)
    for bad in ("003", "EID-3", "EID-123456", "FOO-12345", "EID-1234a"):
        with pytest.raises(AssetError):
            await svc.create_asset(
                bad, actor=admin, description="x", asset_type="Production", site="PLANT-1",
            )


async def test_create_asset_unknown_lookup_rejected(session) -> None:
    """asset_type / department / line 給不存在 lookup code → AssetError(不靜默鑄詞彙)。"""
    await load(ROWS, session)
    admin = await _seed_admin(session)
    svc = AssetService(session)
    with pytest.raises(AssetError):
        await svc.create_asset(
            "EID-90002", actor=admin, description="x", asset_type="Nope", site="PLANT-1",
        )
    with pytest.raises(AssetError):
        await svc.create_asset(
            "EID-90002", actor=admin, description="x", asset_type="Production",
            department="ZZ", site="PLANT-1",
        )
    with pytest.raises(AssetError):
        await svc.create_asset(
            "EID-90002", actor=admin, description="x", asset_type="Production",
            line="No Such Line", site="PLANT-1",
        )
    # 空 description / site 亦擋
    with pytest.raises(AssetError):
        await svc.create_asset(
            "EID-90002", actor=admin, description="  ", asset_type="Production", site="PLANT-1",
        )
    with pytest.raises(AssetError):
        await svc.create_asset(
            "EID-90002", actor=admin, description="x", asset_type="Production", site="  ",
        )
    assert await svc.get_asset("EID-90002") is None  # 全數未落庫


async def test_create_asset_requires_admin(session) -> None:
    """engineer 呼叫 → AuthorizationError(domain 強制);未落庫。"""
    await load(ROWS, session)
    await _seed_admin(session, user_id="eng1", role="engineer")
    svc = AssetService(session)
    with pytest.raises(AuthorizationError):
        await svc.create_asset(
            "EID-90003", actor=Actor.human("eng1"),
            description="x", asset_type="Production", site="PLANT-1",
        )
    assert await svc.get_asset("EID-90003") is None


# ---- 設備負責人 owners(0031 多負責人;create/update/set_owners/set_owner_bulk)----


async def test_create_asset_accepts_and_normalizes_owners(session) -> None:
    """create_asset 接受 owners 清單 + 正規化(strip、去空、去重保序)。"""
    await load(ROWS, session)
    admin = await _seed_admin(session)
    svc = AssetService(session)
    a = await svc.create_asset(
        "EID-90010", actor=admin, description="x", asset_type="Production",
        site="PLANT-1", owners=["  Alice Fang  ", "Ben Yeh", "  ", "Alice Fang"],
    )
    assert await svc.get_owners(a.asset_id) == ["Alice Fang", "Ben Yeh"]  # strip/去空/去重
    # 空清單 → 無負責人
    b = await svc.create_asset(
        "EID-90011", actor=admin, description="y", asset_type="Production",
        site="PLANT-1", owners=["   "],
    )
    assert await svc.get_owners(b.asset_id) == []


async def test_update_asset_sets_owners(session) -> None:
    """update_asset 可整組替換 / 清 owners;owners=None → 不動負責人。"""
    await load(ROWS, session)
    admin = await _seed_admin(session)
    svc = AssetService(session)
    await svc.update_asset("EID-001", actor=admin, **_edit_kwargs(owners=[" Ben Yeh ", "Mars"]))
    assert await svc.get_owners("EID-001") == ["Ben Yeh", "Mars"]
    # owners=None(預設)→ 不動
    await svc.update_asset("EID-001", actor=admin, **_edit_kwargs(description="Renamed"))
    assert await svc.get_owners("EID-001") == ["Ben Yeh", "Mars"]
    # 空清單 → 清除
    await svc.update_asset("EID-001", actor=admin, **_edit_kwargs(owners=[]))
    assert await svc.get_owners("EID-001") == []


async def test_set_owners_replace_and_idempotent(session) -> None:
    """set_owners 整組替換(admin-only);同清單再套 = no-op(不動稽核欄)。"""
    await load(ROWS, session)
    admin = await _seed_admin(session)
    admin2 = await _seed_admin(session, user_id="admin2")
    svc = AssetService(session)
    assert await svc.set_owners("EID-001", ["Ivy", "Bob"], admin) == ["Ivy", "Bob"]
    assert await svc.get_owners("EID-001") == ["Ivy", "Bob"]
    # 替換(移除 Bob、加 Cara、Ivy 保留)
    await svc.set_owners("EID-001", ["Ivy", "Cara"], admin2)
    assert await svc.get_owners("EID-001") == ["Ivy", "Cara"]
    # 清除
    await svc.set_owners("EID-001", [], admin)
    assert await svc.get_owners("EID-001") == []


async def test_set_owners_requires_admin(session) -> None:
    await load(ROWS, session)
    await _seed_admin(session, user_id="eng1", role="engineer")
    svc = AssetService(session)
    with pytest.raises(AuthorizationError):
        await svc.set_owners("EID-001", ["Ivy"], Actor.human("eng1"))
    assert await svc.get_owners("EID-001") == []


async def test_owners_map_batches(session) -> None:
    """owners_map 一查回多台的負責人清單(依 position)。"""
    await load(ROWS, session)
    admin = await _seed_admin(session)
    svc = AssetService(session)
    await svc.set_owners("EID-001", ["Ivy", "Bob"], admin)
    await svc.set_owners("EID-002", ["Mars"], admin)
    m = await svc.owners_map(["EID-001", "EID-002", "EID-003"])
    assert m == {"EID-001": ["Ivy", "Bob"], "EID-002": ["Mars"]}


# ---- 批次指定負責人(set_owner_bulk;admin-only,0031 REPLACE 多負責人)----


async def test_set_owner_bulk_happy(session) -> None:
    """3 台一次替換為同一組負責人:回 3、交叉表落庫。"""
    await load(ROWS, session)
    admin = await _seed_admin(session)
    svc = AssetService(session)
    n = await svc.set_owner_bulk(
        asset_ids=["EID-001", "eid-002", "  EID-003 "],
        owners=["  Alice Fang ", "Ben Yeh"], actor=admin,
    )
    assert n == 3
    for eid in ("EID-001", "EID-002", "EID-003"):
        assert await svc.get_owners(eid) == ["Alice Fang", "Ben Yeh"]  # strip/去重保序


async def test_set_owner_bulk_idempotent_skips_unchanged(session) -> None:
    """再跑一次(清單相同)→ 回 0(冪等,無變更)。"""
    await load(ROWS, session)
    admin = await _seed_admin(session)
    admin2 = await _seed_admin(session, user_id="admin2")
    svc = AssetService(session)
    assert await svc.set_owner_bulk(
        asset_ids=["EID-001", "EID-002"], owners=["Ivy"], actor=admin) == 2
    assert await svc.set_owner_bulk(
        asset_ids=["EID-001", "EID-002"], owners=["Ivy"], actor=admin2) == 0
    assert await svc.get_owners("EID-001") == ["Ivy"]


async def test_set_owner_bulk_clears_with_empty(session) -> None:
    """owners=[](或全空白)→ 清除選定資產的所有負責人。"""
    await load(ROWS, session)
    admin = await _seed_admin(session)
    svc = AssetService(session)
    await svc.set_owner_bulk(asset_ids=["EID-001"], owners=["Mars"], actor=admin)
    n = await svc.set_owner_bulk(asset_ids=["EID-001"], owners=[], actor=admin)
    assert n == 1
    assert await svc.get_owners("EID-001") == []
    # 全空白亦清除
    await svc.set_owner_bulk(asset_ids=["EID-002"], owners=["Mars"], actor=admin)
    assert await svc.set_owner_bulk(asset_ids=["EID-002"], owners=["   "], actor=admin) == 1
    assert await svc.get_owners("EID-002") == []


async def test_set_owner_bulk_unknown_all_or_nothing(session) -> None:
    """任一 EID 不在主檔 → UnknownAssetError,且無任何列被變更(all-or-nothing)。"""
    await load(ROWS, session)
    admin = await _seed_admin(session)
    svc = AssetService(session)
    with pytest.raises(UnknownAssetError):
        await svc.set_owner_bulk(
            asset_ids=["EID-001", "EID-NOPE"], owners=["Alice Fang"], actor=admin
        )
    assert await svc.get_owners("EID-001") == []   # 有效列未被變更


async def test_set_owner_bulk_requires_admin(session) -> None:
    """engineer 呼叫 → AuthorizationError(domain 強制);未變更。"""
    await load(ROWS, session)
    await _seed_admin(session, user_id="eng1", role="engineer")
    svc = AssetService(session)
    with pytest.raises(AuthorizationError):
        await svc.set_owner_bulk(
            asset_ids=["EID-001"], owners=["Alice Fang"], actor=Actor.human("eng1")
        )
    assert await svc.get_owners("EID-001") == []


async def test_set_owner_bulk_empty_selection_rejected(session) -> None:
    """空 asset_ids(或全空白)→ AssetError。"""
    await load(ROWS, session)
    admin = await _seed_admin(session)
    svc = AssetService(session)
    with pytest.raises(AssetError):
        await svc.set_owner_bulk(asset_ids=[], owners=["Alice Fang"], actor=admin)
    with pytest.raises(AssetError):
        await svc.set_owner_bulk(asset_ids=["  ", ""], owners=["Alice Fang"], actor=admin)
