"""Hermes gateway 的 Anthropic 後端單元測試(零網路:假客戶端、假 MCP 工具、暫存帳本)。

gateway / llm_anthropic / ledger 是獨立小服務(不在 src/cmms 套件內),以檔案路徑載入。

驗證面:
- 組裝:persona 為 system 第一塊並掛快取標記、每請求提示在其後;messages[] 結構;模型路由;
  Sonnet / Opus 帶 server-side fallback、Haiku 不帶。
- 計價:Haiku 一般層 / 長 prompt 層、Sonnet 快取讀價、未知模型以最貴價估。
- 帳本:日 / 月預算 80% 警告、100% 熔斷;每來源每日問題上限與跨日歸零;來源鍵不落原值。
- 錯誤對應:金鑰 / 權限 / 模型錯 → 503 類;429 / 5xx / 400 / 連線 / 逾時 → 502 類。
- chat():usage 跨多次呼叫加總、拒答回固定句、工具迴圈超限 → 502 類且已花費仍帶出。
- 端點(HERMES_BACKEND=anthropic):/health 預算欄、/chat 成功記帳、429 帶 Retry-After、
  預算用罄 503、金鑰未設 503、後端錯誤 502、/mrq-description 成功;token 與金鑰不落 log。
- 多產品(target = cmms / board):MCP URL 只取伺服端對照表、board persona(檔案覆寫 / 隨附預設)、
  X-Board-Turn 淨化、/health 的 mcp_targets 與逐 target 日預算、舊帳本就地補 target 欄、
  日預算與題數逐 target 分桶、月預算共用、閘門順序(未設定的 target 不計題)。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import logging
import pathlib
import socket
import sys
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

_APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "infra" / "hermes" / "app"


def _load(name: str, key: str):
    spec = importlib.util.spec_from_file_location(key, _APP_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


ledger = _load("ledger", "hermes_ledger")
llm = _load("llm_anthropic", "hermes_llm_anthropic")
gw = _load("gateway", "hermes_gateway_anthropic")

SECRET = "test-secret"
TOKEN = "scoped-token-SHOULD-NOT-LEAK"
API_KEY = "sk-ant-test-SHOULD-NOT-LEAK"


# ── 假物件 ───────────────────────────────────────────────────────────────────
def _msg(text="", stop="end_turn", model="claude-haiku-5-5", inp=100, out=20, cr=0, cw=0):
    content = [SimpleNamespace(type="text", text=text)] if text else []
    usage = SimpleNamespace(
        input_tokens=inp, output_tokens=out,
        cache_read_input_tokens=cr, cache_creation_input_tokens=cw,
    )
    return SimpleNamespace(content=content, stop_reason=stop, model=model, usage=usage)


class _Runner:
    def __init__(self, messages, exc=None):
        self._messages = list(messages)
        self._exc = exc

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._messages:
            yield m
        if self._exc is not None:
            raise self._exc


class FakeClient:
    """記錄 tool_runner / messages.create 收到的參數;回預先排好的訊息。"""

    def __init__(self, messages=(), exc=None, text_message=None, text_exc=None):
        self.runner_kwargs = None
        self.create_kwargs = None
        outer = self

        def tool_runner(**kwargs):
            outer.runner_kwargs = kwargs
            return _Runner(messages, exc)

        async def create(**kwargs):
            outer.create_kwargs = kwargs
            if text_exc is not None:
                raise text_exc
            return text_message

        self.beta = SimpleNamespace(messages=SimpleNamespace(tool_runner=tool_runner))
        self.messages = SimpleNamespace(create=create)


@contextlib.asynccontextmanager
async def fake_tools(mcp_url, scoped_token):
    yield ["tool-a", "tool-b"]


class RecordingTools:
    """假 MCP opener:記錄每次開啟的 (mcp_url, extra_headers);只收兩個位置參數時記 None。"""

    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []

    @contextlib.asynccontextmanager
    async def __call__(self, mcp_url, scoped_token, **kwargs):
        self.calls.append((mcp_url, kwargs.get("extra_headers", "ABSENT")))
        yield ["tool-a"]


def _resp(status, headers=None):
    return httpx2.Response(status, headers=headers or {}, request=httpx2.Request("POST", "https://x"))


class Clock:
    def __init__(self, when):
        self.when = when

    def __call__(self):
        return self.when


# ── 組裝 ─────────────────────────────────────────────────────────────────────
def test_build_system_caches_persona_and_keeps_extras_after():
    blocks = llm.build_system("PERSONA", ["lang: zh-TW", ""])
    assert blocks[0] == {"type": "text", "text": "PERSONA", "cache_control": {"type": "ephemeral"}}
    assert blocks[1] == {"type": "text", "text": "lang: zh-TW"}
    assert "cache_control" not in blocks[1]
    assert llm.build_system("P", []) == [blocks[0] | {"text": "P"}]


def test_build_messages_real_turns_and_leading_assistant_dropped():
    msgs = llm.build_messages(
        [("assistant", "hi"), ("user", "q1"), ("assistant", "a1"), ("user", "")], "q2"
    )
    assert msgs == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]


def test_model_routing_and_env_override(monkeypatch):
    monkeypatch.delenv("HERMES_MODEL_CHAT", raising=False)
    monkeypatch.delenv("HERMES_MODEL_DEEP", raising=False)
    assert llm.pick_chat_model("open orders?", None) == "claude-haiku-5-5"
    assert llm.pick_chat_model("open orders?", "deep") == "claude-sonnet-5-5"
    assert llm.pick_chat_model("請詳細分析這台機台", None) == "claude-sonnet-5-5"
    monkeypatch.setenv("HERMES_MODEL_CHAT", "claude-sonnet-5-5")
    assert llm.pick_chat_model("hi", None) == "claude-sonnet-5-5"
    assert "opus" not in llm.DEFAULT_MODEL_CHAT + llm.DEFAULT_MODEL_TEXT + llm.DEFAULT_MODEL_DEEP


def test_fallback_only_for_sonnet_and_opus():
    assert llm.fallback_kwargs("claude-haiku-5-5") == {}
    kw = llm.fallback_kwargs("claude-sonnet-5-5")
    assert kw == {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}


def test_make_client_reads_only_anthropic_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(llm.LLMConfigError):
        llm.make_client()
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    client = llm.make_client()
    assert client.api_key == API_KEY and client.max_retries == 2


# ── 計價 ─────────────────────────────────────────────────────────────────────
def test_estimate_usd_haiku_tiers_and_sonnet_cache_read():
    # Haiku 一般層(prompt 剛好 100K,不超過門檻):100K in + 1M out = 0.01 + 0.50
    assert ledger.estimate_usd("claude-haiku-5-5", 100_000, 1_000_000, 0, 0) == pytest.approx(0.51)
    # prompt(input + 快取讀 + 快取寫)> 100K → 整筆改長層價
    long_usd = ledger.estimate_usd("claude-haiku-5-5", 60_000, 1_000, 50_000, 0)
    assert long_usd == pytest.approx((60_000 * 0.50 + 1_000 * 2.50 + 50_000 * 0.05) / 1e6)
    # Sonnet 5.5 快取讀 0.20 / 快取寫 2.50
    assert ledger.estimate_usd("claude-sonnet-5-5", 0, 0, 1_000_000, 0) == pytest.approx(0.20)
    assert ledger.estimate_usd("claude-sonnet-5-5", 0, 0, 0, 1_000_000) == pytest.approx(2.50)
    # 未知模型以最貴價估(寧可高估)
    assert ledger.estimate_usd("some-other-model", 0, 1_000_000, 0, 0) == pytest.approx(20.0)


# ── 帳本 ─────────────────────────────────────────────────────────────────────
def _book(tmp_path, *, daily=0.5, monthly=10.0, clock=None):
    clock = clock or Clock(dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.UTC))
    return ledger.Ledger(str(tmp_path / "l.sqlite"), daily_budget_usd=daily,
                         monthly_budget_usd=monthly, now=clock), clock


def _spend(book, usd):
    book.record(endpoint="chat", model="claude-haiku-5-5", input_tokens=1, output_tokens=1,
                cache_read=0, cache_write=0, usd=usd)


def test_budget_warning_and_exhausted_daily(tmp_path):
    book, _ = _book(tmp_path, daily=1.0)
    _spend(book, 0.79)
    assert book.budget_state()["daily"]["cmms"]["state"] == "ok"
    _spend(book, 0.02)
    assert book.budget_state()["daily"]["cmms"]["state"] == "warning"
    book.check_budget()  # warning 仍放行
    _spend(book, 0.19)
    assert book.budget_state()["daily"]["cmms"]["state"] == "exhausted"
    with pytest.raises(ledger.BudgetExhausted) as exc:
        book.check_budget()
    assert exc.value.scope == "daily"


def test_budget_daily_resets_next_day_monthly_persists(tmp_path):
    book, clock = _book(tmp_path, daily=0.5, monthly=0.6)
    _spend(book, 0.5)
    with pytest.raises(ledger.BudgetExhausted):
        book.check_budget()
    clock.when = clock.when + dt.timedelta(days=1)
    book.check_budget()  # 隔日日預算歸零
    _spend(book, 0.1)
    with pytest.raises(ledger.BudgetExhausted) as exc:
        book.check_budget()
    assert exc.value.scope == "monthly"  # 月度累計 0.6 用罄,月度優先回報


def test_question_limit_per_client_and_rollover(tmp_path):
    book, clock = _book(tmp_path)
    a = ledger.client_hash("203.0.113.7", SECRET)
    b = ledger.client_hash("198.51.100.9", SECRET)
    assert "203.0.113.7" not in a and len(a) == 24
    assert [book.take_question(a, 2) for _ in range(3)] == [True, True, False]
    assert book.take_question(b, 2) is True  # 別的來源不受影響
    assert book.take_question(a, 0) is True  # 0 = 不限
    clock.when = clock.when + dt.timedelta(days=1)
    assert book.take_question(a, 2) is True  # 跨 UTC 日歸零
    assert book.seconds_until_utc_midnight() == 12 * 3600


# ── 錯誤對應 ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (anthropic.AuthenticationError("x", response=_resp(401), body=None), "config"),
        (anthropic.PermissionDeniedError("x", response=_resp(403), body=None), "config"),
        (anthropic.NotFoundError("x", response=_resp(404), body=None), "config"),
        (anthropic.BadRequestError("x", response=_resp(400), body=None), "backend"),
        (anthropic.InternalServerError("x", response=_resp(500), body=None), "backend"),
        (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")), "backend"),
        (anthropic.APITimeoutError(request=httpx2.Request("POST", "https://x")), "backend"),
    ],
)
def test_translate_error(exc, kind):
    err = llm.translate_error(exc)
    assert isinstance(err, llm.LLMConfigError if kind == "config" else llm.LLMBackendError)


def test_translate_rate_limit_keeps_retry_after():
    exc = anthropic.RateLimitError("x", response=_resp(429, {"retry-after": "7"}), body=None)
    err = llm.translate_error(exc)
    assert isinstance(err, llm.LLMBackendError) and err.retry_after == "7"


# ── chat() / complete_text() ─────────────────────────────────────────────────
async def test_chat_sums_usage_and_passes_runner_params(monkeypatch):
    monkeypatch.delenv("HERMES_MODEL_CHAT", raising=False)
    client = FakeClient([_msg(stop="tool_use", inp=500, out=40, cw=600),
                         _msg("3 open work orders.", inp=50, out=30, cr=600)])
    result = await llm.chat(client, persona="P", extra_lines=["lang"], history=[("user", "q0")],
                            message="q1", model="claude-haiku-5-5", mcp_url="http://app/mcp",
                            scoped_token=TOKEN, locale="en", tools_opener=fake_tools)
    assert result.text == "3 open work orders." and not result.refused
    u = result.usage
    assert (u.api_calls, u.input_tokens, u.output_tokens, u.cache_read, u.cache_write) == (
        2, 550, 70, 600, 600)
    assert u.usd > 0
    kw = client.runner_kwargs
    assert kw["tools"] == ["tool-a", "tool-b"]
    assert kw["max_iterations"] == 8 and kw["output_config"] == {"effort": "low"}
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["messages"][-1] == {"role": "user", "content": "q1"}
    assert "fallbacks" not in kw  # Haiku 無 server-side fallback


async def test_chat_deep_model_gets_fallback_and_medium_effort(monkeypatch):
    monkeypatch.delenv("HERMES_MODEL_CHAT", raising=False)
    client = FakeClient([_msg("ok", model="claude-sonnet-5-5")])
    await llm.chat(client, persona="P", extra_lines=[], history=[], message="q",
                   model="claude-sonnet-5-5", mcp_url="u", scoped_token=TOKEN, locale=None,
                   tools_opener=fake_tools)
    kw = client.runner_kwargs
    assert kw["fallbacks"] == "default" and kw["output_config"] == {"effort": "medium"}


async def test_chat_refusal_returns_fixed_sentence():
    client = FakeClient([_msg(stop="refusal")])
    result = await llm.chat(client, persona="P", extra_lines=[], history=[], message="q",
                            model="claude-haiku-5-5", mcp_url="u", scoped_token=TOKEN,
                            locale="zh-TW", tools_opener=fake_tools)
    assert result.refused and result.text == llm.REFUSAL_REPLIES["zh-TW"]
    assert result.usage.api_calls == 1  # 拒答也記帳


async def test_chat_tool_loop_limit_is_backend_error_with_usage():
    client = FakeClient([_msg(stop="tool_use")] * 8)
    with pytest.raises(llm.LLMBackendError) as exc:
        await llm.chat(client, persona="P", extra_lines=[], history=[], message="q",
                       model="claude-haiku-5-5", mcp_url="u", scoped_token=TOKEN, locale=None,
                       tools_opener=fake_tools)
    assert exc.value.usage.api_calls == 8


async def test_chat_api_error_translated_and_usage_kept():
    rate = anthropic.RateLimitError("x", response=_resp(429, {"retry-after": "3"}), body=None)
    client = FakeClient([_msg(stop="tool_use")], exc=rate)
    with pytest.raises(llm.LLMBackendError) as exc:
        await llm.chat(client, persona="P", extra_lines=[], history=[], message="q",
                       model="claude-haiku-5-5", mcp_url="u", scoped_token=TOKEN, locale=None,
                       tools_opener=fake_tools)
    assert exc.value.retry_after == "3" and exc.value.usage.api_calls == 1


async def test_complete_text_and_refusal():
    client = FakeClient(text_message=_msg("Belt replaced; line back up."))
    result = await llm.complete_text(client, persona="MRQ", prompt="body", model="claude-haiku-5-5")
    assert result.text == "Belt replaced; line back up."
    assert client.create_kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert client.create_kwargs["output_config"] == {"effort": "medium"}
    with pytest.raises(llm.LLMBackendError):
        await llm.complete_text(FakeClient(text_message=_msg(stop="refusal")), persona="MRQ",
                                prompt="body", model="claude-haiku-5-5")


# ── 端點(HERMES_BACKEND=anthropic)───────────────────────────────────────────
@pytest.fixture
def anth(monkeypatch, tmp_path):
    """anthropic 後端 + 暫存帳本;回 (TestClient, setter) —— setter 指定假客戶端。"""
    monkeypatch.setenv("HERMES_BACKEND", "anthropic")
    monkeypatch.setenv("HERMES_GATEWAY_SECRET", SECRET)
    monkeypatch.setenv("HERMES_LEDGER_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setenv("HERMES_DAILY_BUDGET_USD", "0.5")
    monkeypatch.setenv("HERMES_MONTHLY_BUDGET_USD", "10")
    monkeypatch.setenv("HERMES_DAILY_QUESTIONS_PER_CLIENT", "2")
    for name in ("CMMS_MCP_URL", "BOARD_MCP_URL", "HERMES_PERSONA_BOARD_FILE",
                 "HERMES_DAILY_BUDGET_USD_CMMS", "HERMES_DAILY_BUDGET_USD_BOARD",
                 "HERMES_DAILY_QUESTIONS_PER_CLIENT_BOARD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gw, "_board_persona_text", None)
    monkeypatch.setattr(gw, "_ledger_instance", None)
    monkeypatch.setattr(gw, "_client_instance", None)
    monkeypatch.setattr(gw, "_semaphore", None)
    mod = gw._sibling("llm_anthropic")
    monkeypatch.setattr(mod, "open_mcp_tools", fake_tools)

    def use(fake):
        monkeypatch.setattr(gw, "_client_instance", fake)

    return TestClient(gw.app), use


def _chat(client, **extra):
    body = {"message": "how many open orders?", "scoped_token": TOKEN, "client_id": "203.0.113.7"}
    body.update(extra)
    return client.post("/chat", headers={"X-Hermes-Secret": SECRET}, json=body)


def test_health_reports_backend_and_budgets(anth):
    client, _ = anth
    body = client.get("/health").json()
    assert body["backend"] == "anthropic"
    assert body["budget"]["daily"] == {
        "cmms": {"spent_usd": 0, "limit_usd": 0.5, "state": "ok"},
        "board": {"spent_usd": 0, "limit_usd": 0.5, "state": "ok"},
    }
    assert body["budget"]["monthly"] == {"spent_usd": 0, "limit_usd": 10.0, "state": "ok"}


def test_chat_success_records_spend(anth, caplog):
    client, use = anth
    use(FakeClient([_msg("Two open orders.", inp=1000, out=200)]))
    with caplog.at_level(logging.INFO):
        resp = _chat(client)
    assert resp.status_code == 200 and resp.json() == {"reply": "Two open orders."}
    spent = client.get("/health").json()["budget"]["daily"]["cmms"]["spent_usd"]
    assert spent == pytest.approx((1000 * 0.10 + 200 * 0.50) / 1e6)
    assert TOKEN not in caplog.text and API_KEY not in caplog.text
    assert "how many open orders" not in caplog.text  # 不 log 提問內文


def test_chat_question_limit_429_per_client(anth):
    client, use = anth
    use(FakeClient([_msg("ok")]))
    assert _chat(client).status_code == 200
    use(FakeClient([_msg("ok")]))
    assert _chat(client).status_code == 200
    resp = _chat(client)
    assert resp.status_code == 429 and int(resp.headers["retry-after"]) > 0
    use(FakeClient([_msg("ok")]))
    assert _chat(client, client_id="198.51.100.9").status_code == 200


def test_chat_budget_exhausted_503(anth):
    client, use = anth
    gw._ledger().record(endpoint="chat", model="claude-haiku-5-5", input_tokens=0,
                        output_tokens=0, cache_read=0, cache_write=0, usd=0.5)
    use(FakeClient([_msg("never")]))
    resp = _chat(client)
    assert resp.status_code == 503 and resp.json() == {"detail": "daily budget exhausted"}
    assert client.get("/health").json()["budget"]["daily"]["cmms"]["state"] == "exhausted"


def test_chat_missing_key_503(anth, monkeypatch):
    client, _ = anth
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resp = _chat(client)
    assert resp.status_code == 503 and "not configured" in resp.json()["detail"]


def test_chat_backend_error_502_with_retry_after(anth):
    client, use = anth
    rate = anthropic.RateLimitError("x", response=_resp(429, {"retry-after": "9"}), body=None)
    use(FakeClient([], exc=rate))
    resp = _chat(client)
    assert resp.status_code == 502 and resp.headers["retry-after"] == "9"


def test_chat_wrong_secret_401_before_any_spend(anth):
    client, use = anth
    fake = FakeClient([_msg("x")])
    use(fake)
    resp = client.post("/chat", headers={"X-Hermes-Secret": "nope"},
                       json={"message": "q", "scoped_token": TOKEN})
    assert resp.status_code == 401 and fake.runner_kwargs is None


def test_mrq_description_anthropic(anth):
    client, use = anth
    fake = FakeClient(text_message=_msg("Belt replaced; output verified."))
    use(fake)
    resp = client.post("/mrq-description", headers={"X-Hermes-Secret": SECRET},
                       json={"work_orders": [{"eid": "EID-97001", "name": "Aligner-01",
                                              "notes": [{"body": "belt worn"}]}],
                             "jira_locale": "English"})
    assert resp.status_code == 200
    assert resp.json() == {"description": "Belt replaced; output verified."}
    sys_blocks = fake.create_kwargs["system"]
    assert sys_blocks[0]["text"] == gw.MRQ_DESCRIPTION_PERSONA  # persona 在 system、不在 prompt
    assert gw.MRQ_DESCRIPTION_PERSONA not in fake.create_kwargs["messages"][0]["content"]


# ── 多產品:target = cmms / board ─────────────────────────────────────────────
BOARD_URL = "http://board.internal:8080/mcp"
CMMS_URL = "http://app:8080/mcp"
_PACKAGED_BOARD_PERSONA = _APP_DIR.parent / "personas" / "board.md"


@pytest.fixture
def tools(monkeypatch):
    """以 RecordingTools 取代 open_mcp_tools,回記錄器。"""
    rec = RecordingTools()
    monkeypatch.setattr(gw._sibling("llm_anthropic"), "open_mcp_tools", rec)
    return rec


def _questions_rows(tmp_path):
    import sqlite3

    gw._ledger()  # 確保帳本已建表(被拒的請求可能根本沒開帳本)
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    try:
        return conn.execute("SELECT client, n FROM questions").fetchall()
    finally:
        conn.close()


def _spend_target(usd, target):
    gw._ledger().record(endpoint="chat", model="claude-haiku-5-5", input_tokens=0,
                        output_tokens=0, cache_read=0, cache_write=0, usd=usd, target=target)


def test_default_target_is_cmms_url_and_system_persona(anth, tools, monkeypatch):
    client, use = anth
    monkeypatch.setenv("CMMS_MCP_URL", CMMS_URL)
    monkeypatch.setenv("BOARD_MCP_URL", BOARD_URL)
    fake = FakeClient([_msg("ok")])
    use(fake)
    resp = _chat(client, locale="zh-TW", jira_locale="vi")  # 不帶 target
    assert resp.status_code == 200
    assert tools.calls == [(CMMS_URL, "ABSENT")]  # opener 只收兩個位置參數(與改動前相同)
    system = fake.runner_kwargs["system"]
    assert system[0]["text"] == gw.SYSTEM_PERSONA
    assert system[1]["text"] == "\n".join(gw.persona_extras("zh-TW", "vi"))


def test_cmms_default_url_kept_when_env_unset(anth, tools):
    client, use = anth
    use(FakeClient([_msg("ok")]))
    assert _chat(client).status_code == 200
    assert tools.calls == [(gw._DEFAULT_MCP_URL, "ABSENT")]


def test_board_url_unset_503_before_llm_and_no_question_counted(anth, tools, tmp_path):
    client, use = anth
    fake = FakeClient([_msg("never")])
    use(fake)
    resp = _chat(client, target="board")
    assert resp.status_code == 503
    assert resp.json() == {"detail": "mcp target not configured: board"}
    assert fake.runner_kwargs is None and tools.calls == []
    assert _questions_rows(tmp_path) == []  # 未計題


def test_board_uses_board_url_and_packaged_persona(anth, tools, monkeypatch):
    client, use = anth
    monkeypatch.setenv("CMMS_MCP_URL", CMMS_URL)
    monkeypatch.setenv("BOARD_MCP_URL", BOARD_URL)
    fake = FakeClient([_msg("M-01 is running.")])
    use(fake)
    # request 夾帶 URL 欄位也不會被採用(MCP URL 只取伺服端對照表)
    resp = _chat(client, target="board", locale="en", jira_locale="vi",
                 mcp_url="http://evil.example/mcp", url="http://evil.example/mcp")
    assert resp.status_code == 200 and resp.json() == {"reply": "M-01 is running."}
    assert [u for u, _ in tools.calls] == [BOARD_URL]
    system = fake.runner_kwargs["system"]
    packaged = _PACKAGED_BOARD_PERSONA.read_text(encoding="utf-8").strip()
    assert system[0]["text"] == packaged and system[0]["text"] != gw.SYSTEM_PERSONA
    assert system[1]["text"] == "The user's preferred reply language is: en."
    assert "Jira" not in system[1]["text"]  # board 無 Jira 提示


def _board_layout_setup(anth, monkeypatch):
    client, use = anth
    monkeypatch.setenv("CMMS_MCP_URL", CMMS_URL)
    monkeypatch.setenv("BOARD_MCP_URL", BOARD_URL)
    fake = FakeClient([_msg("ok")])
    use(fake)
    return client, fake


def test_board_layout_wide_is_last_system_block_persona_still_cached(anth, tools, monkeypatch):
    client, fake = _board_layout_setup(anth, monkeypatch)
    assert _chat(client, target="board", layout="wide").status_code == 200
    system = fake.runner_kwargs["system"]
    assert system[-1]["text"] == "Reply layout: wide"
    assert "cache_control" in system[0]


def test_board_without_layout_has_no_layout_block(anth, tools, monkeypatch):
    client, fake = _board_layout_setup(anth, monkeypatch)
    assert _chat(client, target="board", locale="en").status_code == 200
    assert not any("Reply layout" in b["text"] for b in fake.runner_kwargs["system"])


def test_cmms_ignores_layout(anth, tools, monkeypatch):
    client, fake = _board_layout_setup(anth, monkeypatch)
    assert _chat(client, layout="narrow").status_code == 200
    assert not any("Reply layout" in b["text"] for b in fake.runner_kwargs["system"])


def test_invalid_layout_422(anth):
    client, _ = anth
    assert _chat(client, target="board", layout="huge").status_code == 422


def test_persona_extras_layout_only():
    assert gw.persona_extras(None, None, layout="narrow") == ["Reply layout: narrow"]


def test_chat_request_has_no_url_field():
    assert not [f for f in gw.ChatRequest.model_fields if "url" in f.lower()]


def test_board_persona_file_override_read_once(anth, tools, monkeypatch, tmp_path):
    client, use = anth
    monkeypatch.setenv("BOARD_MCP_URL", BOARD_URL)
    persona_file = tmp_path / "board-persona.md"
    persona_file.write_text("CUSTOM BOARD PERSONA\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PERSONA_BOARD_FILE", str(persona_file))
    fake = FakeClient([_msg("ok")])
    use(fake)
    assert _chat(client, target="board").status_code == 200
    assert fake.runner_kwargs["system"][0]["text"] == "CUSTOM BOARD PERSONA"
    persona_file.write_text("CHANGED", encoding="utf-8")  # 已快取:不再讀檔
    assert _chat(client, target="board").status_code == 200
    assert fake.runner_kwargs["system"][0]["text"] == "CUSTOM BOARD PERSONA"


def test_board_persona_missing_503_no_question_counted(anth, tools, monkeypatch, tmp_path):
    client, use = anth
    monkeypatch.setenv("BOARD_MCP_URL", BOARD_URL)
    monkeypatch.setenv("HERMES_PERSONA_BOARD_FILE", str(tmp_path / "missing.md"))
    fake = FakeClient([_msg("never")])
    use(fake)
    resp = _chat(client, target="board")
    assert resp.status_code == 503 and resp.json() == {"detail": "persona not available: board"}
    assert fake.runner_kwargs is None and _questions_rows(tmp_path) == []


def test_packaged_board_persona_is_final_and_public_safe():
    """定稿(2026-10-31)取代佔位稿:非空、宣告唯讀與合成資料、不提 gateway 內部名。"""
    text = _PACKAGED_BOARD_PERSONA.read_text(encoding="utf-8")
    assert len(text.splitlines()) >= 50
    lowered = text.lower()
    assert "read-only" in lowered and "synthetic" in lowered
    assert "hermes" not in lowered
    assert _PACKAGED_BOARD_PERSONA.resolve() == gw._BOARD_PERSONA_DEFAULT_PATH


def test_invalid_target_422(anth):
    client, use = anth
    fake = FakeClient([_msg("never")])
    use(fake)
    assert _chat(client, target="erp").status_code == 422
    assert fake.runner_kwargs is None


@pytest.mark.parametrize(
    ("target", "turn_id", "expected"),
    [
        ("board", "turn-42", {"X-Board-Turn": "turn-42"}),
        ("board", "  turn-42  ", {"X-Board-Turn": "turn-42"}),
        ("board", "t" * 128, {"X-Board-Turn": "t" * 128}),
        ("cmms", "turn-42", "ABSENT"),  # cmms 不送
        ("board", None, "ABSENT"),
        ("board", "", "ABSENT"),
        ("board", "   ", "ABSENT"),
        ("board", "t" * 129, "ABSENT"),  # 過長 → 整個不送
        ("board", "tür-1", "ABSENT"),  # 非 ASCII
        ("board", "a\nX-Evil: 1", "ABSENT"),  # 控制字元(header 注入)
    ],
)
def test_board_turn_header(anth, tools, monkeypatch, target, turn_id, expected):
    client, use = anth
    monkeypatch.setenv("BOARD_MCP_URL", BOARD_URL)
    monkeypatch.setenv("HERMES_DAILY_QUESTIONS_PER_CLIENT_BOARD", "0")
    use(FakeClient([_msg("ok")]))
    extra = {"target": target}
    if turn_id is not None:
        extra["turn_id"] = turn_id
    assert _chat(client, **extra).status_code == 200
    assert tools.calls[-1][1] == expected


async def test_open_mcp_tools_sends_extra_headers_and_keeps_bearer(monkeypatch):
    """零網路:攔截 create_mcp_http_client,只驗證送出的 header。"""
    from mcp.shared import _httpx_utils

    seen = {}

    class _Stop(Exception):
        pass

    def fake_http_client(headers=None, **_):
        seen["headers"] = headers
        raise _Stop

    monkeypatch.setattr(_httpx_utils, "create_mcp_http_client", fake_http_client)
    with pytest.raises(_Stop):
        async with llm.open_mcp_tools(BOARD_URL, TOKEN,
                                      extra_headers={"X-Board-Turn": "t1",
                                                     "Authorization": "Bearer forged"}):
            pass
    assert seen["headers"] == {"X-Board-Turn": "t1", "Authorization": f"Bearer {TOKEN}"}
    with pytest.raises(_Stop):
        async with llm.open_mcp_tools(CMMS_URL, TOKEN):
            pass
    assert seen["headers"] == {"Authorization": f"Bearer {TOKEN}"}


async def test_chat_passes_extra_headers_to_opener_only_when_given():
    rec = RecordingTools()
    client = FakeClient([_msg("ok")])
    await llm.chat(client, persona="P", extra_lines=[], history=[], message="q",
                   model="claude-haiku-5-5", mcp_url="u", scoped_token=TOKEN, locale=None,
                   tools_opener=rec)
    await llm.chat(client, persona="P", extra_lines=[], history=[], message="q",
                   model="claude-haiku-5-5", mcp_url="u", scoped_token=TOKEN, locale=None,
                   tools_opener=rec, extra_headers={"X-Board-Turn": "t"})
    assert rec.calls == [("u", "ABSENT"), ("u", {"X-Board-Turn": "t"})]


def test_health_mcp_targets(anth, monkeypatch):
    client, _ = anth
    monkeypatch.setattr(gw, "_tcp_reachable", lambda url, timeout=1.0: url == CMMS_URL)
    body = client.get("/health").json()
    assert body["mcp_targets"] == {
        "cmms": {"configured": False, "url": None, "reachable": None},
        "board": {"configured": False, "url": None, "reachable": None},
    }
    monkeypatch.setenv("CMMS_MCP_URL", CMMS_URL)
    monkeypatch.setenv("BOARD_MCP_URL", BOARD_URL)
    body = client.get("/health").json()
    assert body["mcp_targets"] == {
        "cmms": {"configured": True, "url": CMMS_URL, "reachable": True},
        "board": {"configured": True, "url": BOARD_URL, "reachable": False},
    }
    monkeypatch.setenv("BOARD_MCP_URL", "")  # 空字串 = 未設定
    assert client.get("/health").json()["mcp_targets"]["board"] == {
        "configured": False, "url": None, "reachable": None}


def test_tcp_reachable():
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert gw._tcp_reachable(f"http://127.0.0.1:{port}/mcp") is True
    # srv 已關閉 → 該埠沒人聽
    assert gw._tcp_reachable(f"http://127.0.0.1:{port}/mcp") is False
    assert gw._tcp_reachable("not a url") is False
    assert gw._tcp_reachable("") is False


def test_ledger_upgrades_pre_target_db_in_place(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE calls (
            ts TEXT NOT NULL, day TEXT NOT NULL, month TEXT NOT NULL,
            endpoint TEXT NOT NULL, model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
            cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL,
            usd REAL NOT NULL, outcome TEXT NOT NULL
        );
        CREATE TABLE questions (
            day TEXT NOT NULL, client TEXT NOT NULL, n INTEGER NOT NULL,
            PRIMARY KEY (day, client)
        );
        INSERT INTO calls VALUES ('2026-10-30T08:00:00+00:00', '2026-10-30', '2026-10',
                                  'chat', 'claude-haiku-5-5', 1, 1, 0, 0, 0.2, 'ok');
        """
    )
    conn.commit()
    conn.close()
    clock = Clock(dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.UTC))
    book = ledger.Ledger(str(path), daily_budget_usd=0.5, monthly_budget_usd=10.0, now=clock)
    book.record(endpoint="chat", model="claude-haiku-5-5", input_tokens=1, output_tokens=1,
                cache_read=0, cache_write=0, usd=0.1, target="board")
    state = book.budget_state()
    assert state["daily"]["cmms"]["spent_usd"] == pytest.approx(0.2)  # 舊列歸 cmms
    assert state["daily"]["board"]["spent_usd"] == pytest.approx(0.1)
    assert state["monthly"]["spent_usd"] == pytest.approx(0.3)
    # 重開不再 ALTER(冪等)
    ledger.Ledger(str(path), daily_budget_usd=0.5, monthly_budget_usd=10.0, now=clock)
    conn = sqlite3.connect(path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(calls)")]
    rows = conn.execute("SELECT usd, target FROM calls ORDER BY usd").fetchall()
    conn.close()
    assert cols.count("target") == 1
    assert rows == [(0.1, "board"), (0.2, "cmms")]


def test_ledger_daily_per_target_and_overrides(tmp_path):
    clock = Clock(dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.UTC))
    book = ledger.Ledger(str(tmp_path / "l.sqlite"), daily_budget_usd=0.5,
                         monthly_budget_usd=10.0, daily_budget_by_target={"board": 0.1},
                         now=clock)
    assert book.daily_limit("cmms") == 0.5 and book.daily_limit("board") == 0.1
    book.record(endpoint="chat", model="m", input_tokens=0, output_tokens=0, cache_read=0,
                cache_write=0, usd=0.1, target="board")
    with pytest.raises(ledger.BudgetExhausted) as exc:
        book.check_budget("board")
    assert exc.value.scope == "daily"
    book.check_budget("cmms")  # cmms 桶不受影響
    book.check_budget()  # 預設 = cmms
    assert book.budget_state()["daily"]["board"]["state"] == "exhausted"
    assert book.budget_state()["daily"]["cmms"]["state"] == "ok"


