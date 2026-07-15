"""Domain service — 唯一寫入路徑(ADR-001)。

業務規則、狀態機、驗證、稽核都在這裡。MCP/CLI/API 只呼叫 domain service,
不直接碰 session 或下 SQL。每個切片(Asset、WorkOrder…)在此新增自己的 service 模組。
"""
