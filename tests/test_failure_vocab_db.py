"""failure_vocab(C2)切片的 DB 整合測試(testcontainers-postgres)。

本機無 Docker 時自動 skip。驗證:兩軸種子載入計數、冪等重跑(不重複、計數穩定)、
upsert 更新內容欄(semantic_zh 改變)、is_active 預設 true 且**loader 重跑不復活**
(模擬 admin 未來退役 = 直接 flip false)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy import select, update  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.db import Base  # noqa: E402
from cmms.domain.failure_vocab import models as _fv_models  # noqa: E402, F401
from cmms.domain.failure_vocab.loader import (  # noqa: E402
    load_efc_codes,
    load_mes_failmodes,
    read_efc_rows,
    read_mes_failmode_rows,
)
from cmms.domain.failure_vocab.models import EquipmentFailureCode, MesFailmode  # noqa: E402
from cmms.domain.failure_vocab.service import FailureVocabService  # noqa: E402

_DATA = Path(__file__).resolve().parents[1] / "data" / "raw"
_MFC_CSV = _DATA / "mes_failmode_seed.csv"
_EFC_CSV = _DATA / "efc_equipment_codes.csv"

# 本模組每個測試都要餵真實種子 CSV;公開 repo 不夾帶廠內資料 → 整模組 skip。
pytestmark = pytest.mark.skipif(
    not (_MFC_CSV.exists() and _EFC_CSV.exists()),
    reason="real plant data is not shipped in the public repo",
)


@pytest.fixture
async def session():
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            yield s
        await engine.dispose()


async def _load_both(session):
    mfc = await load_mes_failmodes(read_mes_failmode_rows(_MFC_CSV), session)
    efc = await load_efc_codes(read_efc_rows(_EFC_CSV), session)
    return mfc, efc


async def test_load_counts(session) -> None:
    mfc, efc = await _load_both(session)
    assert mfc.read == 126 and mfc.loaded == 116
    assert mfc.fail_flags == 113 and mfc.triage_categories == 3
    assert mfc.skipped_doc_rows == 10
    assert efc.read == 107 and efc.loaded == 107

    svc = FailureVocabService(session)
    assert len(await svc.list_mes_failmodes()) == 116
    assert len(await svc.list_equipment_failure_codes()) == 107
    # station 過濾:sta4 站在 v2 補很多列
    sta4 = await svc.list_mes_failmodes(station="sta4")
    assert len(sta4) >= 20 and all(m.station == "sta4" for m in sta4)
    # efc station_hint 過濾(sta7 家族)
    sta7 = await svc.list_equipment_failure_codes(station_hint="sta7")
    assert len(sta7) == 9 and all(e.station_hint == "sta7" for e in sta7)


async def test_axes_never_merged_and_defaults(session) -> None:
    await _load_both(session)
    svc = FailureVocabService(session)
    # 兩軸是兩張表(不同計數、不同鍵);is_active 預設 true
    mfcs = await svc.list_mes_failmodes()
    efcs = await svc.list_equipment_failure_codes()
    assert all(m.is_active is True for m in mfcs)
    assert all(e.is_active is True for e in efcs)
    # 跨站碰撞:三個 SensorFail 各留(prober/sta3/sta4)
    cmos = [m for m in mfcs if m.label == "SensorFail"]
    assert {m.station for m in cmos} == {"prober", "sta3", "sta4"}
    # TODO station_hint → None(4 SA 家族 12 列)
    none_hint = [e for e in efcs if e.station_hint is None]
    assert len(none_hint) == 12


async def test_load_is_idempotent(session) -> None:
    await _load_both(session)
    mfc2, efc2 = await _load_both(session)
    assert mfc2.loaded == 116 and efc2.loaded == 107
    svc = FailureVocabService(session)
    assert len(await svc.list_mes_failmodes()) == 116  # 無重複
    assert len(await svc.list_equipment_failure_codes()) == 107


async def test_upsert_updates_content_on_change(session) -> None:
    await _load_both(session)
    # 直接改一筆的 semantic_zh(模擬種子內容更新),再跑 loader → upsert 覆蓋回種子值
    async with session.begin():
        await session.execute(
            update(MesFailmode)
            .where(MesFailmode.station == "sta1", MesFailmode.label == "CycleStop")
            .values(semantic_zh="STALE")
        )
    await load_mes_failmodes(read_mes_failmode_rows(_MFC_CSV), session)
    row = (
        await session.scalars(
            select(MesFailmode).where(
                MesFailmode.station == "sta1", MesFailmode.label == "CycleStop"
            )
        )
    ).one()
    assert row.semantic_zh == "循環停止（機台停線訊號）"  # 內容欄被 upsert 覆回


async def test_is_active_survives_loader_rerun_after_retirement(session) -> None:
    """additive-only:退役(is_active=false)後 loader 重跑**不得復活**。"""
    await _load_both(session)
    # 模擬未來 admin 退役一筆 mfc + 一筆 efc(直接 flip false)
    async with session.begin():
        await session.execute(
            update(MesFailmode)
            .where(MesFailmode.station == "sta1", MesFailmode.label == "CycleStop")
            .values(is_active=False)
        )
        await session.execute(
            update(EquipmentFailureCode)
            .where(EquipmentFailureCode.code == "efcSTA7_AirPressure")
            .values(is_active=False)
        )
    # loader 重跑
    await _load_both(session)
    mfc = (
        await session.scalars(
            select(MesFailmode).where(
                MesFailmode.station == "sta1", MesFailmode.label == "CycleStop"
            )
        )
    ).one()
    efc = (
        await session.scalars(
            select(EquipmentFailureCode).where(
                EquipmentFailureCode.code == "efcSTA7_AirPressure"
            )
        )
    ).one()
    assert mfc.is_active is False  # loader 未復活
    assert efc.is_active is False