def test_board_daily_exhausted_cmms_still_ok(anth, tools, monkeypatch):
    client, use = anth
    monkeypatch.setenv("BOARD_MCP_URL", BOARD_URL)
    monkeypatch.setenv("HERMES_DAILY_BUDGET_USD_BOARD", "0.1")
    _spend_target(0.1, "board")
    use(FakeClient([_msg("ok")]))
    resp = _chat(client, target="board")
    assert resp.status_code == 503 and resp.json() == {"detail": "daily budget exhausted"}
    assert _chat(client).status_code == 200
    daily = client.get("/health").json()["budget"]["daily"]
    assert daily["board"] == {"spent_usd": 0.1, "limit_usd": 0.1, "state": "exhausted"}
    assert daily["cmms"]["state"] == "ok" and daily["cmms"]["limit_usd"] == 0.5


def test_cmms_daily_exhausted_board_still_ok(anth, tools, monkeypatch):
    client, use = anth
    monkeypatch.setenv("BOARD_MCP_URL", BOARD_URL)
    monkeypatch.setenv("HERMES_DAILY_BUDGET_USD_CMMS", "0.2")
    _spend_target(0.2, "cmms")
    use(FakeClient([_msg("ok")]))
    resp = _chat(client)
    assert resp.status_code == 503 and resp.json() == {"detail": "daily budget exhausted"}
    assert _chat(client, target="board").status_code == 200


