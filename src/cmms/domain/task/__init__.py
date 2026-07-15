"""Task 切片(#2)— 保養任務範本(reference 主檔)。

migration + domain service + 讀取 API/MCP + 載入器。本切片只做 Task 本體
(task_no + description + is_active);寫入(建立/修改 task)留待後續切片。

範圍外(見 domain model 文件):
- TaskStep(保養細項 checklist)— 資料未提供(T1),不在本切片。
- standard_hours — 歸屬 ScheduledActivity(S5,Jordan 2026-06-20),不建於 task。
- 136 個閒置 task 的 is_active=false 標記 — 需 join ScheduledActivity,延到 SA 切片(#3)。
"""
