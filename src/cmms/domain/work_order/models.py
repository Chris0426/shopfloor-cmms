"""WorkOrder ORM models(對應 docs/domain-model/02-work-orders.md §8)。

- lookup:`work_type`(8 類照原樣)、`wo_status`(O/H);`vendor` 沿用 pm_schedule 切片建的表。
- 反正規化欄(comp_desc/assetsubtp)依 §5.5 DROP;`miscreated` 不載入(W1 未定)。
- 時間軸:opened/closed 為可靠欄;work_start/complete_time 為 §5.2 不可靠 12h(面值載入)。
- 狀態機 / 兩階段寫入 / downtime 結算延到 #4b 寫入切片(本切片純讀取)。
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cmms.audit import AuditMixin
from cmms.db import Base


class _CodeLabel:
    """lookup 共用:code(PK)+ label(人/agent 可讀,ADR-007)。"""

    code: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String, nullable=False)


class WorkType(_CodeLabel, Base):
    __tablename__ = "work_type"  # REACTIVE/PM/ON HALT/PART REQUEST/...(照原樣,W4 重分類延後)


class WoStatus(_CodeLabel, Base):
    __tablename__ = "wo_status"  # #4b canonical 狀態機(OPEN/IN_PROGRESS/ON_HOLD/.../VOIDED)

    rank: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_terminal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )  # CLOSED/CANCELLED/VOIDED = 終態,不可再轉
    is_downtime: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )  # 此狀態機台是否停產(ON_HOLD 另由 hold_reason 覆寫,見 service)


class WoHoldReason(_CodeLabel, Base):
    """ON_HOLD 的暫停原因。`is_downtime` 決定該段是否計入 downtime。

    TEST_RUN(試跑,機台 up)=False;WAITING_PARTS/其他(機台無法生產)=True(Jordan 2026-06-22)。
    """

    __tablename__ = "wo_hold_reason"

    is_downtime: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class WorkOrder(AuditMixin, Base):
    __tablename__ = "work_order"

    # 識別與關聯
    work_order_no: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # wo(101–24172)
    # asset_id 高頻過濾(WO 集中:單台破千張)→ 索引;名 ix_work_order_asset_id 與 migration 對齊
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("asset.asset_id"), nullable=False, index=True
    )  # compid

    # 分類與狀態
    work_type: Mapped[str] = mapped_column(ForeignKey("work_type.code"), nullable=False)  # wo_type
    status: Mapped[str] = mapped_column(ForeignKey("wo_status.code"), nullable=False)  # workstatus

    # 工作內容(brief_description / diagnosis 已 html.unescape)
    brief_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnosis: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True)  # comments(MRQ-xxxx,W2)

    # 時間軸(date_wo 必填;其餘 nullable)
    opened_date: Mapped[date] = mapped_column(Date, nullable=False)  # date_wo
    scheduled_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # sch_date(僅 PM)
    # §5.2 不可靠 12h(無 AM/PM):面值解析載入,不可用於 downtime 計算
    work_start_time: Mapped[time | None] = mapped_column(Time, nullable=True)  # time
    work_complete_time: Mapped[time | None] = mapped_column(Time, nullable=True)  # time_cmpl
    closed_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # editdate(結案簽核日)
    closed_time: Mapped[time | None] = mapped_column(Time, nullable=True)  # edittime(24h,可靠)
    closed_by: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # edituser(FK→app_user 延後)

    # 人員指派(assignto 拆解;person FK→Contacts 切片)
    assigned_vendor: Mapped[str | None] = mapped_column(ForeignKey("vendor.code"), nullable=True)
    assigned_person: Mapped[str | None] = mapped_column(String, nullable=True)

    # [UI] 暫空(比照 Asset;待 UI 補抽 / #4b 寫入時填)
    priority: Mapped[str | None] = mapped_column(String, nullable=True)
    action_taken: Mapped[str | None] = mapped_column(Text, nullable=True)
    # D6 confirmed_reason(efc 軸,人工確認真因;僅 REACTIVE;null=未確認≠無故障)。FK 綁
    # equipment_failure_code.code(107 碼詞彙;永不鑄 canonical、永不觸 mfc 軸)。
    confirmed_reason_code: Mapped[str | None] = mapped_column(
        ForeignKey("equipment_failure_code.code"), nullable=True
    )
    downtime_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)  # close 時鎖定
    labor_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    opened_by: Mapped[str | None] = mapped_column(String, nullable=True)
    pm_source_id: Mapped[str | None] = mapped_column(String, nullable=True)  # →pm_schedule(FK 延後)

    # #4b 寫入切片:狀態機時間軸(系統自動抓,timestamptz)+ downtime 精算旗標 + 暫停原因
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    downtime_estimated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )  # 歷史=True(開單 time 不可靠);未來由 status_history 精算=False
    hold_reason: Mapped[str | None] = mapped_column(
        ForeignKey("wo_hold_reason.code"), nullable=True
    )  # 僅 status=ON_HOLD 時有值

    # #4b-2 on-box(ADR-017 Profile B):Analytics 簽章開立的 reactive 報修 WO
    origin_station: Mapped[str | None] = mapped_column(String, nullable=True)  # 站別歸屬(非 Person)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True
    )  # onbox:<station>:<EID>:<edge_ts>:<nonce>(verbatim,不 parse)
    evidence_ref: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )  # onbox-evidence:v1:onbox:<station>:<EID>:<edge_ts>:<nonce>


class PendingProposal(Base):
    """ADR-016 兩階段外部確認的待確認意圖。propose 建立、confirm 執行(經單一寫入路徑)。

    token ≠ 授權:授權來自 confirm 時攜帶的已驗證 `human:<id>`(拒匿名)。冪等用 idempotency_key
    (同 key 重複 propose 回既有 token)。逾時(expires_at)→ EXPIRED;confirm/reject 為終態。
    """

    __tablename__ = "pending_proposal"

    pending_token: Mapped[str] = mapped_column(String, primary_key=True)
    operation: Mapped[str] = mapped_column(
        String, nullable=False
    )  # open_work_order/close_work_order
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    dry_run_diff: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    proposed_by: Mapped[str] = mapped_column(String, nullable=False)  # agent:analytics
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'PENDING'")
    )  # PENDING / CONFIRMED / REJECTED / EXPIRED
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_by: Mapped[str | None] = mapped_column(String, nullable=True)  # human:<id>
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkOrderStatusHistory(AuditMixin, Base):
    """每次狀態轉移一筆(downtime 與稽核的真相來源,#4b)。

    `changed_at` 為轉移當下的牆鐘(系統自動抓;測試/回填可注入)— downtime 依連續兩筆
    `changed_at` 構成的區段累加(見 service `_recompute_downtime`)。
    """

    __tablename__ = "work_order_status_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    work_order_no: Mapped[int] = mapped_column(
        ForeignKey("work_order.work_order_no"), nullable=False, index=True
    )
    from_status: Mapped[str | None] = mapped_column(String, nullable=True)  # None = 開立
    to_status: Mapped[str] = mapped_column(ForeignKey("wo_status.code"), nullable=False)
    hold_reason: Mapped[str | None] = mapped_column(
        ForeignKey("wo_hold_reason.code"), nullable=True
    )  # 僅 to_status=ON_HOLD
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkOrderPart(AuditMixin, Base):
    """工單領用零件(每次領料一筆;#4b)。對應一筆 stock_transaction(ISSUE)。

    兩種填充路徑:① governed `issue_part_to_work_order`(連動扣 on_hand);② 歷史回填
    `backfill_part_issue`(part_issues.csv 4602 列,**不動 on_hand**;見 part_issue_backfill)。
    `(work_order_no, item_code)` 無 unique 約束:同對多列 = 多次獨立領料;防重靠配對的
    `stock_transaction.idempotency_key`(回填為 occurrence-based key)。
    """

    __tablename__ = "work_order_part"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    work_order_no: Mapped[int] = mapped_column(
        ForeignKey("work_order.work_order_no"), nullable=False, index=True
    )
    item_code: Mapped[str] = mapped_column(ForeignKey("inventory_item.item_code"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    # 軟刪(0022,護欄 #4:取消領料要留誰/何時;讀取面過濾 deleted_at IS NULL)。
    # ledger(stock_transaction)為 append-only 不刪,取消只補 RETURN 補償帳;本列軟刪讓
    # 領料清單/時間線消失。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String, nullable=True)


class WorkOrderAssignee(AuditMixin, Base):
    """工單負責人(多對一;0031)。難維護機台有多位負責人 → 報修開單自動指派全部、開立/結案
    皆通知全部。所有負責人平等(除 `position` 排序外無「主要」概念);`position` 決定回填相容
    單值欄(分析平台 `assigned_person`=首位)與顯示順序。`person_name` = legacy 確切字串(非 FK,
    比照 `work_order.assigned_person`)。複合 PK 防同單同人重複。
    """

    __tablename__ = "work_order_assignee"
    __table_args__ = (Index("ix_work_order_assignee_person", "person_name"),)

    work_order_no: Mapped[int] = mapped_column(
        ForeignKey("work_order.work_order_no"), primary_key=True
    )
    person_name: Mapped[str] = mapped_column(String, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class WoNoteType(_CodeLabel, Base):
    __tablename__ = "wo_note_type"  # progress/diagnosis/hold/resume/part/note/report/ai_candidate


class WorkOrderNote(AuditMixin, Base):
    """工單工作日誌(append-only,domain-model §1.6;ADR-020 決策 7)。

    長 down 工單分次、跨日更新;每筆 = 一次時間戳更新,**保留原貌**(`occurred_at` + `author`),
    不覆寫 `brief_description`。手動與 Hermes NL 輸入同走此表(`source_actor`/`author` 分人/agent)。
    照片掛 `attachment(owner_type='work_order_note', owner_id=str(id))` → 隨該筆的時間戳,
    UI 時間線與 Jira MRQ comment 皆按此筆呈現(照片 1:1 對到該 comment)。
    """

    __tablename__ = "work_order_note"
    # 與 migration 0012 對齊(alembic check 無漂移):時間線熱路徑複合索引 + 建 note 防重唯一索引。
    __table_args__ = (
        Index("ix_work_order_note_wo", "work_order_no", "occurred_at"),
        Index("uq_work_order_note_idem", "idempotency_key", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    work_order_no: Mapped[int] = mapped_column(
        ForeignKey("work_order.work_order_no"), nullable=False
    )
    entry_type: Mapped[str] = mapped_column(ForeignKey("wo_note_type.code"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)  # 保留原貌(html.unescape)
    author: Mapped[str] = mapped_column(String, nullable=False)  # human:<id> / agent:<name>
    # 該次更新時點(≠ created_at 落庫時間);時間線按此排序
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 若此筆隨狀態轉移產生,連 work_order_status_history(§1.6 交叉引用)
    status_history_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_order_status_history.id"), nullable=True
    )
    # agent/pipeline 建 note 防重(ADR-006);亦為 note↔jira comment 同步錨(ADR-020 決策 7)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 軟刪(0022,Jordan 2026-07-05 裁決:日誌記錯要能刪)。護欄 #4:留誰/何時;讀取面
    # (list_notes)過濾 deleted_at IS NULL,照片 attachment 不動(R2 永留),隨 note 消失。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String, nullable=True)


class WorkOrderExternalLink(AuditMixin, Base):
    """工單 ↔ 外部知識庫連結(ADR-020 決策 3;初版 Jira MRQ)。N:M。

    `external_key`=MRQ-xxxx(對外單號);`link_type`=referenced(legacy `external_ref` 回填)/
    forwarded(agent 新開 MRQ)/ appended(後續追加 comment)。冪等唯一鍵
    (work_order_no, system, external_key, link_type)。**cmms 端不呼叫 Jira**(決策 1);
    此表只記「連了什麼」,實際寫入由 gateway-side forwarder 做。dual attribution:
    `source_actor=agent:<name>`(誰轉發)+ `created_by=human:<id>`(代表誰,ADR-005)。
    """

    __tablename__ = "work_order_external_link"
    __table_args__ = (
        Index(
            "uq_wo_external_link",
            "work_order_no",
            "system",
            "external_key",
            "link_type",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    work_order_no: Mapped[int] = mapped_column(
        ForeignKey("work_order.work_order_no"), nullable=False, index=True
    )
    system: Mapped[str] = mapped_column(String, nullable=False)  # jira
    external_key: Mapped[str] = mapped_column(String, nullable=False)  # MRQ-xxxx
    link_type: Mapped[str] = mapped_column(String, nullable=False)  # referenced/forwarded/appended
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    # 軟移除(0020:打錯的 MRQ 連結要能更正;留誰/何時,讀取面過濾 removed_at IS NULL)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    # ADR-020 決策 1 修訂(0025):批次 forward 的防重錨。同一批工單以同 idempotency_key 重跑 →
    # 查得既有 forwarded link 即復用其 external_key、**不重開 MRQ issue**(Jira REST 無原生冪等)。
    # 只在 forward 建立的 forwarded link 上填;referenced/appended/手動 link 為 null。
    forward_idem_key: Mapped[str | None] = mapped_column(String(128), nullable=True)


class JiraOutbox(AuditMixin, Base):
    """工單 note → Jira MRQ comment 的同步 outbox(ADR-020 決策 1 修訂,2026-07-06)。

    連結建立後,該工單每新增一筆 work_order_note → 對其每個 forwarded/appended MRQ link 各排一列
    (enqueue,同交易);背景 flush 逐列用**連結建立者的 PAT**(`on_behalf_user`,ADR-022 vault)呼
    Jira REST 加 comment。**唯一鍵 (note_id, external_key)** = 冪等(同 note 對同 MRQ 只一 comment)。
    失敗誠實記錄(status=failed + last_error + attempts),CLI `jira-flush-outbox` 兜底重試。
    comment 內文 = note 原文忠實不翻譯(自動同步無 LLM);更正/軟刪不回寫(v1 只同步新增)。
    """

    __tablename__ = "jira_outbox"
    __table_args__ = (
        Index("uq_jira_outbox_note_key", "note_id", "external_key", unique=True),
        Index("ix_jira_outbox_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    note_id: Mapped[int] = mapped_column(
        ForeignKey("work_order_note.id"), nullable=False, index=True
    )
    work_order_no: Mapped[int] = mapped_column(
        ForeignKey("work_order.work_order_no"), nullable=False
    )
    external_key: Mapped[str] = mapped_column(String, nullable=False)  # MRQ-<n>
    # PAT 主人 = 連結建立者(bare user_id,如 jordan.lee);flush 以 Actor.human(此值) 取其 PAT
    on_behalf_user: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'pending'")
    )  # pending / sent / failed
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_comment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # 重試防重(0026):附件先上、旗標落定後才送 comment;重試時旗標=true 就跳過再上傳。
    attachments_uploaded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
