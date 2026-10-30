"""Anthropic Claude 後端(`HERMES_BACKEND=anthropic`)—— gateway 的薄封裝,零 cmms 耦合。

- 客戶端:`AsyncAnthropic`,金鑰**只**取環境變數 `ANTHROPIC_API_KEY` 並明確傳入(不讓 SDK 落到
  其他憑證來源);未設 → `LLMConfigError`(gateway 回 503)。timeout 60s、SDK 內建重試 2 次。
- `/chat`:gateway 自己當 MCP client(streamable HTTP 打 cmms `/mcp`,bearer = 該使用者的 scoped
  token,只在記憶體的 HTTP header,不落 log),工具經 `async_mcp_tool` 轉換後交給
  `client.beta.messages.tool_runner` 跑工具迴圈(上限 `max_iterations`)。工具依名稱排序 →
  快取前綴穩定;persona 是 system 第一塊並掛 `cache_control`(tools + persona 一起快取)。
- `/mrq-description`:純文字、零工具,`client.messages.create`。
- 每次 API 呼叫依該次 usage 計價(`ledger.estimate_usd`,Haiku 長 prompt 分層照實算),
  加總後回給 gateway 記帳。
- `stop_reason == "refusal"` → 固定句(依 locale),不假裝有答案;工具迴圈到上限仍要呼叫工具 → 502。
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import anthropic
from anthropic import AsyncAnthropic

try:  # 套件載入(uvicorn app.gateway:app)
    from . import ledger
except ImportError:  # 測試以檔案路徑載入
    import importlib.util as _ilu
    import pathlib as _pl
    import sys as _sys

    ledger = _sys.modules.get("hermes_ledger")
    if ledger is None:
        _spec = _ilu.spec_from_file_location(
            "hermes_ledger", _pl.Path(__file__).with_name("ledger.py")
        )
        ledger = _ilu.module_from_spec(_spec)
        _sys.modules[_spec.name] = ledger
        _spec.loader.exec_module(ledger)

# ── 模型(可經環境變數覆寫;Opus 不設為任何預設,只能明確指定)──────────────────────
DEFAULT_MODEL_CHAT = "claude-haiku-5-5"
DEFAULT_MODEL_TEXT = "claude-haiku-5-5"
DEFAULT_MODEL_DEEP = "claude-sonnet-5-5"

# 「詳細分析」類請求才走 DEEP;其餘一律 CHAT。
_DEEP_MARKERS = ("詳細分析", "深入分析", "deep analysis", "phân tích chi tiết")

# max_tokens 是單次回應上限(含思考);實際計費只看 usage,設寬避免截斷。
CHAT_MAX_TOKENS = 8192
TEXT_MAX_TOKENS = 8192
CHAT_MAX_ITERATIONS = 8

# 有 server-side fallback 的模型(Claude API 上 Sonnet 5.5 只接受 "default" 形式);Haiku 無此功能。
_FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

REFUSAL_REPLIES = {
    "zh-TW": "這個請求我無法協助。",
    "vi": "Tôi không thể hỗ trợ yêu cầu này.",
    "en": "I can't help with that request.",
}


class LLMConfigError(RuntimeError):
    """設定錯誤(金鑰未設 / 無效 / 權限不足 / 模型不存在);gateway → 503。"""


class LLMBackendError(RuntimeError):
    """後端暫時失敗(429 / 5xx / 連線 / 逾時 / 工具迴圈超限 / 空回覆);gateway → 502。"""

    def __init__(self, message: str, retry_after: str | None = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class Usage:
    """一次請求(可能含多次 API 呼叫)的累計 usage 與估價。"""

    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    usd: float = 0.0
    api_calls: int = 0

    def add(self, message: Any) -> None:
        u = getattr(message, "usage", None)
        if u is None:
            return
        model = getattr(message, "model", None) or self.model
        inp = int(getattr(u, "input_tokens", 0) or 0)
        out = int(getattr(u, "output_tokens", 0) or 0)
        cr = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        cw = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        self.input_tokens += inp
        self.output_tokens += out
        self.cache_read += cr
        self.cache_write += cw
        self.usd += ledger.estimate_usd(model, inp, out, cr, cw)
        self.api_calls += 1


@dataclass
class LLMResult:
    text: str
    usage: Usage = field(default_factory=Usage)
    refused: bool = False


# ── 設定 ─────────────────────────────────────────────────────────────────────
def model_chat() -> str:
    return os.environ.get("HERMES_MODEL_CHAT") or DEFAULT_MODEL_CHAT


def model_text() -> str:
    return os.environ.get("HERMES_MODEL_TEXT") or DEFAULT_MODEL_TEXT


def model_deep() -> str:
    return os.environ.get("HERMES_MODEL_DEEP") or DEFAULT_MODEL_DEEP


def pick_chat_model(message: str, tier: str | None) -> str:
    """路由:request 帶 tier="deep" 或訊息含「詳細分析 / deep analysis」→ DEEP,其餘 CHAT。"""
    lowered = message.lower()
    if tier == "deep" or any(m in lowered for m in _DEEP_MARKERS):
        return model_deep()
    return model_chat()


def make_client() -> AsyncAnthropic:
    """建立客戶端;金鑰只讀 `ANTHROPIC_API_KEY`(明確傳入)。"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise LLMConfigError("ANTHROPIC_API_KEY not configured")
    return AsyncAnthropic(api_key=key, timeout=60.0, max_retries=2)


