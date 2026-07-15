"""TelegramSender — 工單通知的 Telegram adapter port(Slice B;mirror email.py 在精神上一致)。

bot token 齊備 → 真 `HttpTelegramSender`(呼 Telegram Bot API sendMessage,重用 httpx = Jira
forwarder 既有依賴);否則 `get_telegram_sender()` 退回 `InMemoryTelegramSender`(dev/CI 友善,
不真送、可檢視)。live 送出 = blocked(待 CMMS_TELEGRAM_BOT_TOKEN;見 secrets-manifest)。
通知落庫走 domain service(notify);此檔只管「送」。純文字(無 parse_mode),chat_id 可為
群組負數字串。
"""

from __future__ import annotations

import logging
import re
from typing import Protocol, runtime_checkable

import httpx

from cmms.config import get_settings

_API_BASE = "https://api.telegram.org"
_TIMEOUT_SECONDS = 10.0
_MESSAGE_LIMIT = 4096  # Telegram sendMessage 單則純文字上限

_logger = logging.getLogger("cmms.telegram")


class TelegramError(Exception):
    """Telegram 送出失敗(連線 / 逾時 / 非 2xx / ok:false)。**token 只在 URL、絕不入訊息。"""


@runtime_checkable
class TelegramSender(Protocol):
    async def send(self, *, chat_id: str, text: str) -> str:
        """送一則純文字訊息,回 provider message id(字串)。"""
        ...

    async def send_chat_action(self, *, chat_id: str, action: str = "typing") -> None:
        """送 chat action(如 typing 「對方正在輸入…」);純 UX 提示,失敗吞掉不 raise。"""
        ...


class NullTelegramSender:
    """no-op(不送、不記;完全靜默)。"""

    async def send(self, *, chat_id: str, text: str) -> str:
        return "null-msg"

    async def send_chat_action(self, *, chat_id: str, action: str = "typing") -> None:
        return None


class InMemoryTelegramSender:
    """記憶體 fake(dev/測試):把訊息收進 `sent`,不真送。可檢視內容。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []
        self.actions: list[dict[str, str]] = []

    async def send(self, *, chat_id: str, text: str) -> str:
        self.sent.append({"chat_id": chat_id, "text": text})
        return f"mem-{len(self.sent)}"

    async def send_chat_action(self, *, chat_id: str, action: str = "typing") -> None:
        self.actions.append({"chat_id": chat_id, "action": action})


class HttpTelegramSender:
    """真 Telegram Bot API:POST `/bot{token}/sendMessage`,json `{chat_id, text}`(純文字)。

    非 2xx 或回傳 `ok:false` → TelegramError(含截斷回應;bot token 只在 URL、絕不入例外/log)。
    回 message_id(字串)。
    """

    def __init__(self, *, token: str, timeout_seconds: float = _TIMEOUT_SECONDS) -> None:
        self._token = token
        self._timeout = timeout_seconds

    @staticmethod
    def _tail(body: str, limit: int = 300) -> str:
        return body.strip()[:limit]

    async def send(self, *, chat_id: str, text: str) -> str:
        url = f"{_API_BASE}/bot{self._token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json={"chat_id": chat_id, "text": text})
        except httpx.HTTPError as exc:  # 連線/逾時:type 名不含 token
            raise TelegramError(
                f"telegram request failed: {type(exc).__name__}"
            ) from exc
        if resp.status_code >= 300:
            raise TelegramError(f"telegram send failed: {resp.status_code} {self._tail(resp.text)}")
        payload = resp.json() or {}
        if not payload.get("ok"):
            raise TelegramError(f"telegram send not ok: {self._tail(resp.text)}")
        msg_id = (payload.get("result") or {}).get("message_id")
        return str(msg_id) if msg_id is not None else "sent"

    async def send_chat_action(self, *, chat_id: str, action: str = "typing") -> None:
        """POST `/sendChatAction`(顯示「對方正在輸入…」)。**失敗一律吞掉不 raise**:
        純 UX 提示,失敗不該中斷提問處理;bot token 只在 URL、絕不入 log。"""
        url = f"{_API_BASE}/bot{self._token}/sendChatAction"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                await client.post(url, json={"chat_id": chat_id, "action": action})
        except httpx.HTTPError as exc:  # type 名不含 token
            _logger.debug("sendChatAction failed (ignored): %s", type(exc).__name__)


def split_message(text: str, limit: int = _MESSAGE_LIMIT) -> list[str]:
    """把回覆文字切成不超過 `limit` 的段(Telegram sendMessage 上限 4096)。

    ≤limit 原樣一段;超過則優先在換行邊界切(保留段落),單行仍超長再硬切。永不回空清單
    (空 / 全空白輸入 → `[""]`,呼叫端至少送出一則,不靜默吞掉)。
    """
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    buf = ""
    for line in text.split("\n"):
        # 單行本身超長 → 先把已累積的送出,再把該行硬切成 limit 大小的塊
        while len(line) > limit:
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:limit])
            line = line[limit:]
        candidate = line if not buf else buf + "\n" + line
        if len(candidate) > limit:
            out.append(buf)
            buf = line
        else:
            buf = candidate
    if buf or not out:
        out.append(buf)
    return out


# 保守匹配「獨立出現」的 /app 相對路徑。判別關鍵:URL 內的 /app 前緣必為 ASCII URL 字元
# (host / 路徑段,如 `…example.com/app`),故**前一字元是 ASCII 英數 / `: / . _ @ -` 就不轉**
# (負向 lookbehind);字串起點 / 空白 / 括號 / 中文字前緣皆通過 → 轉。尾端 `(?![A-Za-z0-9])`
# 擋 `/apple` 誤命中 `/app`。已是 http(s):// 的網址其 /app 段前緣是 ASCII → 天然不動。寧漏不錯。
_APP_LINK_RE = re.compile(
    r"(?<![A-Za-z0-9:/._@-])(?P<path>/app(?:/[^\s)）」』】<>\"']*)?)(?![A-Za-z0-9])"
)


def absolutize_app_links(text: str, base_url: str) -> str:
    """把回覆文字中「獨立出現」的 `/app/...` 相對路徑轉成絕對(`base_url` + path)。

    Telegram 無 <base href>,相對連結不可點 → 轉絕對。只轉獨立路徑(行首 / 空白 / 括號 / 引號 /
    中文字後),已是 `http` 開頭的網址不動(其 host 後的 /app 段前緣是 ASCII → 不落匹配)。
    保守 regex:寧可漏轉不可錯轉。base_url 尾斜線去除後前綴。
    """
    if not base_url:
        return text
    prefix = base_url.rstrip("/")
    return _APP_LINK_RE.sub(lambda m: prefix + m.group("path"), text)


_sender: TelegramSender | None = None


def telegram_configured() -> bool:
    """bot token 是否已設 = 能真送 Telegram。未設 → notify flush 把 telegram 列留 pending。"""
    return bool(get_settings().telegram_bot_token)


def get_telegram_sender() -> TelegramSender:
    """回目前 Telegram sender。bot token 已設 → HttpTelegramSender;否則 InMemory(fallback)。"""
    global _sender
    if _sender is None:
        token = get_settings().telegram_bot_token
        _sender = HttpTelegramSender(token=token) if token else InMemoryTelegramSender()
    return _sender
