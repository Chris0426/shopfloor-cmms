"""Telegram 助理 webhook(續-15;內部規格)—— dock 助理能力上 Telegram DM。

收 Telegram `POST /telegram/webhook`:自帶 secret header 閘門(不疊 static bearer,見 api/auth.py
豁免清單)。**永遠回 200**(除 secret 未設 503 / secret 錯 401)—— Telegram 對非 2xx 會重送,
故容錯:缺欄 / 非 private / 無 text / 已見過 update 一律 200 忽略,500 只留給真 bug。

指令分流(text.strip()):
- `/start <code>`:兌換綁定碼 → 綁定 chat_id、回填通知 chat_id(以綁定者 emaint_assignee)。
- `/start` 裸 / 未綁定者任何輸入 / 錯碼:回綁定教學(固定雙語,零資料)。
- `/new`(已綁定):關閉開啟中的 "Telegram" 對話,下則訊息惰性開新。
- 其他文字(已綁定):BackgroundTasks 打 Hermes gateway `/chat`(webhook 先回 200)。

治理:scoped token 顯式 300s(gateway 短票語意)、**絕不入 log / 回覆**;稽核 = human:<user>。
"""

from __future__ import annotations

import contextlib
import logging
import secrets

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from cmms.api.deps import get_session
from cmms.audit import Actor
from cmms.config import get_settings
from cmms.domain.assistant.service import AssistantError, AssistantService
from cmms.domain.identity.models import UserAccount
from cmms.domain.identity.service import IdentityError, IdentityService
from cmms.domain.notify.service import NotificationService
from cmms.domain.telegram_bridge.service import (
    TelegramBridgeError,
    TelegramBridgeService,
)
from cmms.telegram import (
    absolutize_app_links,
    get_telegram_sender,
    split_message,
)
from cmms.web.i18n import DEFAULT_LOCALE, translate

router = APIRouter(tags=["telegram"])
_logger = logging.getLogger("cmms.web.telegram")

# 對話標題:所有 Telegram 入口的提問共用一條開啟中對話(以此 title 尋回 / `/new` 關閉)。
_TG_CONV_TITLE = "Telegram"


def _ok() -> JSONResponse:
    """Telegram 期望的 2xx(否則重送);一切容錯忽略走此。"""
    return JSONResponse({"ok": True})


def _teach() -> str:
    """綁定教學(固定雙語 zh-TW+en;收訊者未綁定 → locale 未知,故不 per-user 翻譯)。"""
    return translate("tg.reply.teach", DEFAULT_LOCALE)


@router.post("/telegram/webhook", include_in_schema=False)
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    settings = get_settings()
    secret = settings.telegram_webhook_secret
    if not secret:  # fail-closed:任何環境未設 secret → 503(絕不裸奔收 update)
        return JSONResponse(
            {"detail": "telegram webhook not configured"}, status_code=503
        )
    provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not provided or not secrets.compare_digest(provided, secret):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    try:
        update = await request.json()
    except Exception:  # 非 JSON body → 忽略(200,不重送)
        return _ok()
    if not isinstance(update, dict):
        return _ok()

    message = update.get("message")
    if not isinstance(message, dict):
        return _ok()  # edited_message / callback_query / channel_post … 一律忽略
    chat = message.get("chat")
    text = message.get("text")
    if not isinstance(chat, dict) or chat.get("type") != "private":
        return _ok()  # 只吃 private chat(群組不處理)
    chat_id_raw = chat.get("id")
    if chat_id_raw is None or not isinstance(text, str) or not text.strip():
        return _ok()
    chat_id = str(chat_id_raw)

    bridge = TelegramBridgeService(session)

    # 冪等:Telegram 重送(同 update_id)→ skip(mark_update_seen 首次 True)
    update_id = update.get("update_id")
    if isinstance(update_id, int) and not await bridge.mark_update_seen(update_id):
        return _ok()

    try:
        await _dispatch(session, bridge, chat_id, text.strip(), background_tasks)
    except Exception as exc:  # 任何未預期 → 誠實 log、仍回 200(不讓 Telegram 重送打結)
        _logger.warning("telegram dispatch failed: %s: %s", type(exc).__name__, exc)
    return _ok()