def test_shared_monthly_exhaustion_blocks_both_targets(anth, tools, monkeypatch, tmp_path):
    client, use = anth
    monkeypatch.setenv("BOARD_MCP_URL", BOARD_URL)
    monkeypatch.setenv("HERMES_MONTHLY_BUDGET_USD", "0.4")
    _spend_target(0.2, "cmms")
    _spend_target(0.2, "board")  # 各自日預算都未滿,但合計用罄月池
    use(FakeClient([_msg("never")]))
    for extra in ({}, {"target": "board"}):
        resp = _chat(client, **extra)
        assert resp.status_code == 503 and resp.json() == {"detail": "monthly budget exhausted"}
    assert _questions_rows(tmp_path) == []  # 預算閘在計題之前
    assert client.get("/health").json()["budget"]["monthly"]["state"] == "exhausted"


def test_question_buckets_separate_per_target(anth, tools, monkeypatch, tmp_path):
    client, use = anth
    monkeypatch.setenv("BOARD_MCP_URL", BOARD_URL)
    monkeypatch.setenv("HERMES_DAILY_QUESTIONS_PER_CLIENT", "10")  # cmms;board 用預設 3
    use(FakeClient([_msg("ok")]))
    board = [_chat(client, target="board").status_code for _ in range(4)]
    assert board == [200, 200, 200, 429]
    cmms = [_chat(client).status_code for _ in range(11)]  # 同一來源,cmms 桶另計
    assert cmms == [200] * 10 + [429]
    rows = dict(_questions_rows(tmp_path))
    source = ledger.client_hash("203.0.113.7", SECRET)
    assert rows == {f"board:{source}": 3, f"cmms:{source}": 10}
    resp = _chat(client, target="board")
    assert resp.status_code == 429 and int(resp.headers["retry-after"]) > 0


