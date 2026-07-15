"""MCP server — thin client(ADR-001/003)。

工具 = 領域操作(`get_asset`、`close_work_order`…),**不是** `run_sql` / `update_table`。
讀取工具開放;寫入工具須 dry-run + 確認(ADR-004),高風險操作不暴露給 agent。
對外用 Streamable HTTP transport,本地測試可用 stdio(ADR-012)。
"""
