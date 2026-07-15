"""C2 失效詞彙讀 API `GET /vocab/failure` 的路由層 DB 整合測試(testcontainers)。

本機無 Docker 時自動 skip。驗證:兩軸分列(永不合併)、欄位形狀、排序 deterministic、
退役列仍曝出、**不含內部欄**(id / notes / source_adapter / audit)。

★ DB fixture 用 NullPool + monkeypatch 全域 sessionmaker(比照 test_mcp_http):
TestClient 的 portal thread loop 與 asyncio.run(seed)的短命 loop 交錯用同一 engine,
pool 化連線會綁死舊 loop。read API auth 在 local(預設 app_env)+ 未設 token → 放行。
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("testcontainers.postgres")
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.api.app import app  # noqa: E402
from cmms.audit import Actor  # noqa: E402
from cmms.config import get_settings  # noqa: E402
from cmms.db import Base  # noqa: E402

# hold-reason 路由測試需 wo_hold_reason 表 + 其 FK 依賴鏈全註冊(比照 test_asset_composition_db)
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.attachment import models as _attachment_models  # noqa: E402, F401
from cmms.domain.contacts import models as _contacts_models  # noqa: E402, F401
from cmms.domain.failure_vocab import models as _fv_models  # noqa: E402, F401
from cmms.domain.failure_vocab.service import FailureVocabService  # noqa: E402
from cmms.domain.failure_vocab.transform import (  # noqa: E402
    EquipmentFailureCodeImport,
    MesFailmodeImport,
)
from cmms.domain.inventory import models as _inventory_models  # noqa: E402, F401
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401
from cmms.domain.work_order.service import WorkOrderService  # noqa: E402

ACTOR = Actor.human("tester")

# 種子:mfc 兩站(排序驗證 + triage null 分支 + 退役列)、efc 兩碼(station_hint null 分支)
_MFC = [
    MesFailmodeImport(
        station="sta3", label="SensorFail", signal_id="mes.failmode.sensorfail",
        entry_kind="fail_flag", seg_class="sta3PanelSeg", mes_variable="mfcSensorFail",
        material_class="assyModule", semantic_zh="感測器失效",
        dominant_in_chronic="n", source_adapter="oet_adapter.py", notes="internal note",
    ),
    MesFailmodeImport(
        station="sta1", label="triage_A", signal_id=None, entry_kind="triage_category",
        seg_class=None, mes_variable=None, material_class="assyModule",
        semantic_zh="三分流大類 A", dominant_in_chronic="n",
        source_adapter="cpt_adapter.py", notes=None,
    ),
]
_EFC = [
    EquipmentFailureCodeImport(
        code="efcSTA7_AirPressure", descr="STA7 air pressure out of range",
        station_hint="sta7", recency_status="source_alive_2026-07",
    ),
    EquipmentFailureCodeImport(
        code="efcSA1_TcpComms", descr="SA1 PLC TCP comms fault",
        station_hint=None, recency_status="source_alive_2026-07",
    ),
]


@pytest.fixture
def client():
    mp = pytest.MonkeyPatch()
    get_settings.cache_clear()
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url, poolclass=NullPool)
        sm = async_sessionmaker(engine, expire_on_commit=False)

        async def _setup() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with sm() as s:
                svc = FailureVocabService(s)
                async with svc.write(ACTOR):
                    for m in _MFC:
                        await svc.upsert_mes_failmode(m, ACTOR)
                    for e in _EFC:
                        await svc.upsert_equipment_failure_code(e, ACTOR)

        asyncio.run(_setup())
        # deps.py 以 `from cmms.db import get_sessionmaker` 綁入自身命名空間 → patch 該引用
        mp.setattr("cmms.api.deps.get_sessionmaker", lambda: sm)
        try:
            yield TestClient(app, raise_server_exceptions=False)
        finally:
            mp.undo()
            asyncio.run(engine.dispose())
            get_settings.cache_clear()


def test_get_failure_vocab_two_axes_split(client) -> None:
    r = client.get("/vocab/failure")
    assert r.status_code == 200
    body = r.json()
    # 兩軸分列(永不合併):回應信封兩個 key,各自 list
    assert set(body) == {"mes_failmodes", "equipment_failure_codes"}
    assert len(body["mes_failmodes"]) == 2
    assert len(body["equipment_failure_codes"]) == 2


def test_mfc_shape_and_sort(client) -> None:
    mfcs = client.get("/vocab/failure").json()["mes_failmodes"]
    # deterministic 排序:(station, label) → sta1/triage_A 在 sta3/SensorFail 之前
    assert [(m["station"], m["label"]) for m in mfcs] == [
        ("sta1", "triage_A"),
        ("sta3", "SensorFail"),
    ]
    triage = mfcs[0]
    # 欄位形狀:曝出的鍵完全 == schema(不含內部欄)
    assert set(triage) == {
        "station", "label", "signal_id", "entry_kind", "seg_class", "mes_variable",
        "material_class", "semantic_zh", "dominant_in_chronic", "is_active",
    }
    # 內部欄不外洩
    assert "notes" not in triage and "source_adapter" not in triage
    assert "id" not in triage and "created_by" not in triage
    # triage_category:signal_id/seg_class/mes_variable = null
    assert triage["signal_id"] is None and triage["seg_class"] is None
    assert triage["entry_kind"] == "triage_category"
    assert triage["is_active"] is True


def test_efc_shape_and_sort(client) -> None:
    efcs = client.get("/vocab/failure").json()["equipment_failure_codes"]
    # 依 code 排序:efcSA1_* < efcSTA7_*
    assert [e["code"] for e in efcs] == ["efcSA1_TcpComms", "efcSTA7_AirPressure"]
    e = efcs[1]
    assert set(e) == {"code", "descr", "station_hint", "recency_status", "is_active"}
    assert "id" not in e and "notes" not in e
    # station_hint null 分支(種子 TODO → None)保真曝出
    assert efcs[0]["station_hint"] is None


# ---- hold_reason 唯讀 lookup `GET /vocab/hold-reason`
# (分析平台消費 contract_hold_reason_vocab.v1)----

_HOLD_REASONS = [
    ("WAITING_PARTS", "Waiting for Parts(等待零件)", True),  # 停產=計 downtime
    ("TEST_RUN", "Test Run(試跑,機台運轉中)", False),  # 機台運轉=不計
    ("OTHER", "Other(其他,機台停產)", True),
]


@pytest.fixture
def hold_client():
    mp = pytest.MonkeyPatch()
    get_settings.cache_clear()
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url, poolclass=NullPool)
        sm = async_sessionmaker(engine, expire_on_commit=False)

        async def _setup() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with sm() as s:
                svc = WorkOrderService(s)
                async with svc.write(ACTOR):
                    for code, label, is_downtime in _HOLD_REASONS:
                        await svc.upsert_hold_reason(code, label, is_downtime=is_downtime)

        asyncio.run(_setup())
        mp.setattr("cmms.api.deps.get_sessionmaker", lambda: sm)
        try:
            yield TestClient(app, raise_server_exceptions=False)
        finally:
            mp.undo()
            asyncio.run(engine.dispose())
            get_settings.cache_clear()


def test_get_hold_reason_vocab_flat_list(hold_client) -> None:
    r = hold_client.get("/vocab/hold-reason")
    assert r.status_code == 200
    body = r.json()
    # 單一扁平 list(非兩軸)
    assert set(body) == {"hold_reasons"}
    assert len(body["hold_reasons"]) == 3


def test_hold_reason_shape_and_sort(hold_client) -> None:
    reasons = hold_client.get("/vocab/hold-reason").json()["hold_reasons"]
    # deterministic 排序(list_hold_reasons:WAITING_* 優先、其餘依 code)
    assert [r["code"] for r in reasons] == ["WAITING_PARTS", "OTHER", "TEST_RUN"]
    r = reasons[0]
    # 欄位形狀:曝出的鍵完全 == schema(WoHoldReason 無 is_active,不虛構)
    assert set(r) == {"code", "label", "is_downtime"}
    assert "id" not in r and "is_active" not in r
    # is_downtime 機器可讀(分析平台算 true downtime 用)
    assert r["is_downtime"] is True
    assert reasons[2]["is_downtime"] is False  # TEST_RUN 機台運轉,不計 downtime
