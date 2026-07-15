"""Hermes gateway 單元測試(codex 子行程以 monkeypatch mock,不真跑 codex)。

gateway.py 是獨立小服務(不在 src/cmms 套件內),故以檔案路徑 importlib 載入。
只依賴 repo 既有套件(fastapi + httpx via TestClient)。

驗證面:
- secret 錯 → 401(且不呼叫 codex);secret 未設 → 503。
- secret 對 → 走到 codex 呼叫,回 {"reply": ...}。
- codex 逾時 → 502。
- prompt 組裝含 persona 與 history。
- scoped_token 進 env、**不進 argv**(token 絕不落 argv/log)。
"""

from __future__ import annotations

import importlib.util
import logging
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

_GATEWAY_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "infra"
    / "hermes"
    / "app"
    / "gateway.py"
)


def _load_gateway():
    spec = importlib.util.spec_from_file_location("hermes_gateway", _GATEWAY_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # 註冊到 sys.modules:讓 pydantic 能解析 `from __future__ import annotations` 下的
    # forward ref(list[HistoryTurn])—— 正式 uvicorn import 一律有此註冊,測試須比照。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gateway = _load_gateway()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_SECRET", "test-secret")
    return TestClient(gateway.app)


# ── health ────────────────────────────────────────────────────────────────────
def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ── secret 閘門 ──────────────────────────────────────────────────────────────
def test_chat_wrong_secret_401_and_codex_not_called(client, monkeypatch):
    called = False

    async def _spy(*args, **kwargs):
        nonlocal called
        called = True
        return "should not run"

    monkeypatch.setattr(gateway, "invoke_codex", _spy)

    resp = client.post(
        "/chat",
        json={"message": "hi", "scoped_token": "tok"},
        headers={"X-Hermes-Secret": "wrong"},
    )
    assert resp.status_code == 401
    assert called is False


def test_chat_missing_secret_header_401(client, monkeypatch):
    async def _spy(*args, **kwargs):
        raise AssertionError("codex must not be called on 401")

    monkeypatch.setattr(gateway, "invoke_codex", _spy)
    resp = client.post("/chat", json={"message": "hi", "scoped_token": "tok"})
    assert resp.status_code == 401


def test_chat_secret_not_configured_503(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_SECRET", raising=False)
    client = TestClient(gateway.app)
    resp = client.post(
        "/chat",
        json={"message": "hi", "scoped_token": "tok"},
        headers={"X-Hermes-Secret": "whatever"},
    )
    assert resp.status_code == 503


# ── happy path：secret 對 → 走 codex ────────────────────────────────────────
def test_chat_correct_secret_invokes_codex(client, monkeypatch):
    seen = {}

    async def _fake(prompt, scoped_token, *, timeout_seconds, mcp_url, codex_bin, model=None):
        seen["prompt"] = prompt
        seen["scoped_token"] = scoped_token
        # invoke_codex 回傳 (reply, subprocess_seconds) —— 分段計時後的形狀
        return "EID-70021 has 3 open work orders.", 4.2

    monkeypatch.setattr(gateway, "invoke_codex", _fake)

    resp = client.post(
        "/chat",
        json={"message": "list work orders for Aligner46", "scoped_token": "tok-abc"},
        headers={"X-Hermes-Secret": "test-secret"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"reply": "EID-70021 has 3 open work orders."}
    assert seen["scoped_token"] == "tok-abc"
    assert "list work orders for Aligner46" in seen["prompt"]


# ── ★ 分段計時 log(延遲數據)且不洩 prompt 內文 ─────────────────────────────
def test_chat_logs_timing_without_prompt_content(client, monkeypatch, caplog):
    secret_msg = "list work orders for very-secret-machine-name-42"

    async def _fake(prompt, scoped_token, *, timeout_seconds, mcp_url, codex_bin, model=None):
        return "3 open work orders.", 5.7

    monkeypatch.setattr(gateway, "invoke_codex", _fake)

    with caplog.at_level(logging.INFO, logger="hermes.gateway"):
        resp = client.post(
            "/chat",
            json={"message": secret_msg, "scoped_token": "tok-secret-xyz"},
            headers={"X-Hermes-Secret": "test-secret"},
        )
    assert resp.status_code == 200

    records = [r for r in caplog.records if r.name == "hermes.gateway"]
    # 端到端 log 有打:含 total / subprocess / history_turns 秒數欄
    done = [r.getMessage() for r in records if r.getMessage().startswith("chat done")]
    assert len(done) == 1
    line = done[0]
    assert "total=" in line and "subprocess=5.7s" in line and "history_turns=0" in line

    # ★ 絕不洩漏 prompt 內文 / message / token / 回覆內文
    all_log_text = " ".join(r.getMessage() for r in records)
    assert secret_msg not in all_log_text
    assert "very-secret-machine-name-42" not in all_log_text
    assert "tok-secret-xyz" not in all_log_text
    assert "3 open work orders." not in all_log_text


# ── codex 逾時 → 502 ─────────────────────────────────────────────────────────
def test_chat_codex_timeout_502(client, monkeypatch):
    async def _timeout(*args, **kwargs):
        raise gateway.CodexTimeoutError("codex exec timed out after 120s")

    monkeypatch.setattr(gateway, "invoke_codex", _timeout)
    resp = client.post(
        "/chat",
        json={"message": "hi", "scoped_token": "tok"},
        headers={"X-Hermes-Secret": "test-secret"},
    )
    assert resp.status_code == 502
    assert "timed out" in resp.json()["detail"]


def test_chat_codex_error_502(client, monkeypatch):
    async def _err(*args, **kwargs):
        raise gateway.CodexError("codex exec exited 1: boom")

    monkeypatch.setattr(gateway, "invoke_codex", _err)
    resp = client.post(
        "/chat",
        json={"message": "hi", "scoped_token": "tok"},
        headers={"X-Hermes-Secret": "test-secret"},
    )
    assert resp.status_code == 502


# ── prompt 組裝 ──────────────────────────────────────────────────────────────
def test_build_prompt_contains_persona_history_message():
    history = [
        gateway.HistoryTurn(role="user", content="what is EID-70021"),
        gateway.HistoryTurn(role="assistant", content="Aligner46"),
    ]
    prompt = gateway.build_prompt("open a work order", history, locale="zh-TW")

    # persona 硬性規則要在 prompt 內(EID 不猜 / 寫入只能 propose）
    assert "Never invent asset IDs" in prompt
    assert "PROPOSE" in prompt
    # dock 回覆格式規範(窄欄可讀:不用表格 + 實體連結規約)
    assert "NEVER use markdown tables" in prompt
    assert "/app/work-orders/" in prompt
    assert "/app/equipment/EID-xxxxx" in prompt
    # locale 提示
    assert "zh-TW" in prompt
    # 歷史逐行還原
    assert "User: what is EID-70021" in prompt
    assert "Assistant: Aligner46" in prompt
    # 當前訊息
    assert "User: open a work order" in prompt


# ── ★ token 進 env、不進 argv ────────────────────────────────────────────────
def test_scoped_token_in_env_not_argv():
    token = "super-secret-scoped-token-xyz"
    argv = gateway.build_codex_command(
        codex_bin="codex",
        prompt="hello",
        workdir="/tmp/x",
        output_path="/tmp/x/last.txt",
        mcp_url="https://cmms.example.com/mcp",
    )
    env = gateway.build_codex_env(token)

    # token 絕不出現在任何 argv 元素(不落命令列 / process list / log)
    assert all(token not in part for part in argv)
    # 只以環境變數傳遞,且 codex config 指向該變數名
    assert env["CMMS_MCP_TOKEN"] == token
    joined = " ".join(argv)
    assert 'bearer_token_env_var="CMMS_MCP_TOKEN"' in joined
    # 唯讀 + 不升權 + 遠端 MCP url
    assert 'sandbox_mode="read-only"' in joined
    assert 'approval_policy="never"' in joined
    assert 'mcp_servers.cmms.url="https://cmms.example.com/mcp"' in joined
    # ★ headless MCP 呼叫必要:exec stdin 關閉 → 審批 EOF = 拒絕(user cancelled);
    #   auto-approve 限 cmms server,寫入防線在 domain(ADR-016/027)
    assert 'mcp_servers.cmms.default_tools_approval_mode="approve"' in joined
    # 延遲優化(2026-07-12):/chat 降 reasoning effort(助理=查資料,不需深推理);
    # /mrq-description(對外摘要,品質優先)不降 → 見下一測試
    assert 'model_reasoning_effort="low"' in joined


def test_build_codex_text_command_keeps_default_effort():
    """/mrq-description 走 build_codex_text_command:對外摘要品質優先,不降 effort。"""
    argv = gateway.build_codex_text_command(
        codex_bin="codex", prompt="p", workdir="/tmp/x", output_path="/tmp/x/last.txt"
    )
    assert "model_reasoning_effort" not in " ".join(argv)


def test_build_codex_command_model_override():
    """HERMES_MODEL 覆寫(2026-07-13 歸因:模型速度=唯一有效槓桿):
    有帶 → argv 含 -c model="...";未帶(None)→ 不出現(codex 預設)。prompt 恆為末位。"""
    base = dict(
        codex_bin="codex", prompt="p", workdir="/tmp/x",
        output_path="/tmp/x/last.txt", mcp_url="https://cmms.example.com/mcp",
    )
    with_model = gateway.build_codex_command(**base, model="gpt-5.4-mini")
    assert 'model="gpt-5.4-mini"' in " ".join(with_model)
    assert with_model[-1] == "p"
    without = gateway.build_codex_command(**base)
    assert 'model="' not in " ".join(without)
    assert without[-1] == "p"


def test_chat_model_env(monkeypatch):
    """_chat_model:未設 / 空字串 → None;有值 → 原樣。"""
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    assert gateway._chat_model() is None
    monkeypatch.setenv("HERMES_MODEL", "")
    assert gateway._chat_model() is None
    monkeypatch.setenv("HERMES_MODEL", "gpt-5.4-mini")
    assert gateway._chat_model() == "gpt-5.4-mini"


def test_build_codex_env_inherits_process_env(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/data/codex")
    env = gateway.build_codex_env("tok")
    assert env["CODEX_HOME"] == "/data/codex"
    assert env["CMMS_MCP_TOKEN"] == "tok"


# ── ★ ADR-020 MRQ forwarding persona + jira_locale ──────────────────────────
def test_persona_teaches_mrq_forward_tool():
    p = gateway.SYSTEM_PERSONA
    assert "forward_work_orders_to_mrq" in p
    assert "dry_run=true FIRST" in p
    assert "jira_locale" in p
    assert "sync to that MRQ automatically" in p
    # 決策 8:只准 MRQ,不得碰其他 Jira project/issue
    assert "may not touch any" in p
    # summary/description 以維修事實為本、禁提 CMMS/工單機制
    assert "NEVER mention CMMS" in p
    assert "MAINTENANCE REALITY" in p
    # summary 必含 EID + 真設備名(get_asset 查回,不猜)
    assert "EID-xxxxx <equipment name>" in p
    assert "at least one real\n  EID and its real equipment name" in p


def test_persona_teaches_help_docs_tool():
    """內部規格:how-to 提問先呼 list_help_docs、摘要 + 附 url、絕不整段照唸/翻譯。"""
    p = gateway.SYSTEM_PERSONA
    assert "list_help_docs" in p
    assert "Call list_help_docs FIRST" in p
    assert "NEVER recite or translate the full guide" in p
    assert "ALWAYS attach the" in p and "url" in p
    # 無對應 SOP → 誠實說明,不臆造步驟
    assert "If no guide matches" in p


def test_build_prompt_includes_jira_locale():
    prompt = gateway.build_prompt(
        "record these to MRQ", locale="zh-TW", jira_locale="en"
    )
    assert "write it in: en" in prompt
    # 無 jira_locale 時不加該行
    assert "write it in:" not in gateway.build_prompt("hi", locale="zh-TW")


def test_chat_passes_jira_locale_into_prompt(client, monkeypatch):
    seen = {}

    async def _fake(prompt, scoped_token, *, timeout_seconds, mcp_url, codex_bin, model=None):
        seen["prompt"] = prompt
        return "ok", 1.0

    monkeypatch.setattr(gateway, "invoke_codex", _fake)
    resp = client.post(
        "/chat",
        json={
            "message": "record WO 30167 to MRQ",
            "scoped_token": "tok",
            "locale": "zh-TW",
            "jira_locale": "vi",
        },
        headers={"X-Hermes-Secret": "test-secret"},
    )
    assert resp.status_code == 200
    assert "write it in: vi" in seen["prompt"]


# ── MRQ description 生成(POST /mrq-description;純文字、零工具)─────────────────
def _mrq_req(**over):
    base = {
        "work_orders": [
            {
                "eid": "EID-70021",
                "name": "Aligner46 Bonder",
                "brief": "nozzle jam",
                "notes": [
                    {"at": "2026-07-01 09:05", "author": "human:jlee", "body": "cleaned nozzle"},
                ],
            },
        ],
        "jira_locale": "en",
        "truncated": False,
    }
    base.update(over)
    return base


def test_mrq_description_persona_hard_rules():
    p = gateway.MRQ_DESCRIPTION_PERSONA
    # 去模板 / 禁 CMMS / 純文字 / 敘事 / 多工單分段
    assert "DESCRIPTION body of a Jira MRQ" in p
    assert "NO opened/closed dates or status template lines" in p
    assert "NEVER mention the CMMS" in p
    assert "PLAIN TEXT" in p
    assert "ONE short paragraph per piece of equipment" in p
    # 單工單不重述設備名 / EID
    assert "Do NOT restate the equipment\nname or the EID" in p


def test_build_mrq_description_prompt_shape():
    req = gateway.MptDescriptionRequest(**_mrq_req(jira_locale="vi", truncated=True))
    prompt = gateway.build_mrq_description_prompt(req)
    assert "Write the description in this language: vi." in prompt
    assert "were truncated to fit a size limit" in prompt   # 截斷 NOTE
    assert "1 work order(s)" in prompt
    assert "EID-70021 Aligner46 Bonder" in prompt        # 工單身分入 context
    assert "Initial report: nozzle jam" in prompt
    assert "[2026-07-01 09:05 · human:jlee] cleaned nozzle" in prompt


def test_build_mrq_description_prompt_defaults_english_no_truncate():
    req = gateway.MptDescriptionRequest(**_mrq_req(jira_locale=None, truncated=False))
    prompt = gateway.build_mrq_description_prompt(req)
    assert "Write the description in this language: English." in prompt
    assert "were truncated to fit a size limit" not in prompt  # 無截斷 → 不加 NOTE


def test_build_mrq_description_prompt_multi_wo():
    req = gateway.MptDescriptionRequest(**_mrq_req(work_orders=[
        {"eid": "EID-1", "name": "A", "brief": None, "notes": []},
        {"eid": "EID-2", "name": "B", "brief": None,
         "notes": [{"at": "t", "author": "u", "body": "x"}]},
    ]))
    prompt = gateway.build_mrq_description_prompt(req)
    assert "2 work order(s)" in prompt
    assert "Work order 1: EID-1 A" in prompt
    assert "Work order 2: EID-2 B" in prompt
    assert "Work-log entries: (none)" in prompt        # 無 notes 誠實標示


def test_build_codex_text_command_has_no_mcp_no_token():
    argv = gateway.build_codex_text_command(
        codex_bin="codex", prompt="hello",
        workdir="/tmp/x", output_path="/tmp/x/last.txt",
    )
    joined = " ".join(argv)
    # ★ 純文字任務:不掛 MCP、不指定 token env、無 auto-approve
    assert "mcp_servers" not in joined
    assert "bearer_token_env_var" not in joined
    assert "CMMS_MCP_TOKEN" not in joined
    assert "default_tools_approval_mode" not in joined
    # 仍套隔離約束
    assert 'sandbox_mode="read-only"' in joined
    assert 'approval_policy="never"' in joined
    assert "--ephemeral" in argv
    assert "--output-last-message" in argv
    assert argv[-1] == "hello"                          # prompt 為最後位置參數


def test_mrq_description_wrong_secret_401(client, monkeypatch):
    async def _spy(*a, **k):
        raise AssertionError("codex must not run on 401")

    monkeypatch.setattr(gateway, "invoke_codex_text", _spy)
    resp = client.post(
        "/mrq-description", json=_mrq_req(), headers={"X-Hermes-Secret": "wrong"}
    )
    assert resp.status_code == 401


def test_mrq_description_secret_not_configured_503(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_SECRET", raising=False)
    c = TestClient(gateway.app)
    resp = c.post(
        "/mrq-description", json=_mrq_req(), headers={"X-Hermes-Secret": "x"}
    )
    assert resp.status_code == 503


def test_mrq_description_success(client, monkeypatch):
    seen = {}

    async def _fake(prompt, *, timeout_seconds, codex_bin):
        seen["prompt"] = prompt
        # invoke_codex_text 回傳 (description, subprocess_seconds)
        return "Nozzle jam on the bonder was cleared and the pickup realigned.", 3.1

    monkeypatch.setattr(gateway, "invoke_codex_text", _fake)
    resp = client.post(
        "/mrq-description", json=_mrq_req(), headers={"X-Hermes-Secret": "test-secret"}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "description": "Nozzle jam on the bonder was cleared and the pickup realigned."
    }
    assert "synthesized narrative of the MAINTENANCE" in seen["prompt"]  # persona 前置


def test_mrq_description_codex_error_502(client, monkeypatch):
    async def _err(*a, **k):
        raise gateway.CodexError("codex exec exited 1: boom")

    monkeypatch.setattr(gateway, "invoke_codex_text", _err)
    resp = client.post(
        "/mrq-description", json=_mrq_req(), headers={"X-Hermes-Secret": "test-secret"}
    )
    assert resp.status_code == 502
