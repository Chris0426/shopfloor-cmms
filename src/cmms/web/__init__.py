"""工程師操作台 web UI(ADR-019 thin client:HTMX + Jinja2 伺服器渲染)。

由 `cmms` app 同進程服務,掛在 `/app` 前綴(避免撞 `/work-orders` 等 JSON 讀取 API
—— 那些是 Analytics 的對外契約,不可動)。所有寫入仍走 domain service(護欄 #1)。
多語系見 ADR-023(`web/i18n.py`);樣式 token 見 `web/static/app.css`。
"""
