# Hermes 常駐 gateway 映像(ADR-020 / ADR-019 Hermes 沙箱)。獨立 Fly app
# `shopfloor-cmms-hermes`,同 org 同私網(sin region),internal-only(不對公網開放)。
#
# 內容 = python slim(FastAPI gateway)+ node(@openai/codex CLI)。gateway 每請求
# 跑 `codex exec`(headless),codex 用 Jordan 的 OpenAI 訂閱 OAuth 憑證(codex login
# --device-auth 一次性裝置流),憑證持久化在 Fly volume 掛載的 CODEX_HOME=/data/codex。
# 服務以非 root(hermes)身分跑;/data volume 首次掛載為 root 擁有,entrypoint 於啟動時
# 修正擁有權後再 gosu 降權(見 entrypoint.sh)。

FROM python:3.12-slim

# node/npm(裝 codex CLI)+ gosu(啟動時降權)+ ca-certificates/curl(TLS / 驗證)。
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs npm ca-certificates curl gosu \
 && rm -rf /var/lib/apt/lists/*

# OpenAI Codex CLI(headless `codex exec` + remote MCP + 訂閱 OAuth）。
RUN npm install -g @openai/codex

# gateway 依賴(僅 fastapi + uvicorn;gateway.py 不依賴 cmms/其他套件)。
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.32"

# CODEX_HOME 指向 volume → OAuth 憑證(auth.json)與 config 持久化跨重啟。
ENV CODEX_HOME=/data/codex

# 非 root 執行身分(護欄:AI 不當生產操作員;最小權限)。
RUN useradd -m -u 10001 hermes

WORKDIR /app
COPY app/ /app/app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080
# entrypoint(root)修 /data 擁有權 → gosu 降為 hermes 跑 CMD。
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.gateway:app", "--host", "0.0.0.0", "--port", "8080"]
