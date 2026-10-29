"""scripts/hermes_smoke.py 的零網路測試(假 SDK 客戶端、暫存帳本、假金鑰)。

驗證面:未設金鑰 → 結束碼 2;成功 → 印出模型 / usage / 估價且帳本恰一列;
SDK 錯誤 → 結束碼 1、印例外類別、帳本無列;金鑰絕不出現在輸出。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sqlite3
import sys
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "hermes_smoke.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("hermes_smoke_script", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke = _load_script()

API_KEY = "sk-ant-test-SHOULD-NOT-LEAK"


def _msg(text, model, inp=40, out=2):
    usage = SimpleNamespace(
        input_tokens=inp, output_tokens=out,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn", model=model, usage=usage,
    )


class FakeClient:
    """取代 AsyncAnthropic:記錄建構參數與 messages.create 參數。"""

    def __init__(self, *, exc=None, **kwargs):
        self.init_kwargs = kwargs
        self.create_kwargs = None
        self.closed = False
        outer = self

        async def create(**kw):
            outer.create_kwargs = kw
            if exc is not None:
                raise exc
            return _msg("OK", kw["model"])

        self.messages = SimpleNamespace(create=create)

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_sdk(monkeypatch):
    """把 llm_anthropic 內的 AsyncAnthropic 換成假客戶端;回已建立的客戶端清單。"""
    for name in ("HERMES_MODEL_TEXT", "HERMES_MODEL_DEEP"):
        monkeypatch.delenv(name, raising=False)
    made: list[FakeClient] = []
    state = {"exc": None}

    def factory(**kwargs):
        client = FakeClient(exc=state["exc"], **kwargs)
        made.append(client)
        return client

    monkeypatch.setattr(smoke.llm, "AsyncAnthropic", factory)
    return made, state


def _rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT endpoint, model, input_tokens, outcome FROM calls").fetchall()
    finally:
        conn.close()


def test_smoke_missing_key_exits_2(monkeypatch, capsys, fake_sdk, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert smoke.main(["--ledger", str(tmp_path / "l.sqlite")]) == 2
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err
    assert fake_sdk[0] == []  # 沒建客戶端、沒打 API
    assert not (tmp_path / "l.sqlite").exists()


@pytest.mark.parametrize(
    ("argv", "model"),
    [([], "claude-haiku-5-5"), (["--tier", "deep"], "claude-sonnet-5-5")],
)
def test_smoke_success_prints_usage_cost_and_one_row(
    monkeypatch, capsys, fake_sdk, tmp_path, argv, model
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    path = tmp_path / "ledger.sqlite"
    assert smoke.main([*argv, "--ledger", str(path)]) == 0
    out = capsys.readouterr()
    made, _ = fake_sdk
    assert len(made) == 1 and made[0].init_kwargs["api_key"] == API_KEY and made[0].closed
    assert made[0].create_kwargs["model"] == model
    assert "tools" not in made[0].create_kwargs  # 純文字路徑、零工具
    assert f"model:   {model}" in out.out
    assert "input=40 output=2" in out.out
    usd = smoke.ledger.estimate_usd(model, 40, 2, 0, 0)
    assert f"cost:    ~${usd:.6f} USD" in out.out
    assert _rows(path) == [("smoke", model, 40, "ok")]
    assert API_KEY not in out.out + out.err


def test_smoke_sdk_error_exits_1_without_leaking_key(monkeypatch, capsys, fake_sdk, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    _, state = fake_sdk
    response = httpx2.Response(401, request=httpx2.Request("POST", "https://x"))
    state["exc"] = anthropic.AuthenticationError("invalid x-api-key", response=response, body=None)
    path = tmp_path / "ledger.sqlite"
    assert smoke.main(["--ledger", str(path)]) == 1
    out = capsys.readouterr()
    assert "LLMConfigError" in out.err and "AuthenticationError" in out.err
    assert _rows(path) == []
    assert API_KEY not in out.out + out.err
