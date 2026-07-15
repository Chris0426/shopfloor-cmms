"""telegram_bridge — Telegram DM 助理入口的 domain 半邊(續-15)。

一次性綁定碼(telegram_link_code,TTL 10 分)+ chat_id↔user 連結(telegram_link,一人一 DM)
+ webhook 冪等去重(telegram_update_seen)。綁定成功即回填通知 chat_id
(`NotificationService.fill_telegram_chat_id`)。webhook / UI 是下一棒,本模組只提供 service。
"""