def test_mrq_description_is_cmms_only(anth, monkeypatch, tmp_path):
    import sqlite3

    client, use = anth
    monkeypatch.setenv("HERMES_DAILY_BUDGET_USD_BOARD", "0.1")
    _spend_target(0.1, "board")  # board 用罄不影響 /mrq-description
    use(FakeClient(text_message=_msg("Belt replaced.")))
    body = {"work_orders": [{"eid": "EID-97001", "name": "Aligner-01"}]}
    headers = {"X-Hermes-Secret": SECRET}
    assert client.post("/mrq-description", headers=headers, json=body).status_code == 200
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    rows = conn.execute("SELECT endpoint, target FROM calls ORDER BY rowid").fetchall()
    conn.close()
    assert rows[-1] == ("mrq-description", "cmms")
    _spend_target(0.5, "cmms")  # cmms 日預算用罄 → /mrq-description 503
    resp = client.post("/mrq-description", headers=headers, json=body)
    assert resp.status_code == 503 and resp.json() == {"detail": "daily budget exhausted"}


# ── 題數:成功才計;/health 題數總計 ──────────────────────────────────────────────
def test_question_quota_left_does_not_count(tmp_path):
    book, _ = _book(tmp_path)
    a = ledger.client_hash("203.0.113.7", SECRET)
    assert book.question_quota_left(a, 0) is True  # 0 = 不限
    for _ in range(3):
        assert book.question_quota_left(a, 2) is True  # 只檢查,不改計數
    assert book.questions_today()["total"] == 0
    book.take_question(a, 2)
    assert book.question_quota_left(a, 2) is True
    book.take_question(a, 2)
    assert book.question_quota_left(a, 2) is False
    assert book.questions_today()["total"] == 2


