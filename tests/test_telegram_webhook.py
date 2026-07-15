"""Telegram 助理 webhook + 純函式測試(續-15;內部規格)。

不需 DB:純函式直測 split_message / absolutize_app_links;webhook 以 dependency override
(get_session)+ monkeypatch(get_settings / get_telegram_sender / domain service 方法 / httpx)
避開真 DB,只驗 secret 閘門 / 冪等 / 指令分流 / 背景提問 → gateway → 分段送出。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from cmms.api.app import app
from cmms.api.deps import get_session
from cmms.domain.telegram_bridge.service import TelegramBridgeError
from cmms.telegram import (
    InMemoryTelegramSender,
    absolutize_app_links,
    split_message,
)
from cmms.web import telegram_webhook as tw

client = TestClient(app)

_HDR = "X-Telegram-Bot-Api-Secret-Token"


# ---- 純函式:split_message ----

def test_split_short_single_segment():
    assert split_message("hello", limit=10) == ["hello"]
    assert split_message("", limit=10) == [""]          # 空 → 永不回空清單


def test_split_boundary_exact():
    assert split_message("abcdefghij", limit=10) == ["abcdefghij"]  # 恰 limit → 一段


def test_split_long_single_line_hard_split():
    assert split_message("abcdefghijk", limit=10) == ["abcdefghij", "k"]


def test_split_multiline_on_newline_boundary():
    assert split_message("aaa\nbbb\nccc", limit=7) == ["aaa\nbbb", "ccc"]


def test_split_default_limit_4096():
    text = "x" * 5000
    segs = split_message(text)
    assert len(segs) == 2 and segs[0] == "x" * 4096 and segs[1] == "x" * 904


# ---- 純函式:absolutize_app_links ----

def test_absolutize_standalone_path():
    out = absolutize_app_links("See /app/work-orders/WO-1 now", "https://x.dev")
    assert out == "See https://x.dev/app/work-orders/WO-1 now"


def test_absolutize_leaves_http_untouched():
    src = "visit https://y.dev/app/foo please"
    assert absolutize_app_links(src, "https://x.dev") == src   # URL 內 /app 不轉


def test_absolutize_multiple_and_cjk_lead():
    out = absolutize_app_links("看 /app/x 和/app/y", "https://x.dev/")
    assert out == "看 https://x.dev/app/x 和https://x.dev/app/y"


def test_absolutize_does_not_match_apple():
    assert absolutize_app_links("eat an /apple", "https://x.dev") == "eat an /apple"


def test_absolutize_empty_base_noop():
    assert absolutize_app_links("/app/x", "") == "/app/x"


# ---- webhook 固件 ----

def _settings(*, secret: str | None = "whsecret", hermes: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        telegram_webhook_secret=secret,
        telegram_bot_username="shopfloor_cmms_bot",
        hermes_configured=hermes,
        hermes_gateway_url="http://gw.internal:8080",
        hermes_gateway_secret="HS",
        public_base_url="https://cmms.example.com",
    )


def _user(**kw) -> SimpleNamespace:
    base = dict(
        user_id="alice", username="alice", display_name="Alice", is_active=True,
        ui_locale="en", jira_output_locale="en", emaint_assignee="Alice Wu",
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeSession:
    """webhook 同步半邊的 session:只需支援 async get(UserAccount)。"""

    def __init__(self, user=None):
        self.user = user

    async def get(self, model, key):
        return self.user


@pytest.fixture
def wh(monkeypatch):
    """webhook 測試環境:override get_session + monkeypatch settings/sender。回可調整 holder。"""
    sender = InMemoryTelegramSender()
    holder = {"user": None, "session_user": None, "settings": _settings()}

    async def _session():
        yield _FakeSession(holder["session_user"])

    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(tw, "get_settings", lambda: holder["settings"])
    monkeypatch.setattr(tw, "get_telegram_sender", lambda: sender)

    # 預設:mark_update_seen 首見 True;resolve 依 holder["user"]
    async def _seen(self, update_id, *, actor=None):
        return True

    async def _resolve(self, chat_id):
        return holder["user"]

    monkeypatch.setattr(tw.TelegramBridgeService, "mark_update_seen", _seen)
    monkeypatch.setattr(tw.TelegramBridgeService, "resolve_user_by_chat", _resolve)
    try:
        yield holder, sender, monkeypatch
    finally:
        app.dependency_overrides.pop(get_session, None)


def _update(text: str, *, update_id: int = 1, chat_type: str = "private", chat_id: int = 111):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id, "type": chat_type}, "text": text},
    }


def _post(body, *, secret: str | None = "whsecret"):
    headers = {_HDR: secret} if secret is not None else {}
    return client.post("/telegram/webhook", json=body, headers=headers)


# ---- webhook:secret 閘門 ----

def test_webhook_secret_unset_503(wh):
    holder, _sender, _mp = wh
    holder["settings"] = _settings(secret=None)
    r = _post(_update("/start"))
    assert r.status_code == 503


def test_webhook_wrong_secret_401(wh):
    r = _post(_update("/start"), secret="WRONG")
    assert r.status_code == 401


def test_webhook_missing_secret_header_401(wh):
    r = _post(_update("/start"), secret=None)
    assert r.status_code == 401


# ---- webhook:容錯忽略(永遠 200)----

def test_webhook_non_private_ignored(wh):
    _holder, sender, _mp = wh
    r = _post(_update("hi", chat_type="group"))
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert sender.sent == []                                  # 群組不處理


def test_webhook_no_text_ignored(wh):
    _holder, sender, _mp = wh
    r = _post({"update_id": 5, "message": {"chat": {"id": 1, "type": "private"}}})
    assert r.status_code == 200 and sender.sent == []


def test_webhook_duplicate_update_skipped(wh):
    holder, sender, mp = wh

    async def _seen_false(self, update_id, *, actor=None):
        return False                                          # 重送

    mp.setattr(tw.TelegramBridgeService, "mark_update_seen", _seen_false)
    r = _post(_update("/start"))
    assert r.status_code == 200 and sender.sent == []         # 已見過 → 不處理


# ---- webhook:/start ----

def test_webhook_start_bare_teaches(wh):
    _holder, sender, _mp = wh
    r = _post(_update("/start"))
    assert r.status_code == 200
    assert len(sender.sent) == 1 and "/start" in sender.sent[0]["text"]


def test_webhook_start_valid_code_binds_and_fills_notify(wh):
    holder, sender, mp = wh
    holder["session_user"] = _user(ui_locale="zh-TW")        # session.get(UserAccount) 回此
    filled = {}

    async def _peek(self, code):
        return "alice"

    async def _redeem(self, code, chat_id, actor):
        return SimpleNamespace(user_id="alice", chat_id=chat_id)

    async def _fill(self, *, assignee_name, chat_id, actor):
        filled["args"] = (assignee_name, chat_id)
        return True

    mp.setattr(tw.TelegramBridgeService, "peek_code_user", _peek)
    mp.setattr(tw.TelegramBridgeService, "redeem_code", _redeem)
    mp.setattr(tw.NotificationService, "fill_telegram_chat_id", _fill)
    r = _post(_update("/start ABC-123"))
    assert r.status_code == 200
    assert filled["args"] == ("Alice Wu", "111")             # 以 emaint_assignee 回填
    assert "綁定成功" in sender.sent[0]["text"]               # zh-TW 綁定成功回覆


def test_webhook_start_bad_code_teaches(wh):
    _holder, sender, mp = wh

    async def _peek(self, code):
        return None                                          # 碼無效

    mp.setattr(tw.TelegramBridgeService, "peek_code_user", _peek)
    r = _post(_update("/start NOPE"))
    assert r.status_code == 200 and "/start" in sender.sent[0]["text"]


def test_webhook_start_redeem_error_teaches(wh):
    holder, sender, mp = wh

    async def _peek(self, code):
        return "alice"

    async def _redeem(self, code, chat_id, actor):
        raise TelegramBridgeError("chat already linked to another account")

    mp.setattr(tw.TelegramBridgeService, "peek_code_user", _peek)
    mp.setattr(tw.TelegramBridgeService, "redeem_code", _redeem)
    r = _post(_update("/start ABC"))
    assert r.status_code == 200 and "/start" in sender.sent[0]["text"]  # 統一教學


# ---- webhook:未綁定提問 → 教學(零資料)----

def test_webhook_unbound_question_teaches(wh):
    _holder, sender, _mp = wh                                 # holder["user"] None = 未綁
    r = _post(_update("EID-70021 的工單"))
    assert r.status_code == 200 and "/start" in sender.sent[0]["text"]


# ---- webhook:已綁定提問 → 觸發背景 ----

def test_webhook_bound_question_schedules_background(wh):
    holder, _sender, mp = wh
    holder["user"] = _user()                                 # 已綁定
    calls = []

    async def _proc(chat_id, user_id, text):
        calls.append((chat_id, user_id, text))

    mp.setattr(tw, "_process_telegram_question", _proc)
    r = _post(_update("列出 EID-70017 的工單"))
    assert r.status_code == 200
    assert calls == [("111", "alice", "列出 EID-70017 的工單")]


# ---- webhook:/new 關對話 ----

def test_webhook_new_closes_telegram_conversation(wh):
    holder, sender, mp = wh
    holder["user"] = _user(ui_locale="en")
    closed = []

    async def _list_open(self, user_id):
        return [
            SimpleNamespace(id=7, title="Telegram"),
            SimpleNamespace(id=8, title="Other"),
        ]

    async def _close(self, user_id, conv_id, actor):
        closed.append(conv_id)

    mp.setattr(tw.AssistantService, "list_open_conversations", _list_open)
    mp.setattr(tw.AssistantService, "close_conversation", _close)
    r = _post(_update("/new"))
    assert r.status_code == 200
    assert closed == [7]                                     # 只關 title="Telegram"
    assert "new conversation" in sender.sent[0]["text"].lower()


def test_webhook_new_unbound_teaches(wh):
    _holder, sender, _mp = wh                                 # 未綁
    r = _post(_update("/new"))
    assert r.status_code == 200 and "/start" in sender.sent[0]["text"]


# ---- 背景提問處理:gateway mock → 分段送出 ----

class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._payload


class _FakeClient:
    resp = None
    captured = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.captured = {"url": url, "headers": headers, "json": json}
        return _FakeClient.resp


class _CtxSession:
    def __init__(self, user):
        self._user = user

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, model, key):
        return self._user


async def test_process_question_gateway_reply_segmented(monkeypatch):
    user = _user(user_id="alice", username="alice", ui_locale="zh-TW")
    sender = InMemoryTelegramSender()
    monkeypatch.setattr(tw, "get_telegram_sender", lambda: sender)
    monkeypatch.setattr(tw, "get_settings", lambda: _settings())

    monkeypatch.setattr("cmms.db.get_sessionmaker", lambda: (lambda: _CtxSession(user)))

    async def _list_open(self, user_id):
        return []

    async def _create(self, user_id, title, actor):
        return SimpleNamespace(id=1, title=title)

    async def _add_user(self, *, user_id, conversation_id, content, actor):
        return SimpleNamespace(id=1), SimpleNamespace(id=10, content=content)

    async def _hist(self, user_id, conv_id, max_turns=None, before_message_id=None):
        return []

    added = {}

    async def _add_asst(self, *, user_id, conversation_id, content, actor):
        added["content"] = content
        return SimpleNamespace(id=11)

    monkeypatch.setattr(tw.AssistantService, "list_open_conversations", _list_open)
    monkeypatch.setattr(tw.AssistantService, "create_conversation", _create)
    monkeypatch.setattr(tw.AssistantService, "add_user_message", _add_user)
    monkeypatch.setattr(tw.AssistantService, "recent_history", _hist)
    monkeypatch.setattr(tw.AssistantService, "add_assistant_message", _add_asst)

    async def _mint(self, *, username, agent, scope, ttl_seconds=None, at=None):
        assert ttl_seconds == 300                            # 顯式短票
        return "TOK", datetime.now(UTC)

    monkeypatch.setattr(tw.IdentityService, "mint_scoped_token_for_user", _mint)

    # gateway 回覆:含相對 /app 連結 + 一行超長 → 觸發分段
    reply = "/app/work-orders/WO-1\n" + ("y" * 5000)
    _FakeClient.resp = _FakeResp({"reply": reply})
    monkeypatch.setattr(tw.httpx, "AsyncClient", _FakeClient)

    await tw._process_telegram_question("111", "alice", "hello")

    # typing 提示送出
    assert sender.actions and sender.actions[0]["action"] == "typing"
    # 落 DB 的是原文(SoR;相對連結)
    assert added["content"] == reply
    # 送出:相對連結已絕對化 + 分段(≥2)
    assert len(sender.sent) >= 2
    assert sender.sent[0]["text"].startswith("https://cmms.example.com/app/work-orders/WO-1")
    # scoped token 只進 gateway body,不入任何送出訊息
    assert all("TOK" not in s["text"] for s in sender.sent)
    assert _FakeClient.captured["json"]["scoped_token"] == "TOK"


async def test_process_question_hermes_disabled(monkeypatch):
    user = _user(ui_locale="en")
    sender = InMemoryTelegramSender()
    monkeypatch.setattr(tw, "get_telegram_sender", lambda: sender)
    monkeypatch.setattr(tw, "get_settings", lambda: _settings(hermes=False))
    monkeypatch.setattr("cmms.db.get_sessionmaker", lambda: (lambda: _CtxSession(user)))
    await tw._process_telegram_question("111", "alice", "hi")
    assert len(sender.sent) == 1 and "enabled" in sender.sent[0]["text"].lower()


async def test_process_question_gateway_empty_reply_unavailable(monkeypatch):
    user = _user(ui_locale="en")
    sender = InMemoryTelegramSender()
    monkeypatch.setattr(tw, "get_telegram_sender", lambda: sender)
    monkeypatch.setattr(tw, "get_settings", lambda: _settings())
    monkeypatch.setattr("cmms.db.get_sessionmaker", lambda: (lambda: _CtxSession(user)))

    async def _list_open(self, user_id):
        return [SimpleNamespace(id=1, title="Telegram")]

    async def _add_user(self, *, user_id, conversation_id, content, actor):
        return SimpleNamespace(id=1), SimpleNamespace(id=10, content=content)

    async def _hist(self, user_id, conv_id, max_turns=None, before_message_id=None):
        return []

    async def _mint(self, *, username, agent, scope, ttl_seconds=None, at=None):
        return "TOK", datetime.now(UTC)

    monkeypatch.setattr(tw.AssistantService, "list_open_conversations", _list_open)
    monkeypatch.setattr(tw.AssistantService, "add_user_message", _add_user)
    monkeypatch.setattr(tw.AssistantService, "recent_history", _hist)
    monkeypatch.setattr(tw.IdentityService, "mint_scoped_token_for_user", _mint)
    _FakeClient.resp = _FakeResp({"reply": ""})              # gateway 回空
    monkeypatch.setattr(tw.httpx, "AsyncClient", _FakeClient)

    await tw._process_telegram_question("111", "alice", "hi")
    assert len(sender.sent) == 1                              # 只送一則「暫時無法回應」
