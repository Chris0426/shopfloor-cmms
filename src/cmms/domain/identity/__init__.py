"""Identity & access 切片(ADR-022):cmms 自建本地帳號 + server-side session + RBAC。

產生**可信** `human:<id>` 餵稽核(ADR-005)+ gated-write 確認(ADR-016)。用戶橫跨
Shopfloor + CMA(CMA 不在 the corporate SSO)→ 本地帳密而非 SSO。per-user locale 存此(ADR-023)。
per-user Jira PAT vault(ADR-022 §5)留 ADR-020 轉發切片。
"""