def test_questions_today_by_target_without_hashes(tmp_path):
    book, _ = _book(tmp_path)
    for _ in range(2):
        book.take_question("cmms:aaa", 99)
    book.take_question("cmms:bbb", 99)
    book.take_question("board:ccc", 99)
    book.take_question("legacyhash", 99)  # 舊格式(無冒號)歸 cmms
    out = book.questions_today()
    assert out == {
        "day": "2026-10-30",
        "total": 5,
        "by_target": {
            "cmms": {"questions": 4, "clients": 3},
            "board": {"questions": 1, "clients": 1},
        },
    }
    assert "aaa" not in str(out) and "legacyhash" not in str(out)


def test_health_includes_questions_without_hashes(anth):
    client, use = anth
    use(FakeClient([_msg("ok")]))
    assert _chat(client).status_code == 200
    body = client.get("/health").json()
    assert body["questions"]["total"] == 1
    assert body["questions"]["by_target"]["cmms"] == {"questions": 1, "clients": 1}
    assert body["questions"]["by_target"]["board"] == {"questions": 0, "clients": 0}
    assert ledger.client_hash("203.0.113.7", SECRET) not in str(body)


def test_backend_failures_do_not_consume_questions(anth, tmp_path):
    client, use = anth
    boom = anthropic.InternalServerError("x", response=_resp(500), body=None)
    for _ in range(3):  # 超過每日上限 2:失敗不計,所以仍是 502 而非 429
        use(FakeClient([_msg(stop="tool_use")], exc=boom))
        assert _chat(client).status_code == 502
    use(FakeClient([_msg(stop="tool_use")], exc=RuntimeError("tool server down")))
    assert _chat(client).status_code == 502
    assert _questions_rows(tmp_path) == []
    use(FakeClient([_msg("ok")]))
    assert _chat(client).status_code == 200
    assert [n for _, n in _questions_rows(tmp_path)] == [1]


def test_refusal_counts_one_question(anth, tmp_path):
    client, use = anth
    use(FakeClient([_msg(stop="refusal")]))
    assert _chat(client).status_code == 200
    assert [n for _, n in _questions_rows(tmp_path)] == [1]
