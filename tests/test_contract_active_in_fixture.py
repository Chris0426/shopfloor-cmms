"""A3 視窗查詢契約的 golden fixture 測試(EXECUTION-PLAYBOOK §4-1;免 Docker,永遠跑)。

`tests/fixtures/contract_wo_active_in.v1.json` 是 **Analytics 消費契約的金樣本** ——
`GET /work-orders/active-in` 回應信封(`WorkOrderActiveInResponse`)的真實序列化。
schema_id = **contract_wo_active_in.v1**(分析平台對版用)。以固定資料建構 pydantic 物件 →
`model_dump(mode="json")` → 與 fixture 整體相等。契約形狀一變(欄位增 / 刪 / 改名 / 改型)
→ 本測試**紅** = 機械觸發對 Analytics 的 push。

★★ 契約硬化制度(硬性義務,同 contract_wo_detail):
- 本 fixture 變更**必須**先過 下游 consumer 登記表 Gate A + 產 push note(Analytics §19)。
- 形狀有破壞性變更時,**新檔名 bump**(contract_wo_active_in.**v2**.json),舊版保留,不可原地覆寫。

時戳規約(分析平台 2026-07-05 對版問答釘死):所有 datetime = **UTC ISO-8601 帶 `Z`**(與 detail
契約同一條 pydantic response_model 路徑);樣本第二項含 still-open 遷移單代表值
(closed_at=null、status_history=[] —— 分析平台 fallback 分支的 wire 形)。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from cmms.domain.work_order.schemas import (
    WorkOrderActiveInResponse,
    WorkOrderActiveWindowRead,
    WorkOrderStatusHistoryRead,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "contract_wo_active_in.v1.json"


def _sample_response() -> WorkOrderActiveInResponse:
    """形狀覆蓋樣本:①全欄非 null + 完整 history(canonical 形)②still-open 遷移單
    (closed_at/hold_reason=null、history 空 —— 鎖住 分析平台 fallback 分支的 wire 形)。"""
    return WorkOrderActiveInResponse(
        items=[
            WorkOrderActiveWindowRead(
                work_order_no=30318,
                asset_id="EID-70021",
                work_type="REACTIVE",
                opened_at=datetime(2026, 7, 1, 9, 12, 0, tzinfo=UTC),
                closed_at=datetime(2026, 7, 2, 16, 50, 0, tzinfo=UTC),
                status="CLOSED",
                hold_reason="WAITING_MACHINE_TIME",
                status_history=[
                    WorkOrderStatusHistoryRead(
                        from_status=None,
                        to_status="OPEN",
                        hold_reason=None,
                        changed_at=datetime(2026, 7, 1, 9, 12, 0, tzinfo=UTC),
                        source_actor="human:jlee",
                        is_downtime=True,  # REACTIVE OPEN 段 → 停產(內部規格)
                    ),
                    WorkOrderStatusHistoryRead(
                        from_status="IN_PROGRESS",
                        to_status="ON_HOLD",
                        hold_reason="WAITING_MACHINE_TIME",
                        changed_at=datetime(2026, 7, 1, 14, 30, 0, tzinfo=UTC),
                        source_actor="human:alice.fang",
                        is_downtime=False,  # 等機台空檔=機台運轉中 → 不計
                    ),
                    WorkOrderStatusHistoryRead(
                        from_status="ON_HOLD",
                        to_status="CLOSED",
                        hold_reason=None,
                        changed_at=datetime(2026, 7, 2, 16, 50, 0, tzinfo=UTC),
                        source_actor="human:alice.fang",
                        is_downtime=False,  # 終態 → 不計
                    ),
                ],
            ),
            WorkOrderActiveWindowRead(
                work_order_no=24401,
                asset_id="EID-70004",
                work_type="PM",
                opened_at=datetime(2026, 7, 3, 1, 0, 0, tzinfo=UTC),
                closed_at=None,
                status="OPEN",
                hold_reason=None,
                status_history=[],
            ),
        ],
        truncated=False,
    )


def test_active_in_matches_golden_fixture() -> None:
    """WorkOrderActiveInResponse 序列化 == 金樣本(形狀契約)。不等 → 形狀漂移,見本檔頂部制度。"""
    serialized = _sample_response().model_dump(mode="json")
    expected = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert serialized == expected
