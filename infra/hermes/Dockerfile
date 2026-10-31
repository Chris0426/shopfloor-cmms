# Hermes 常駐 gateway 映像(ADR-020 / ADR-019 Hermes 沙箱)。獨立 Fly app
# `shopfloor-cmms-hermes`,同 org 同私網(sin region),internal-only(不對公網開放)。
#
# 內容 = python slim(FastAPI gateway)+ node(@openai/codex CLI)。gateway 每請求
# 跑 `codex exec`(headless),codex 用 Jordan 的 OpenAI 訂閱 OAuth 憑證(codex login
# --device-auth 一次性裝置流),憑證持久化在 Fly volume 掛載的 CODEX_HOME=/data/codex。
# 服務以非 root(hermes)身分跑;/data volume 首次掛載為 root 擁有,entrypoint 於啟動時
# 修正擁有權後再 gosu 降權(見 entrypoint.sh)。
# 映像同時帶兩個後端(HERMES_BACKEND=codex|anthropic,預設 codex):codex CLI(OAuth 憑證在
# volume)與 Anthropic SDK(金鑰由 ANTHROPIC_API_KEY 環境變數注入,絕不烤進映像)。

FROM python:3.12-slim

# node/npm(裝 codex CLI)+ gosu(啟動時降權)+ ca-certificates/curl(TLS / 驗證)。
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs npm ca-certificates curl gosu \
 && rm -rf /var/lib/apt/lists/*

# OpenAI Codex CLI(headless `codex exec` + remote MCP + 訂閱 OAuth）。
RUN npm install -g @openai/codex

# gateway 依賴(fastapi + uvicorn;gateway.py 不依賴 cmms/其他套件)。
# ★ 所有依賴一律釘版、與 uv.lock 一致:demo_reset.sh 每 30 分鐘 `compose build` 一次,
#   不釘版等於每輪都可能悄悄升級(2026-10-31 mcp 2.x 事故就是這樣來的)。
RUN pip install --no-cache-dir "fastapi==0.138.1" "uvicorn[standard]==0.49.0"
# Anthropic 後端(SDK + MCP extra + mcp);codex 後端不使用但一併安裝。
# 釘死版本、與 repo 的 uv.lock 一致(anthropic 2.3.0 / mcp 1.28.0):mcp 2.x 的
# streamable_http_client 介面不同(只 yield (read, write)),不釘版曾讓線上裝到 2.x 而 502。
# 升級時 uv.lock 與這裡一起改。
RUN pip install --no-cache-dir "anthropic[mcp]==2.3.0" "mcp==1.28.0"

# CODEX_HOME 指向 volume → OAuth 憑證(auth.json)與 config 持久化跨重啟。
ENV CODEX_HOME=/data/codex

# 非 root 執行身分(護欄:AI 不當生產操作員;最小權限)。
RUN useradd -m -u 10001 hermes

WORKDIR /app
COPY app/ /app/app/
# board persona 檔(gateway 以 <app 上一層>/personas/board.md 解析 = /app/personas/board.md)。
COPY personas/ /app/personas/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080
# entrypoint(root)修 /data 擁有權 → gosu 降為 hermes 跑 CMD。
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.gateway:app", "--host", "0.0.0.0", "--port", "8080"]