async def _dispatch(
    session: AsyncSession,
    bridge: TelegramBridgeService,
    chat_id: str,
    text: str,
    background_tasks: BackgroundTasks,
) -> None:
    """指令分流。回覆用同步 sender.send(短訊息);提問掛 BackgroundTasks。"""
    sender = get_telegram_sender()
    user = await bridge.resolve_user_by_chat(chat_id)  # 已綁定者(否則 None)

    # /start [code]
    if text == "/start" or text.startswith("/start ") or text.startswith("/start@"):
        parts = text.split(maxsplit=1)
        code = parts[1].strip() if len(parts) > 1 else ""
        if not code:  # 裸 /start → 教學
            await sender.send(chat_id=chat_id, text=_teach())
            return
        await _handle_start_code(session, bridge, chat_id, code, sender)
        return

    # /new(已綁定才有意義)
    if text == "/new" or text.startswith("/new ") or text.startswith("/new@"):
        if user is None:
            await sender.send(chat_id=chat_id, text=_teach())
            return
        await _handle_new(session, chat_id, user, sender)
        return

    # 一般文字 = 提問
    if user is None:  # 未綁定 → 零資料,只給教學
        await sender.send(chat_id=chat_id, text=_teach())
        return
    background_tasks.add_task(_process_telegram_question, chat_id, user.user_id, text)


async def _handle_start_code(
    session: AsyncSession,
    bridge: TelegramBridgeService,
    chat_id: str,
    code: str,
    sender,
) -> None:
    """兌換綁定碼 → 綁定 + 回填通知 chat_id。失敗一律回統一教學(不洩漏碼狀態)。"""
    user_id = await bridge.peek_code_user(code)  # 決定 actor(=綁定者本人);不標已用
    if user_id is None:
        await sender.send(chat_id=chat_id, text=_teach())
        return
    actor = Actor.human(user_id)
    try:
        await bridge.redeem_code(code, chat_id, actor)  # 標已用 + upsert link(單一交易)
    except TelegramBridgeError:  # 過期 / 已用 / chat 撞他人 → 教學
        await sender.send(chat_id=chat_id, text=_teach())
        return
    user = await session.get(UserAccount, user_id)
    # 綁定成功即回填通知名單的 telegram_chat_id(空欄且 assignee_name 精確命中才填,有值不覆蓋)
    if user is not None and user.emaint_assignee:
        try:
            await NotificationService(session).fill_telegram_chat_id(
                assignee_name=user.emaint_assignee, chat_id=chat_id, actor=actor
            )
        except Exception as exc:  # 回填失敗不該擋綁定成功回覆
            _logger.warning("fill_telegram_chat_id failed: %s", type(exc).__name__)
    locale = user.ui_locale if user is not None else DEFAULT_LOCALE
    await sender.send(chat_id=chat_id, text=translate("tg.reply.bound", locale))


async def _handle_new(
    session: AsyncSession, chat_id: str, user: UserAccount, sender
) -> None:
    """關閉該 user 開啟中的 "Telegram" 對話(下則提問惰性開新)。"""
    asvc = AssistantService(session)
    actor = Actor.human(user.user_id)
    for conv in await asvc.list_open_conversations(user.user_id):
        if conv.title == _TG_CONV_TITLE:
            await asvc.close_conversation(user.user_id, conv.id, actor)
    await sender.send(
        chat_id=chat_id, text=translate("tg.reply.newchat", user.ui_locale)
    )


