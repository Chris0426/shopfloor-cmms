"""ScheduledActivity 切片(#3)— PM 排程(`pm_schedule`,domain-model 03 的現代化命名)。

Asset↔Task 的橋接實體,也是 PM 工單的產生器。本切片**讀取為主**:
migration + domain service + 讀取 API/MCP + 載入器 + tests。

範圍與決議(2026-06-21):
- **寫入全延後**:suppress/unsuppress、PM 自動產生工單、next_due_date 重算延到後續切片
  (PM 產生需 WorkOrder 切片 + Shadow/Fixed 的 UI 資料)。
- **assignto 拆解**:`assigned_vendor` 建 vendor lookup(CMA/CMB,WorkOrder 切片共用);
  `assigned_person` 存文字,FK→person 延到 Contacts 切片(#6)。
- **T3 兌現**:載入後經 TaskService 把未被任何 SA 引用的 task 標 is_active=false。
- **[UI]-only 暫空**:`calendar_freq_type`(S1 值域未定 → 先存 text 不做 lookup)、
  `skip_weekends_holidays`、`pm_group`;`consumables` N:M(無資料)整個延後。
"""
