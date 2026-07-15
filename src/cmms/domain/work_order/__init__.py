"""WorkOrder 切片(#4)— 工單(歷史紀錄讀取本體)。

結構最複雜的實體、cmms-mes-pipeline 的主要寫入端。本切片**純讀取(#4a)**:
migration + domain service + 讀取 API/MCP + 載入器(21,799 筆歷史工單)+ tests。

範圍與決議(2026-06-21):
- **寫入機制全延後到 #4b**:現代狀態機(DRAFT/IN_PROGRESS/…)、open/close/void
  governed ops、ADR-016 兩階段 propose/confirm + pending_proposal、idempotency、
  MES up/down 耦合、W4 work_type 重分類。
- **`miscreated` 不載入**(Jordan,W1 語意未定 → 不假設、不搬;日後確認再 backfill)。
- **時間欄轉新格式**(Jordan):`time`/`time_cmpl`→ `work_start_time`/`work_complete_time`
  (SQL time,best-effort 解析;12h 無 AM/PM 值面值載入,不可用於 downtime,§5.2)。
- **HTML-entity**:`brief_desc`/`diag` 經 `html.unescape()` 還原中文。
- **lookup**:`work_type`/`wo_status` 照原樣種子(O/H、8 類);`vendor` 沿用 SA 切片表(增 SF)。
- **assigned_person / closed_by** 存 text(FK→person / app_user 延後)。
- **[UI] 空欄**:priority / action_taken / downtime / labor_hours / cost / opened_by /
  pm_source_id 建 nullable 空欄(比照 Asset),待 UI 補抽。
"""
