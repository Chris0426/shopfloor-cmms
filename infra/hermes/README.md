# Hermes 常駐 gateway(`infra/hermes/`)

> agent 試點的**伺服端**(ADR-020 / ADR-019 Hermes 沙箱)。獨立 Fly app
> `shopfloor-cmms-hermes`,internal-only(不對公網開放)。任何登入 cmms 操作台的工程師,
> 由 dock 命令它查 / 改 cmms 資料;LLM 額度 = Jordan 的 OpenAI 訂閱 OAuth。

## 架構(一頁)

```
dock(web,之後接)
  → cmms web(session → mint 300s scoped token)
  → 本 gateway  POST /chat   (Fly 私網 flycast + 共享 secret X-Hermes-Secret)
  → codex exec(headless;OAuth=訂閱憑證;MCP=cmms /mcp,bearer=該使用者 scoped token)
  → 回覆 {"reply": ...}
```

- **codex 用訂閱 OAuth**:容器內 `codex login --device-auth` 一次性裝置流授權;憑證
  (`auth.json`)落 Fly volume 的 `CODEX_HOME=/data/codex`,跨重啟持久化、對使用者隱形。
- **scoped token 帶身分**:每請求把該使用者 scoped token 以環境變數 `CMMS_MCP_TOKEN`
  注入 codex 子行程;codex config `mcp_servers.cmms.bearer_token_env_var` 指向它。
  **token 永不入 argv / log**。
- **後端可切換**(`HERMES_BACKEND`,預設 `codex`):`codex` = 上述 OpenAI Codex CLI,需要 volume
  `/data/codex` 上的 OAuth 憑證;`anthropic` = 經 Anthropic SDK 呼叫 Claude,需要環境變數
  `ANTHROPIC_API_KEY`,另在 `/data` 放 SQLite 帳本(`ledger.sqlite`:用量、預算、每來源每日問題數)。
  公開示範站(`docker-compose.public.yml`)跑 `anthropic`。