# ── 組裝(純函式,易測)──────────────────────────────────────────────────────
def build_system(persona: str, extra_lines: list[str]) -> list[dict[str, Any]]:
    """system 第一塊 = persona(掛快取標記);每請求不同的提示放在其後(不破壞快取前綴)。"""
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": persona, "cache_control": {"type": "ephemeral"}}
    ]
    extra = "\n".join(line for line in extra_lines if line)
    if extra:
        blocks.append({"type": "text", "text": extra})
    return blocks


def build_messages(history: list[tuple[str, str]], message: str) -> list[dict[str, Any]]:
    """歷史 → 真正的 messages[](user / assistant);開頭的 assistant 丟掉(首則須為 user)。"""
    msgs: list[dict[str, Any]] = []
    for role, content in history:
        role = "assistant" if role == "assistant" else "user"
        if not msgs and role == "assistant":
            continue
        if content:
            msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": message})
    return msgs


def fallback_kwargs(model: str) -> dict[str, Any]:
    """Sonnet / Opus 走 server-side fallback("default" = 依拒答類別自動路由)。"""
    if model in _FALLBACK_MODELS:
        return {"betas": [_FALLBACK_BETA], "fallbacks": "default"}
    return {}


def refusal_reply(locale: str | None) -> str:
    if locale and locale.startswith("zh"):
        return REFUSAL_REPLIES["zh-TW"]
    if locale and locale.startswith("vi"):
        return REFUSAL_REPLIES["vi"]
    return REFUSAL_REPLIES["en"]


def _text_of(message: Any) -> str:
    blocks = getattr(message, "content", None) or []
    parts = [b.text for b in blocks if getattr(b, "type", "") == "text"]
    return "\n".join(p for p in parts if p).strip()


# ── 錯誤對應(最特定優先)───────────────────────────────────────────────────────
def translate_error(exc: Exception) -> Exception:
    """SDK 例外 → LLMConfigError(503)/ LLMBackendError(502)。其他例外原樣回傳。"""
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return LLMConfigError("anthropic credentials rejected")
    if isinstance(exc, anthropic.NotFoundError):
        return LLMConfigError("anthropic model or endpoint not found")
    if isinstance(exc, anthropic.RateLimitError):
        retry = exc.response.headers.get("retry-after") if exc.response is not None else None
        return LLMBackendError("anthropic rate limited", retry_after=retry)
    if isinstance(exc, anthropic.BadRequestError):
        return LLMBackendError("anthropic rejected the request")
    if isinstance(exc, anthropic.APIStatusError):
        return LLMBackendError(f"anthropic error {exc.status_code}")
    if isinstance(exc, anthropic.APITimeoutError):
        return LLMBackendError("anthropic timeout")
    if isinstance(exc, anthropic.APIConnectionError):
        return LLMBackendError("anthropic unreachable")
    return exc