async def _process_telegram_question(chat_id: str, user_id: str, text: str) -> None:
    """背景:打 Hermes gateway `/chat` 取回覆、落 assistant 訊息、分段送回 Telegram。

    自建 session(比照 routes._flush_notify_outbox_bg);全程 try/except 兜底,例外只 log
    warning(**絕不含 scoped token**)並盡力送誠實錯誤訊息。gateway payload 與 dock phase-2 同形。
    """
    sender = get_telegram_sender()
    settings = get_settings()
    # 逾時對齊 dock phase-2(gateway 內 codex 工具往返可能久);單次不重試。
    from cmms.db import get_sessionmaker
    from cmms.web.routes import _ASSISTANT_TIMEOUT_SECONDS, _cap_history

    try:
        async with get_sessionmaker()() as session:
            user = await session.get(UserAccount, user_id)
            if user is None or not user.is_active:
                return  # 帳號中途停用 → 靜默(綁定者已不再有效)
            locale = user.ui_locale

            await sender.send_chat_action(chat_id=chat_id, action="typing")

            if not settings.hermes_configured:  # 助理未配置 → 誠實狀態
                await sender.send(
                    chat_id=chat_id, text=translate("tg.reply.disabled", locale)
                )
                return

            asvc = AssistantService(session)
            actor = Actor.human(user_id)
            # 找開啟中的 "Telegram" 對話;無則顯式建(取得 title="Telegram" 供尋回 / `/new`)。
            conv = next(
                (
                    c
                    for c in await asvc.list_open_conversations(user_id)
                    if c.title == _TG_CONV_TITLE
                ),
                None,
            )
            try:
                if conv is None:
                    conv = await asvc.create_conversation(user_id, _TG_CONV_TITLE, actor)
                _, umsg = await asvc.add_user_message(
                    user_id=user_id, conversation_id=conv.id, content=text, actor=actor
                )
            except AssistantError:  # 開啟中對話超上限 → 誠實提示(請先 /new 收掉)
                await sender.send(
                    chat_id=chat_id, text=translate("tg.reply.unavailable", locale)
                )
                return

            # gateway 短票:顯式 300s(維持短票語意)、per-user、可即時撤;**絕不入 log / 回覆**。
            try:
                token, _ = await IdentityService(session).mint_scoped_token_for_user(
                    username=user.username, agent="hermes", scope="pilot",
                    ttl_seconds=300,
                )
            except IdentityError:
                await sender.send(
                    chat_id=chat_id, text=translate("tg.reply.unavailable", locale)
                )
                return

            history = _cap_history(
                await asvc.recent_history(
                    user_id, conv.id, before_message_id=umsg.id
                )
            )
            payload = {
                "message": umsg.content,
                "scoped_token": token,  # ★ 只進 gateway body,永不回前端 / log
                "history": history,
                "locale": user.ui_locale,
                "jira_locale": user.jira_output_locale,
            }
            try:
                async with httpx.AsyncClient(
                    timeout=_ASSISTANT_TIMEOUT_SECONDS
                ) as client:
                    resp = await client.post(
                        settings.hermes_gateway_url.rstrip("/") + "/chat",
                        headers={"X-Hermes-Secret": settings.hermes_gateway_secret},
                        json=payload,
                    )
                resp.raise_for_status()
                reply = str((resp.json() or {}).get("reply") or "").strip()
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                # token 在 body、httpx 例外不回放 request body → log 不含 token
                _logger.warning(
                    "telegram gateway call failed: %s: %s", type(exc).__name__, exc
                )
                await sender.send(
                    chat_id=chat_id, text=translate("tg.reply.unavailable", locale)
                )
                return
            if not reply:  # gateway 回空 → 暫時無法回應(不落 DB)
                await sender.send(
                    chat_id=chat_id, text=translate("tg.reply.unavailable", locale)
                )
                return

            # 落 assistant 訊息(存原文 = SoR;會話競態關閉 → 不落但仍送)
            with contextlib.suppress(AssistantError):
                await asvc.add_assistant_message(
                    user_id=user_id, conversation_id=conv.id, content=reply, actor=actor
                )

            # 相對 /app 連結轉絕對(Telegram 無 base href)→ 分段 ≤4096 逐段送
            out = absolutize_app_links(reply, settings.public_base_url)
            for chunk in split_message(out):
                await sender.send(chat_id=chat_id, text=chunk)
    except Exception as exc:  # 最外層兜底:例外只 log(不含 token),盡力已在各分支送錯誤訊息
        _logger.warning(
            "telegram question processing failed: %s: %s", type(exc).__name__, exc
        )