- **治理不隨 agent 變**:gateway 對 cmms 只走 `/mcp`(護欄 #1),寫入=提案→admin
  confirm(ADR-016/027,cmms domain 強制,gateway 縮不掉);EID 不猜(ADR-027/D9,persona 硬性)。

## 檔案

| 檔 | 作用 |
|---|---|
| `app/gateway.py` | 單檔 FastAPI:`POST /chat` + `GET /health`;組 prompt → 跑 `codex exec` → 回覆。零 cmms 耦合(只 stdlib + fastapi)。 |
| `Dockerfile` | python slim + node(`@openai/codex`)+ gosu;`CODEX_HOME=/data/codex`;非 root(hermes)跑。 |
| `entrypoint.sh` | root 修 `/data` 擁有權 → gosu 降為 hermes 跑 uvicorn。 |
| `fly.hermes.toml` | app=`shopfloor-cmms-hermes`、sin、volume `hermes_data`→/data、internal-only、512MB。 |

## 環境變數(非祕密,`[env]` 在 fly.hermes.toml)

「後端」欄:通用 = 兩個後端皆用;codex / anthropic = 僅該後端生效。

| 鍵 | 預設 | 後端 | 說明 |
|---|---|---|---|
| `HERMES_BACKEND` | `codex` | 通用 | 後端選擇:`codex` / `anthropic`(其他值視為 `codex`)。`/health` 會回報目前後端。 |
| `CMMS_MCP_URL` | `https://cmms.example.com/mcp` | 通用 | 連的 cmms remote MCP 端點。 |
| `HERMES_LLM_TIMEOUT` | `120` | 通用 | 單次 LLM 請求整體逾時秒數(codex 逾時強殺 → 502)。舊名 `HERMES_CODEX_TIMEOUT` 仍相容。prod `fly.hermes.toml` 設 `180`。 |
| `HERMES_MAX_CONCURRENCY` | `2` | 通用 | 併發上限(asyncio semaphore)。 |
| `HERMES_MODEL` | (未設 = codex 預設) | codex | `/chat` 模型覆寫(如 `gpt-5.4-mini`);`/mrq-description` 不吃。以 secret 設/撤即可 A/B(免重 build)。 |
| `CODEX_HOME` | `/data/codex` | codex | codex OAuth 憑證 / config / 快取(在 volume)。 |
| `HERMES_MODEL_CHAT` | `claude-haiku-5-5` | anthropic | `/chat` 一般問題的模型。 |
| `HERMES_MODEL_TEXT` | `claude-haiku-5-5` | anthropic | `/mrq-description`(純文字、零工具)的模型。 |
| `HERMES_MODEL_DEEP` | `claude-sonnet-5-5` | anthropic | 請求帶 `tier="deep"`(或訊息含「詳細分析」「deep analysis」等)時改用。 |
| `HERMES_LEDGER_PATH` | `/data/ledger.sqlite` | anthropic | SQLite 成本帳本路徑(須在持久 volume 上)。 |
| `HERMES_DAILY_BUDGET_USD` | `0.5` | anthropic | 每日(UTC)預算上限,美元,**逐 target** 各一桶(cmms / board 各 0.5);達上限 → 503。 |
| `HERMES_DAILY_BUDGET_USD_CMMS` / `_BOARD` | (未設 = 用上一列) | anthropic | 逐 target 覆寫日預算。 |
| `HERMES_MONTHLY_BUDGET_USD` | `10.0` | anthropic | 每月(UTC)預算上限,美元,**全 target 共用一池**;達上限 → 503。 |
| `HERMES_DAILY_QUESTIONS_PER_CLIENT` | `10` | anthropic | cmms target:`/chat` 每個來源每個 UTC 日的問題數上限(逐 target 計數);超過 → 429。 |
| `HERMES_DAILY_QUESTIONS_PER_CLIENT_BOARD` | `3` | anthropic | board target 的同上限,另計;`0` = 不限。 |
| `BOARD_MCP_URL` | (無預設) | anthropic | board target 的 MCP 端點;未設 → board 請求 503。request 不能帶 URL。 |
| `HERMES_PERSONA_BOARD_FILE` | (隨附 `personas/board.md`) | anthropic | board persona 檔路徑;讀不到 → 503。 |

anthropic 後端的工具迴圈上限為 8 輪(`CHAT_MAX_ITERATIONS`);超限仍要呼叫工具 → 502。
模型回 `refusal` 時回固定拒答句(依 locale),不假裝有答案。

## 祕密

| 鍵 | 後端 | 說明 |
|---|---|---|
| `HERMES_GATEWAY_SECRET` | 通用 | `/chat` 的共享 secret(常數時間比較)。**同一把也要設在 cmms app**,dock 接線時 cmms 帶 `X-Hermes-Secret` 打本 gateway。未設 → `/chat` 一律 503(fail-closed)。 |
| `ANTHROPIC_API_KEY` | anthropic | Anthropic API 金鑰,**只**從這個環境變數讀(未設 → 503)。不進 git / log / 對話 / commit message。 |

產生 gateway secret:`python -c "import secrets; print(secrets.token_urlsafe(32))"`。登記見
`../secrets-manifest.md`。

### Anthropic 金鑰流程

1. 在 Anthropic Console 為本 gateway 建一把**專用** key(不與其他用途共用)。
2. **不要開啟自動儲值(auto-reload),也不要預先多買額度。** 成本天花板 = 上列環境變數預算
   + 帳戶當下餘額,兩者取小。
3. 金鑰只經環境變數傳入:compose 從 repo 根目錄 `.env` 讀取(`.env` 不入 git);
   不貼進 git、log、對話或 commit message。
4. 輪替:在 Console 發新 key → 換掉環境變數值並重啟 hermes → 停用舊 key。
5. 冒煙測試(會真的打一次 API、計費;印出模型、usage、估算成本與一列帳本紀錄):
   - **Oracle host**(線上 compose):在 hermes 映像裡跑腳本,repo 唯讀掛進容器:

     ```bash
     cd /opt/shopfloor-cmms && docker compose run --rm --no-deps -T -v /opt/shopfloor-cmms:/repo:ro --entrypoint python hermes /repo/scripts/hermes_smoke.py
     ```

     `ANTHROPIC_API_KEY` 由 compose 從根目錄 `.env` 注入 hermes 服務,**不需要**在 shell
     export。`--no-deps` 不會連帶起 app;腳本走純文字路徑、不碰 MCP。帳本預設寫暫存檔,
     不污染 `/data/ledger.sqlite`。
   - **本機開發**:`export ANTHROPIC_API_KEY=...`(當下 shell 設,不寫進檔案)後
     `uv run scripts/hermes_smoke.py`。

### 成本與限額(anthropic)

- `GET /health` 回報 `backend` 與 `mcp_targets.{cmms,board}.{configured,url}`;anthropic 時另有 `budget.monthly`(共用池)與逐 target 的 `budget.daily.{cmms,board}`(各含 `spent_usd` / `limit_usd` / `state`=ok|warning|exhausted),以及 `questions`(今日題數總計:`total` 與逐 target 的 `questions` / `clients`,只有計數、不含來源雜湊,維運不用進容器查 SQLite)。
- 預算用盡 → `/chat` 與 `/mrq-description` 都回 **503**(cmms 顯示「預算」氣泡);fail-closed,呼叫前就拒絕。
  達 80% 為 warning。
- 每來源每日問題數超限 → **429** + `Retry-After`(到 UTC 午夜的秒數;cmms 顯示「額度」氣泡)。
  題數在**拿到回答或 refusal 後**才計;502 / 503 / timeout 不扣,訪客在故障期間重試不會耗光額度。
  來源鍵(web = 使用者 IP)以 gateway secret 做 HMAC-SHA256,只存雜湊前綴,不存原值。
- 價格寫死在 `app/ledger.py` 的 `PRICES`,2026-10-29 核對(USD / 百萬 token):

| 模型 | input | output | cache write | cache read |
|---|---|---|---|---|
| `claude-haiku-5-5` | 0.10 | 0.50 | 0.125 | 0.01 |
| `claude-sonnet-5-5` | 2.00 | 10.00 | 2.50 | 0.20 |
| `claude-opus-5-5` | 4.00 | 20.00 | 5.00 | 0.20 |

  Haiku 單次 prompt(input + cache read + cache write)> 100K token 時整筆改用長層價
  (0.50 / 2.50 / 0.625 / 0.05)。未列在表上的模型以表中最貴者估算(寧可高估、提早熔斷)。

## 第二個產品(board target)

完整對外契約見 [`CONTRACT.md`](CONTRACT.md)。重點:

- request 帶 `target=board`(預設 `cmms`;其他值 → 422)。
- 自己的 MCP URL:只從伺服端環境變數 `BOARD_MCP_URL` 取,request 不帶 URL;未設 → 503。
- 自己的 persona 檔:`personas/board.md`(映像內 `/app/personas/board.md`),可用 `HERMES_PERSONA_BOARD_FILE` 替換。
- 自己的每日桶:日預算與每來源題數都逐 target 分開計。
- 月預算與 cmms 共用同一池。

## 部署 / 驗證

完整 operator runbook(apps create → volume → secret → deploy → `codex login` →
驗證)見 本目錄 README。快照:

```powershell
# 1) 建 app + volume(sin)
flyctl apps create shopfloor-cmms-hermes
flyctl volumes create hermes_data -a shopfloor-cmms-hermes -r sin -s 1

# 2) secret(同一把也 set 到 shopfloor-cmms)
$s = python -c "import secrets; print(secrets.token_urlsafe(32))"
flyctl secrets set -a shopfloor-cmms-hermes HERMES_GATEWAY_SECRET="$s"
flyctl secrets set -a shopfloor-cmms        HERMES_GATEWAY_SECRET="$s"

# 3) deploy(internal-only:不 allocate 公網 IP;flycast 私網位址)
cd infra/hermes
flyctl deploy -c fly.hermes.toml
flyctl ips allocate-v6 --private -a shopfloor-cmms-hermes   # flycast(私有 v6,非公網)

# 4) 一次性 OAuth 授權(裝置流:印 URL+code → 瀏覽器授權一次,憑證落 /data)
flyctl ssh console -a shopfloor-cmms-hermes
#   進去後(容器內):
#   CODEX_HOME=/data/codex gosu hermes codex login --device-auth
#   → 依畫面把 URL 貼到瀏覽器、輸入 code、用 Jordan 的 ChatGPT 帳號授權。

# 5) 驗證(從 cmms app machine 打私網 flycast;需帶 secret)
flyctl ssh console -a shopfloor-cmms
#   curl -s http://shopfloor-cmms-hermes.flycast:8080/health
#   curl -s -X POST http://shopfloor-cmms-hermes.flycast:8080/chat \
#     -H "X-Hermes-Secret: <secret>" -H "content-type: application/json" \
#     -d '{"message":"ping cmms","scoped_token":"<mcp scoped token>"}'
```

### docker-compose 變體(anthropic 後端)

```bash
# 在 repo 根目錄 .env 放入 ANTHROPIC_API_KEY 與 HERMES_GATEWAY_SECRET(.env 不入 git)
docker compose up -d --build hermes
docker compose exec hermes curl -s http://127.0.0.1:8080/health
curl -s http://127.0.0.1:8797/health   # 或在 host 端(公開 compose 疊加 docker-compose.public.yml 後)
```

> scoped token 的發放見 `docs/integrations/agent-pilot.md`(`cmms mcp-token`)。dock 正式
> 接線時由 cmms web 於 session 換發短時效 token,不需人工。