# ── MCP 工具(seam:測試以假實作取代)─────────────────────────────────────────
@contextlib.asynccontextmanager
async def open_mcp_tools(
    mcp_url: str, scoped_token: str, extra_headers: dict[str, str] | None = None
) -> AsyncIterator[list[Any]]:
    """連 MCP 端點(bearer = scoped token)→ 依名稱排序的 tool_runner 工具清單。

    `extra_headers`:附加的 HTTP header(例:board 的 `X-Board-Turn`);呼叫端負責淨化。
    ★ Authorization 恆以 scoped token 為準,extra_headers 蓋不掉它。
    """
    from anthropic.lib.tools.mcp import async_mcp_tool
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    headers = {**(extra_headers or {}), "Authorization": f"Bearer {scoped_token}"}
    async with (
        create_mcp_http_client(headers=headers) as http_client,
        streamable_http_client(mcp_url, http_client=http_client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        tools = sorted(listed.tools, key=lambda t: t.name)
        yield [async_mcp_tool(t, session) for t in tools]


# ── 兩個入口 ─────────────────────────────────────────────────────────────────
async def chat(
    client: Any,
    *,
    persona: str,
    extra_lines: list[str],
    history: list[tuple[str, str]],
    message: str,
    model: str,
    mcp_url: str,
    scoped_token: str,
    locale: str | None,
    tools_opener=None,
    extra_headers: dict[str, str] | None = None,
) -> LLMResult:
    """一題對話:MCP 工具 + tool_runner;回 (回覆, 累計 usage, 是否拒答)。

    `extra_headers` 為空 → opener 只收 (mcp_url, scoped_token),與加入此參數前完全相同。
    """
    opener = tools_opener or open_mcp_tools
    usage = Usage(model=model)
    last = None
    opened = (
        opener(mcp_url, scoped_token, extra_headers=extra_headers)
        if extra_headers
        else opener(mcp_url, scoped_token)
    )
    try:
        async with opened as tools:
            runner = client.beta.messages.tool_runner(
                model=model,
                max_tokens=CHAT_MAX_TOKENS,
                system=build_system(persona, extra_lines),
                messages=build_messages(history, message),
                tools=tools,
                output_config={"effort": "medium" if model != model_chat() else "low"},
                max_iterations=CHAT_MAX_ITERATIONS,
                **fallback_kwargs(model),
            )
            async for msg in runner:
                usage.add(msg)
                last = msg
    except anthropic.APIError as exc:
        err = translate_error(exc)
        err.usage = usage  # 已花掉的部分仍要記帳
        raise err from exc
    if last is None:
        raise LLMBackendError("anthropic produced no message")
    if last.stop_reason == "refusal":
        return LLMResult(text=refusal_reply(locale), usage=usage, refused=True)
    if last.stop_reason == "tool_use":
        err = LLMBackendError("tool loop limit reached")
        err.usage = usage
        raise err
    text = _text_of(last)
    if not text:
        err = LLMBackendError("anthropic produced an empty reply")
        err.usage = usage
        raise err
    return LLMResult(text=text, usage=usage)


async def complete_text(client: Any, *, persona: str, prompt: str, model: str) -> LLMResult:
    """純文字生成(零工具)。拒答 → LLMBackendError(對外摘要不給替代文字,誠實失敗)。"""
    usage = Usage(model=model)
    try:
        msg = await client.messages.create(
            model=model,
            max_tokens=TEXT_MAX_TOKENS,
            system=build_system(persona, []),
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "medium"},
        )
    except anthropic.APIError as exc:
        raise translate_error(exc) from exc
    usage.add(msg)
    if msg.stop_reason == "refusal":
        err = LLMBackendError("anthropic declined the request")
        err.usage = usage
        raise err
    text = _text_of(msg)
    if not text:
        err = LLMBackendError("anthropic produced an empty reply")
        err.usage = usage
        raise err
    return LLMResult(text=text, usage=usage)
