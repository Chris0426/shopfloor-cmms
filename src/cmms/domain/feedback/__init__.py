"""feedback — 說明中心使用者回饋的 domain 半邊(續-16)。

`/app/help/feedback` 由 email-only 改為 DB 為主(email 曾送達延遲)。回饋落 `help_feedback`
表(誰留、全文、是否已處理),顯示於 `/admin/proposals` 同頁獨立區;email 降為盡力通知。
"""
