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

| 鍵 | 預設 | 說明 |
|---|---|---|
| `CMMS_MCP_URL` | `https://cmms.example.com/mcp` | codex 連的 cmms remote MCP 端點。 |
| `HERMES_MODEL` | (未設 = codex 預設) | `/chat` 模型覆寫(如 `gpt-5.4-mini`);`/mrq-description` 不吃。以 secret 設/撤即可 A/B(免重 build)。 |
| `CODEX_HOME` | `/data/codex` | codex OAuth 憑證 / config / 快取(在 volume)。 |
| `HERMES_CODEX_TIMEOUT` | `120` | 單次 codex exec 逾時秒數(逾時強殺 → 502)。prod `fly.hermes.toml` 設 `180`。 |
| `HERMES_MAX_CONCURRENCY` | `2` | 併發 codex 上限(asyncio semaphore)。 |

## 祕密

| 鍵 | 說明 |
|---|---|
| `HERMES_GATEWAY_SECRET` | `/chat` 的共享 secret(常數時間比較)。**同一把也要設在 cmms app**,dock 接線時 cmms 帶 `X-Hermes-Secret` 打本 gateway。未設 → `/chat` 一律 503(fail-closed)。 |

產生:`python -c "import secrets; print(secrets.token_urlsafe(32))"`。登記見
`../secrets-manifest.md`。

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

> scoped token 的發放見 `docs/integrations/agent-pilot.md`(`cmms mcp-token`)。dock 正式
> 接線時由 cmms web 於 session 換發短時效 token,不需人工。
