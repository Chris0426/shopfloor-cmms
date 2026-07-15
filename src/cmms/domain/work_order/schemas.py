"""WorkOrder 讀取 DTO(pydantic v2)。API / MCP 回傳這些,不直接吐 ORM 物件。

日期/時間以 date/time 呈現(MCP 端用 model_dump(mode="json") 轉 ISO 字串)。
#4b 加入狀態機時間軸(opened_at/closed_at)、downtime(精算/估算)、暫停原因,以及
狀態歷程(status_history)與領料(parts)讀取 DTO。
"""

from __future__ import annotations

from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict


class WorkOrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    work_order_no: int
    asset_id: str
    work_type: str
    status: str
    brief_description: str | None
    diagnosis: str | None
    external_ref: str | None
    opened_date: date
    scheduled_date: date | None
    work_start_time: time | None
    work_complete_time: time | None
    closed_date: date | None
    closed_time: time | None
    closed_by: str | None
    assigned_vendor: str | None
    assigned_person: str | None
    # 結單處置摘要——新單的最終結論欄;歷史匯入單此欄 null、其最終診斷在 legacy `diagnosis`
    action_taken: str | None
    # D6 人工確認故障真因(efc 碼;僅 REACTIVE 有值)。null=未確認≠無故障;歷史匯入單皆 null。
    confirmed_reason_code: str | None
    # #4b 狀態機 / downtime
    opened_at: datetime | None
    closed_at: datetime | None
    hold_reason: str | None
    downtime_minutes: int | None
    downtime_estimated: bool


class WorkOrderStatusHistoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_status: str | None
    to_status: str
    hold_reason: str | None
    changed_at: datetime
    source_actor: str | None
    # 內部規格 additive:該段機台是否停產(work_type-aware 計算欄,非 ORM 欄)。REACTIVE OPEN=True /
    # PM OPEN=False / ON_HOLD 依 hold_reason / 終態 False。schema 仍 v1(消費端勿白名單拒未知值)。
    # ★ 非 from_attributes 可填欄 → 建構點須顯式帶入(見 route helper);model_validate ORM 列會缺值。
    is_downtime: bool


class WorkOrderPartRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_code: str
    quantity: float


class WorkOrderNoteRead(BaseModel):
    """工作日誌一筆(分析平台 §19.6 消費;author 面僅 source_actor,同 status_history 慣例)。"""

    model_config = ConfigDict(from_attributes=True)

    occurred_at: datetime
    # wo_note_type code: report/progress/diagnosis/hold/resume/part/note/ai_candidate
    # (additive 詞彙,消費端勿白名單拒未知值)
    entry_type: str
    body: str
    source_actor: str | None


class WorkOrderDetail(WorkOrderRead):
    """工單 + 狀態歷程 + 領料明細 + 工作日誌。

    `notes` = 未軟刪、occurred_at 升冪(同刻以 id 次序);超過 cap 保留**最新** N 筆並升冪
    呈現、`notes_truncated`=true(cap 見 route 常數 `_DETAIL_NOTES_CAP`)。

    `assignees`(0031 additive)= 全部負責人(依 position;首位 == `assigned_person`)。
    schema_id 仍 v1(additive 欄,消費端勿白名單拒未知值);分析平台讀取契約 golden 原地補值。
    """

    status_history: list[WorkOrderStatusHistoryRead]
    parts: list[WorkOrderPartRead]
    notes: list[WorkOrderNoteRead]
    notes_truncated: bool
    assignees: list[str] = []


class WorkOrderActiveWindowRead(BaseModel):
    """A3 視窗查詢(Analytics 消費端需求)的列表項:精簡工單 + inline 全量狀態歷程。

    `status_history` 形狀 == `WorkOrderDetail` 同形(`WorkOrderStatusHistoryRead`,cmms
    canonical `{from_status, to_status, hold_reason, changed_at, source_actor}`;W8/下游交付
    已裁定 分析平台適配 cmms 形)。datetime 序列化走與既有讀 API 同一條 pydantic response_model
    路徑(UTC → `Z`,見 golden fixture contract_wo_detail.v1.json)。
    """

    model_config = ConfigDict(from_attributes=True)

    work_order_no: int
    asset_id: str
    work_type: str
    opened_at: datetime | None
    closed_at: datetime | None
    status: str
    hold_reason: str | None
    status_history: list[WorkOrderStatusHistoryRead]


class WorkOrderActiveInResponse(BaseModel):
    """A3 端點回應信封:活躍於窗內的工單清單 + 是否被 v1 上限截斷。"""

    items: list[WorkOrderActiveWindowRead]
    truncated: bool


class HoldReasonRead(BaseModel):
    """hold_reason 受控詞彙一筆(自然鍵 code + 機器可讀 is_downtime)。

    `is_downtime` = 該 ON_HOLD 區段機台是否停產,Analytics 算 true downtime 時據此判斷
    哪些暫停段計入(等料/等承包商=停產=True;試跑/等機台空檔=機台運轉中=False)。
    """

    model_config = ConfigDict(from_attributes=True)

    code: str
    label: str
    is_downtime: bool


class HoldReasonVocabResponse(BaseModel):
    """hold_reason 唯讀 lookup 回應信封(單一扁平 list —— 非兩軸,無「永不合併」鐵則)。

    schema_id = **contract_hold_reason_vocab.v1**(Analytics 對版用;golden fixture
    `tests/fixtures/contract_hold_reason_vocab.v1.json`)。`WoHoldReason` 無退役旗標
    (無 is_active 欄)→ 全量曝出,不虛構欄位。
    """

    hold_reasons: list[HoldReasonRead]
