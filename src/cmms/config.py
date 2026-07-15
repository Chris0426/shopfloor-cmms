"""執行期設定。值由環境變數注入(雲端走 `flyctl secrets set`,ADR-013)。

祕密的「鍵清單 + 來源」見 infra/secrets-manifest.md;**值不入 git**。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CMMS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 部署環境標籤:local / staging / production
    app_env: str = Field(default="local")

    # 非同步 driver 連線字串(asyncpg);Fly 私網為 *.internal(ADR-013)
    database_url: str = Field(
        default="postgresql+asyncpg://cmms:cmms@localhost:5432/cmms",
    )

    # SQL echo(僅本機除錯)
    db_echo: bool = Field(default=False)

    # ADR-017 on-box principal JWS 的 JWKS 來源(Analytics `/.well-known/onbox-jwks.json`)。
    # 待 Analytics 部署網域(下游契約登記);未配置 → on-box API 回 503。
    onbox_jwks_url: str | None = Field(default=None)
    # Analytics 裁決:公鑰以**值**交付(靜態 JWKS JSON,不走 URL fetch)。設此 secret
    # (`CMMS_ONBOX_JWKS_JSON`,RFC7517 JWKS)→ **優先於** `onbox_jwks_url`;輪替 = 分析平台重交一次值。
    # 兩者皆未設 → on-box API 維持 503(現行 fail-closed)。
    onbox_jwks_json: str | None = Field(default=None)

    # ADR-019 媒體物件儲存(R2,S3 相容):app→R2 上傳附件照片 + 簽發短期 presigned url。
    # 值不入 git(見 infra/secrets-manifest.md);endpoint/access_key/secret 三者皆 None →
    # storage 自動退回 InMemory(本機 / CI 友善),三者齊備才連真實 R2。
    r2_endpoint: str | None = Field(default=None)
    r2_access_key_id: str | None = Field(default=None)
    r2_secret_access_key: str | None = Field(default=None)
    r2_media_bucket: str = Field(default="cmms-media")
    # presigned GET url 預設 TTL 秒(900 = 15 分;非祕密 config tunable)
    attachment_url_ttl_seconds: int = Field(default=900)

    # ADR-020/022 §5 per-user 憑證保管庫(Jira PAT)的封套加密主鑰(Fernet key,base64)。
    # 值不入 git(Fly secret CMMS_CREDENTIAL_MASTER_KEY);None → vault 在正式環境 fail-closed
    # (拒存/拒取,不明文 fallback)。dev/CI 以環境變數提供測試鑰。
    credential_master_key: str | None = Field(default=None)
    # web session 衍生的 MCP scoped token TTL 秒(ADR-020 決策 5;短命、可即時撤)
    scoped_token_ttl_seconds: int = Field(default=300)
    # agent 試點:operator CLI(`cmms mcp-token`)直發的 MCP token 預設 TTL 秒(12h)。
    # 與上面 gateway 短票(300s)語意分離、互不影響:pilot token 是「工作階段憑證」,
    # 仍 per-user、可即時撤(mcp_scoped_token.revoked_at)、只走 HTTPS。
    mcp_pilot_token_ttl_seconds: int = Field(default=43200)

    # 服務間讀取 JSON API 的 static bearer token(峰會裁決 消費端需求;值不入 git,Fly secret
    # CMMS_READ_API_TOKEN)。None + app_env=production → 受保護讀取端點一律 503(fail-closed,
    # 失敗模式 FP-3,絕不裸奔);None + 非 production(local/CI)→ 放行(本機/測試友善)。
    # 產生:python -c "import secrets; print(secrets.token_urlsafe(32))"
    read_api_token: str | None = Field(default=None)

    # RFQ 詢價信 SMTP(ADR-026;值不入 git,Fly secret)。host+username+password 三者齊備才走
    # 真 SmtpEmailSender,否則 get_email_sender() 退回 InMemoryEmailSender(dev/CI 友善,不真發)。
    smtp_host: str | None = Field(default=None)
    smtp_port: int = Field(default=465)  # implicit TLS
    smtp_username: str | None = Field(default=None)
    smtp_password: str | None = Field(default=None)
    rfq_from: str | None = Field(default=None)  # 寄件地址(如 purchasing@example.com)
    rfq_reply_to: str | None = Field(default=None)  # 回信地址(如 maintenance@example.com)

    # Slice B 工單 open/close 通知(email + Telegram;值不入 git,Fly secret)。皆選填 ——
    # 未配置的通道其 outbox 列維持 pending(不燒 attempts),配置後由 flush 補送。
    # Telegram:bot token(BotFather 發);未設 → telegram 通道略過。
    telegram_bot_token: str | None = Field(default=None)
    # 通知信寄件地址;未設則沿用 rfq_from(見 notify service)。email 送出重用 SMTP(smtp_* 三鍵)。
    notify_from: str | None = Field(default=None)
    # 通知內文的工單連結基底(非祕密;prod 走 fly.toml [env] 或此預設)。
    public_base_url: str = Field(default="https://cmms.example.com")

    # 續-15 Telegram 助理 webhook(dock 助理能力上 Telegram DM)。
    # webhook secret(值不入 git,Fly secret CMMS_TELEGRAM_WEBHOOK_SECRET):setWebhook 時設給
    # Telegram,之後每則 update 以 `X-Telegram-Bot-Api-Secret-Token` 標頭帶回;webhook 端常數時間
    # 比較。None + 任何環境 → webhook 一律 503(fail-closed,絕不裸奔收 update)。
    # 產生:python -c "import secrets; print(secrets.token_urlsafe(32))"
    telegram_webhook_secret: str | None = Field(default=None)
    # bot 使用者名(非祕密;settings 頁 deep link `https://t.me/<username>?start=<code>` 用)。
    telegram_bot_username: str = Field(default="shopfloor_cmms_bot")

    # ADR-020 dock → Hermes gateway 實接(工程師操作台助理)。
    # url 非祕密(prod 走 fly.toml [env] `CMMS_HERMES_GATEWAY_URL` = flycast 內網位址);
    # secret 是與 hermes app 共享的同一把 `HERMES_GATEWAY_SECRET`(值不入 git,Fly secret;
    # 故用 validation_alias 讀非 CMMS_ 前綴的共享名,兩 app 設同值)。
    # 任一未設 → 助理顯示「尚未啟用」誠實狀態(不假裝、不打 gateway)。
    hermes_gateway_url: str | None = Field(default=None)
    hermes_gateway_secret: str | None = Field(
        default=None, validation_alias="HERMES_GATEWAY_SECRET"
    )

    # ADR-020 決策 1 修訂(2026-07-06):cmms 直呼 Jira REST 轉發工單→MRQ(經 per-user PAT,
    # ADR-022 vault)。三值皆非祕密(base_url/project/issue-type,走 fly.toml [env] 或 secret 皆可),
    # PAT 才是祕密(vault,per-user)。base_url + project_key 齊 → forwarder 可建;缺 → 轉發路徑
    # 誠實 fail(outbox 標 config-missing,不假成功)。Jira Data Center 假設(Bearer PAT);
    # 若為 Jira Cloud(Basic email:token),改 forwarder auth header 一處(見 jira_forwarder.py)。
    jira_base_url: str | None = Field(default=None)  # 如 https://jira.example.com
    jira_mrq_project_key: str | None = Field(default=None)  # 如 MRQ
    jira_mrq_issue_type: str = Field(default="Task")  # MRQ issue-type 名稱

    @property
    def hermes_configured(self) -> bool:
        """助理功能是否已配置(url + secret 皆設)。任一缺 → 前端顯示「尚未啟用」。"""
        return bool(self.hermes_gateway_url and self.hermes_gateway_secret)

    @property
    def jira_forwarder_configured(self) -> bool:
        """Jira 轉發是否已配置(base_url + project_key 皆設;PAT 為 per-user,另查 vault)。"""
        return bool(self.jira_base_url and self.jira_mrq_project_key)


@lru_cache
def get_settings() -> Settings:
    """單例設定(快取)。"""
    return Settings()
