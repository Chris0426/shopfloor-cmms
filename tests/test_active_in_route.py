"""A3 端點 `/work-orders/active-in` 的路由層測試(免 DB,永遠跑):bearer 保護 + 422 壞格式。

行為(相交語意 / fallback / truncated)在 test_active_in_db.py(DB 整合)覆蓋;此處只驗
① 非豁免路徑要求 static bearer(峰會 消費端需求)② start/end 壞格式 → 422(在 service 呼叫前擋下)。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cmms.api.app import app
from cmms.api.deps import get_session
from cmms.config import get_settings

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _isolate_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _fake_session():
    """override get_session:422 在 service 呼叫前擋下,session 不被使用(免真 DB)。"""
    async def _s():
        yield None

    app.dependency_overrides[get_session] = _s
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_session, None)


def test_active_in_requires_bearer(monkeypatch):
    """production + 已設 read token + 無 Authorization → 401(不在豁免清單)。"""
    monkeypatch.setenv("CMMS_READ_API_TOKEN", "tok")
    monkeypatch.setenv("CMMS_APP_ENV", "production")
    get_settings.cache_clear()
    r = client.get(
        "/work-orders/active-in",
        params={"start": "2026-07-01 00:00:00", "end": "2026-07-01 01:00:00"},
    )
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"


def test_active_in_bad_format_422(_fake_session, monkeypatch):
    """start 壞格式 → 422(local:read bearer 未設 → 放行,進路由後解析失敗)。"""
    monkeypatch.delenv("CMMS_READ_API_TOKEN", raising=False)
    monkeypatch.setenv("CMMS_APP_ENV", "local")
    get_settings.cache_clear()
    r = client.get(
        "/work-orders/active-in",
        params={"start": "nope", "end": "2026-07-01 01:00:00"},
    )
    assert r.status_code == 422


def test_active_in_missing_param_422(_fake_session, monkeypatch):
    """缺必填 end → FastAPI 422(Query(...))。"""
    monkeypatch.delenv("CMMS_READ_API_TOKEN", raising=False)
    monkeypatch.setenv("CMMS_APP_ENV", "local")
    get_settings.cache_clear()
    r = client.get("/work-orders/active-in", params={"start": "2026-07-01 00:00:00"})
    assert r.status_code == 422


def test_active_in_envelope_and_z_serialization(_fake_session, monkeypatch):
    """回應信封 {items, truncated} + status_history 同 detail 形 + datetime UTC→`Z`
    (與 golden fixture 同序列化路徑;不另寫序列化)。以 canned service 回傳驗路由層。"""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from cmms.domain.work_order.service import WorkOrderService

    monkeypatch.delenv("CMMS_READ_API_TOKEN", raising=False)
    monkeypatch.setenv("CMMS_APP_ENV", "local")
    get_settings.cache_clear()

    wo = SimpleNamespace(
        work_order_no=30318, asset_id="EID-70021", work_type="REACTIVE",
        opened_at=datetime(2026, 7, 1, 9, 12, 0, tzinfo=UTC),
        closed_at=datetime(2026, 7, 2, 16, 50, 0, tzinfo=UTC),
        status="CLOSED", hold_reason="WAITING_MACHINE_TIME",
    )
    hist = [
        SimpleNamespace(from_status=None, to_status="OPEN", hold_reason=None,
                        changed_at=datetime(2026, 7, 1, 9, 12, 0, tzinfo=UTC),
                        source_actor="human:jlee"),
        SimpleNamespace(from_status="OPEN", to_status="COMPLETED", hold_reason=None,
                        changed_at=datetime(2026, 7, 2, 16, 45, 0, tzinfo=UTC),
                        source_actor="human:alice.fang"),
    ]

    async def _fake(self, *, start, end, asset_id=None, cap=2000):
        return [(wo, hist)], True

    async def _fake_maps(self):
        # 內部規格:canned lookup maps(免 DB;值域比照 loader 種子)
        return (
            {"OPEN": True, "IN_PROGRESS": True, "ON_HOLD": True, "COMPLETED": False,
             "CLOSED": False, "CANCELLED": False, "VOIDED": False},
            {"WAITING_MACHINE_TIME": False, "WAITING_PARTS": True},
        )

    monkeypatch.setattr(WorkOrderService, "list_active_in_window", _fake)
    monkeypatch.setattr(WorkOrderService, "downtime_lookup_maps", _fake_maps)
    r = client.get(
        "/work-orders/active-in",
        params={"start": "2026-07-01 00:00:00", "end": "2026-07-03 00:00:00"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["truncated"] is True
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["work_order_no"] == 30318 and item["hold_reason"] == "WAITING_MACHINE_TIME"
    # datetime UTC → `Z`(與既有讀 API / golden fixture 同法)
    assert item["opened_at"] == "2026-07-01T09:12:00Z"
    # status_history 同 detail 形(cmms canonical 欄位名 + 內部規格 additive is_downtime 計算欄)
    sh = item["status_history"]
    assert sh[0]["to_status"] == "OPEN" and sh[0]["changed_at"] == "2026-07-01T09:12:00Z"
    assert set(sh[0]) == {
        "from_status", "to_status", "hold_reason", "changed_at", "source_actor", "is_downtime",
    }
    # 內部規格 判定:REACTIVE OPEN 段 → 停產;COMPLETED 段 → 不計
    assert sh[0]["is_downtime"] is True and sh[1]["is_downtime"] is False
