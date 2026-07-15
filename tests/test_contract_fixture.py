"""WorkOrderDetail 讀取契約的 golden fixture 測試(EXECUTION-PLAYBOOK §4-1;免 Docker,永遠跑)。

`tests/fixtures/contract_wo_detail.v1.json` 是 **Analytics 消費契約的金樣本** —— WorkOrderDetail
(work-orders 讀取 API / MCP 回傳形狀)的真實序列化。以固定資料建構 pydantic 物件 →
`model_dump(mode="json")` → 與 fixture 整體相等。契約形狀一變(欄位增 / 刪 / 改名 / 改型)→ 本測試
**紅** = 機械觸發對 Analytics 的 push。

★★ 契約硬化制度(硬性義務):
- 本 fixture 變更**必須**先過 下游 consumer 登記表 Gate A + 產 push note(Analytics §19)。
- 形狀有破壞性變更時,**新檔名 bump**(contract_wo_detail.**v2**.json),舊版保留以利下游對版,
  不可原地覆寫 v1。單純補一筆代表值(不改形狀)可原地更新。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from pathlib import Path

from cmms.domain.work_order.schemas import (
    WorkOrderDetail,
    WorkOrderNoteRead,
    WorkOrderPartRead,
    WorkOrderStatusHistoryRead,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "contract_wo_detail.v1.json"


def _sample_detail() -> WorkOrderDetail:
    """涵蓋每個欄位的代表值(可選欄位一律給非 null,以鎖住完整形狀)。

    注意:此樣本以**形狀覆蓋**為目的,非語意上一致的 live 工單
    (例如同時帶 closed_* 與 hold_reason,只為讓每個欄位都有非 null 代表值)。
    時間戳用固定 **tz-aware UTC** 值:live 端點的 opened_at/closed_at/changed_at 皆為
    DB `timestamptz` → asyncpg 回 tz-aware → 序列化帶 `Z` 後綴。fixture 必須忠實此
    wire 格式(naive 樣本會把「無時區後綴」的錯誤契約傳染給下游 consumer)。
    """
    return WorkOrderDetail(
        work_order_no=30318,
        asset_id="EID-70021",
        work_type="REACTIVE",
        status="CLOSED",
        brief_description="吸嘴堵塞、取料失敗連續報警",
        diagnosis="真空泵老化,真空度不足導致取料失敗。",
        external_ref="MRQ-4821",
        opened_date=date(2026, 7, 1),
        scheduled_date=date(2026, 7, 2),
        work_start_time=time(9, 12, 0),
        work_complete_time=time(16, 45, 0),
        closed_date=date(2026, 7, 2),
        closed_time=time(16, 50, 0),
        closed_by="Alice Fang",
        assigned_vendor="CMB",
        assigned_person="Alice Fang",
        action_taken="更換真空泵,真空度回復正常。",
        confirmed_reason_code="efcPickupVacuumFault",  # D6 人工確認真因(efc 軸;僅 REACTIVE)
        opened_at=datetime(2026, 7, 1, 9, 12, 0, tzinfo=UTC),
        closed_at=datetime(2026, 7, 2, 16, 50, 0, tzinfo=UTC),
        hold_reason="WAITING_MACHINE_TIME",  # 0019 受控值(等機台空檔)
        downtime_minutes=452,
        downtime_estimated=False,
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
                hold_reason="WAITING_MACHINE_TIME",  # 非 null hold_reason(≥1 筆)
                changed_at=datetime(2026, 7, 1, 14, 30, 0, tzinfo=UTC),
                source_actor="human:alice.fang",
                is_downtime=False,  # 等機台空檔=機台運轉中 → 不計(hold_reason lookup)
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
        parts=[
            WorkOrderPartRead(item_code="ES000804", quantity=1.0),
            WorkOrderPartRead(item_code="ES000805", quantity=2.0),
        ],
        # notes 升冪(occurred_at,同刻以 id);source_actor 帶 `Z` 後綴同 status_history 路徑
        notes=[
            WorkOrderNoteRead(
                occurred_at=datetime(2026, 7, 1, 9, 15, 0, tzinfo=UTC),
                entry_type="report",
                body="吸嘴堵塞、取料失敗連續報警",
                source_actor="human:jlee",
            ),
            WorkOrderNoteRead(
                occurred_at=datetime(2026, 7, 1, 11, 40, 0, tzinfo=UTC),
                entry_type="diagnosis",
                body="真空度不足,疑真空泵老化。",
                source_actor="human:alice.fang",
            ),
            WorkOrderNoteRead(
                occurred_at=datetime(2026, 7, 2, 16, 45, 0, tzinfo=UTC),
                entry_type="progress",
                body="更換真空泵完成,試機 OK。",
                source_actor="human:alice.fang",
            ),
        ],
        notes_truncated=False,
        assignees=["Alice Fang"],  # 0031 additive:全部負責人(首位 == assigned_person)
    )


def test_wo_detail_matches_golden_fixture() -> None:
    """WorkOrderDetail 序列化 == 金樣本(形狀契約)。不等 → 形狀漂移,見本檔頂部制度。"""
    serialized = _sample_detail().model_dump(mode="json")
    expected = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert serialized == expected
