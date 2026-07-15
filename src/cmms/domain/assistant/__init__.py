"""assistant 切片(ADR-020 dock 助理)—— 對話落 DB、跨頁持久 + 多 session。

Jordan 實測 Hermes 助理後的回饋:整頁導覽會讓 dock 對話全滅(重大缺失),且需要能同時
保留多個對話(session)。故把「純前端 hidden history」升級為 DB-backed:

- `assistant_conversation`:每人多個對話,`closed_at` null = 開啟中(user 主動結束才設)。
- `assistant_message`:對話的逐則訊息(role = user / assistant)。

治理:所有讀寫**user-scoped、擁有權在 domain 層強制**(查詢一律 where user_id=呼叫者;
non-owner 拿 None / 拒絕,不靠 route 守門)。寫入走 `self.write()`、actor=`Actor.human(<user_id>)`
(護欄 #1 單一寫入路徑 + #4 全稽核)。開啟中對話每人上限 `MAX_OPEN_CONVERSATIONS`。

gateway 失敗的輪次**不落 DB**(user 可重送);token / gateway secret 絕不進本切片的資料。
"""
