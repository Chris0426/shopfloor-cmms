"""notify — 工單 open/close 通知(email + Telegram)切片(Slice B)。

收件人詞彙(notify_recipient,不綁 user_account,管理者可收)+ outbox 佇列
(notification_outbox,逐列 flush、冪等)+ 固定 zh-TW 模板(render.py)。
"""
