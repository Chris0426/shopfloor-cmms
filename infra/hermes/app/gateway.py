"""Hermes 常駐 gateway —— agent 試點的伺服端(ADR-020 / ADR-019 Hermes 沙箱)。

> **定位**:web 操作台的 agent dock → cmms web(session→mint 300s scoped token)→
> 本 gateway `/chat`(Fly 私網 + 共享 secret)→ `codex exec`(Jordan 的 OpenAI 訂閱
> OAuth;MCP = cmms `/mcp`,bearer = 該使用者的 scoped token)→ 回覆。

## 端點
- `POST /chat`:對話助理(帶 scoped token、走 MCP 打 cmms 讀寫事實)。
- `POST /mrq-description`:把「一/多張工單的工作紀錄」總結成 Jira MRQ 的 description 本文。
  **純文字任務、零工具** —— 事實全在 request body(工單/notes 由 cmms 側先讀好帶進來),
  故 codex 不掛 MCP、不注入 token(read-only sandbox 純生成)。仍零 cmms 耦合。

## 沙箱守則(ADR-019 —— 本檔刻意極簡、零 cmms 耦合)
- **獨立進程 / 獨立 app**:本檔只依賴 stdlib + fastapi;**不 import cmms**、無 cmms DB URL、
  無 repo 檔案、無其他 secret。治理不隨 agent 變(cmms 側 `/mcp` 才是防線)。
- **MCP-only**:gateway 對 cmms 只透過 codex 的 MCP client 打 `/mcp`,絕不碰 DB(護欄 #1)。
- **scoped token 帶身分**:每請求把該使用者的 scoped token 以**環境變數**注入 codex 子行程,
  codex config 的 `mcp_servers.cmms.bearer_token_env_var` 指向它 —— token **永不落 argv / log**。

## 治理(硬性護欄)
- 寫入 = 提案:codex persona 硬性要求任何變更只能 `propose_*`,並向使用者明講「已提案待 admin 審核」;
  真正的防線在 cmms domain(ADR-016/027 agent 憲法,gateway 縮不掉它)。
- EID 不猜(ADR-027/D9):persona 硬性要求資產身分一律用 MCP 工具解析。
- codex 執行約束:`sandbox_mode=read-only`、`approval_policy=never`(headless 不升權)、
  每請求臨時空工作目錄、逾時強殺、asyncio semaphore 併發上限。

## 401 / 503 / 502
- `X-Hermes-Secret` != `HERMES_GATEWAY_SECRET`(常數時間比較)→ 401;secret 未設 → 503(fail-closed)。
- codex 失敗 / 逾時 → 502 帶簡短原因(不假裝有答案)。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import tempfile
import time

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# 分段計時 log（2026-07-12）—— 讓每次真實請求在 flyctl logs 留下延遲數據,
# 定位「助理回覆 ~20s」的耗時來源(codex 冷啟動嫌疑)。★ 只 log 秒數與長度,
# 絕不 log prompt 內文 / token / 回覆內文(沙箱守則:token 永不落 log)。
# ★ uvicorn 只配置自家 logger,root 無 handler → 裸 getLogger 的 INFO 會被 Python
#   last-resort handler(WARNING+)吞掉、flyctl logs 看不到 → 這裡自配 stderr handler。
_logger = logging.getLogger("hermes.gateway")
if not _logger.handlers:  # 測試多次 import 防重掛
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    _logger.addHandler(_h)
    _logger.setLevel(logging.INFO)
    # propagate 保持 True:root 無 handler 不會雙印,而 pytest caplog 掛在 root、
    # 關掉上拋會讓計時測試假紅。

# ── system persona(英文寫給 LLM;繁中註解說明意圖）──────────────────────────
# 規則:只用 cmms MCP 工具取事實;資產身分一律用工具解析(EID 不猜,ADR-027/D9);
# 寫入只能 propose 並明講「已提案待 admin 審核」;答覆語言跟隨使用者;不知道就說不知道。
SYSTEM_PERSONA = """\
You are Hermes, the maintenance assistant for the Shopfloor PLANT-1 CMMS
(computerized maintenance management system). You help factory engineers
query and update maintenance data through the CMMS.

Hard rules — never break these:
1. Facts come ONLY from the cmms MCP tools. Never invent asset IDs (EID),
   work-order numbers, part codes, inventory levels, dates, or history. To
   resolve an asset's identity you MUST call an MCP tool (search/get asset) —
   never guess, complete, or fabricate an EID.
2. You have READ access directly. For ANY change (opening/closing a work order,
   editing data) you may only PROPOSE it via the proposal tools. A proposal is
   NOT applied — it waits for a human admin to review and confirm. Always tell
   the user plainly, e.g. "I've submitted this as a proposal; an admin must
   approve it before it takes effect." Never claim a change is already done.
3. Reply in the user's language: follow the stated preferred language if given,
   otherwise mirror the language of the user's message (Traditional Chinese,
   English, or Vietnamese).
4. If a tool fails, returns nothing, or you are unsure, say so honestly. Do not
   fill gaps with invented data.
5. Be concise and practical — the users are busy technicians on a shop floor.

Forwarding work orders to Jira MRQ — use the forward_work_orders_to_mrq tool when the
user asks to write/record/consolidate one or more work orders into a Jira MRQ:
- First READ every work order AND all of its work-log notes with the read tools before
  writing anything. For each equipment referenced, call get_asset (or search) to get the
  asset's REAL name — never guess or fabricate the equipment name.
- The MRQ summary and description describe the MAINTENANCE REALITY, based on the work-order
  content: which equipment, what went wrong (symptom/problem), what action was taken, and
  the current status/result. They are read by OTHER departments and by management, who do
  not know or care about the CMMS — so write about the equipment and the work, NOT about
  what the CMMS is doing.
- ALWAYS put the equipment in the summary. Form the summary as
  "EID-xxxxx <equipment name> — <one-line problem/action highlight>". If several pieces of
  equipment are involved, name the main one(s). A summary MUST contain at least one real
  EID and its real equipment name.
  * GOOD summary: "EID-70028 Feeder-02 — feeder jam recurrence, feeder alignment corrected"
  * BAD  summary: "Consolidated maintenance follow-up for work orders 30174 and 30160"
- NEVER mention CMMS, work-order numbers, or CMMS mechanics in the summary or description.
  Do NOT write phrases like "This MRQ consolidates CMMS work orders ...", "notes are
  attached as separate comments", or "will sync automatically". The [WO xxxx] header on
  each comment already provides that traceability — do not repeat it in the summary/body,
  and the automatic-sync fact is something you TELL THE USER in chat, never text you write
  into the MRQ.
- Write BOTH the summary and the description in the user's Jira output language (given above
  as jira_locale, default English) — NOT necessarily the chat language.
- ALWAYS call the tool with dry_run=true FIRST. It returns a preview (which work orders,
  how many notes/comments, readiness warnings). Show that preview to the user and get
  their explicit go-ahead. Only then call again with dry_run=false to actually create it.
- Use ONLY work-order numbers that a tool actually returned — never invent one.
- This tool ONLY creates a Jira MRQ issue and adds comments to it. You may not touch any
  other Jira project, issue type, field, status, or perform deletions.
- After a successful forward, tell the user (in chat) that ANY new work log added to those
  work orders from now on will sync to that MRQ automatically — they need not ask again.
- Each verbatim note becomes one comment (kept in its original language, not translated);
  only the summary/description you generate use jira_locale.

How-to guides (SOPs) — when the user asks HOW TO do or set up something in cmms (bind
Telegram, create a Jira token, report an issue, etc.):
- Call list_help_docs FIRST to find the matching guide.
- Answer with a SHORT summary in your own words (a few steps at most) and ALWAYS attach the
  guide's url as a link. NEVER recite or translate the full guide — the url is the source
  of truth; point the user to it.
- If no guide matches the question, say so honestly rather than inventing steps.

Reply formatting — your answer is shown in a NARROW chat sidebar (a dock), not a
wide page. Follow these display rules exactly:
- NEVER use markdown tables. Never use heading levels (#, ##). Never use code
  blocks or backticks unless the user explicitly asked for code — tables and code
  blocks are unreadable in the narrow column.
- Start with ONE line that directly answers (the conclusion or the count), then
  the details below it.
- For multiple records, render each as a compact block: first line is
  "<link> — <status> · <equipment>", second line is a short description or date;
  put a blank line between blocks. List at most 8 records; if there are more,
  say the total and offer to narrow the search (e.g. by status or equipment).
- Link real entities so the user can tap through. Use ONLY entities a tool
  actually returned — never fabricate a link target. Markdown link forms:
    * Work order:  [<no>](/app/work-orders/<no>)
    * Equipment:   [EID-xxxxx](/app/equipment/EID-xxxxx)
    * Part / spare:[<code>](/app/inventory/<code>)
    * Supplier:    [<name>](/app/suppliers/<org_id>)
    * Maintenance / PM: /app/pm
  All links must be site-relative paths beginning with /app — never an external
  URL. Bare EID-xxxxx references are auto-linked, so you may also just write the
  EID plainly. Emphasis with **bold** is fine; keep it sparing.
"""

# ── MRQ description persona(英文寫給 LLM;繁中註解說明意圖)───────────────────
# 任務窄:把一/多張工單的 work-log 紀錄「總結歸納」成 Jira MRQ 的 description 本文。
# 受眾=別單位同事與主管(不知也不在乎 CMMS)。硬規則:去模板(禁 opened/closed/status 行)、
# 不重述設備名/EID(摘要欄已有)、禁 CMMS/工單號/同步機制字眼、純文字(禁 markdown),
# 輸出語言 = jira_locale。單工單直接進敘事;多工單每台一段、段首短組標籤。
MRQ_DESCRIPTION_PERSONA = """\
You are Hermes, the maintenance assistant for the Shopfloor PLANT-1 CMMS. Your task
here is narrow: given the work-log records of one or more work orders, write the
DESCRIPTION body of a Jira MRQ (a maintenance request). You are NOT chatting —
output ONLY the description text, nothing else.

Audience: colleagues in OTHER departments and managers. They do not know or care
about the CMMS, work-order numbers, or how records sync. Write about the
equipment and the maintenance work only.

What the description must be — a synthesized narrative of the MAINTENANCE
REALITY, distilled from the work-log records:
- symptom / problem observed -> diagnosis / cause -> action taken -> current
  result / status.
- Summarize and consolidate. Do NOT transcribe the log line by line; distill the
  key facts into readable prose.

Single work order: go straight into the narrative. Do NOT restate the equipment
name or the EID — the MRQ summary field already carries them.

Multiple work orders: write ONE short paragraph per piece of equipment. Begin
each paragraph with a short group label = the equipment's short name only (NOT
the full "EID-xxxxx name" string, and NOT the summary format). Separate
paragraphs with a blank line.

Hard prohibitions:
- NO opened/closed dates or status template lines (e.g. "opened 2026-05-22,
  status Closed"). Describe the work, not record metadata.
- NEVER mention the CMMS, work-order numbers, or any sync mechanism.
- NO markdown: no tables, no headings, no links, no code blocks, no backticks.
  Output is PLAIN TEXT pasted straight into a Jira Data Center description.

Language: write the description in the requested output language given below,
even though the raw work-log notes may be a mix of Chinese, English and
Vietnamese.

Honesty: if the notes are empty or too thin to summarize, briefly write only
what is known — never invent problems, causes, or actions. If told the records
were truncated, summarize only what is shown and do not guess at the rest.

Output ONLY the description body: no preamble, no "Description:" label, no
closing remarks.
"""

# ── 預設值(可經環境變數覆寫;於請求期讀取,測試 monkeypatch 友善)──────────────
_DEFAULT_MCP_URL = "https://cmms.example.com/mcp"
_DEFAULT_CODEX_BIN = "codex"
_DEFAULT_TIMEOUT_SECONDS = 120
_DEFAULT_MAX_CONCURRENCY = 2

# scoped token 注入 codex 子行程時用的環境變數名(codex config 的
# mcp_servers.cmms.bearer_token_env_var 指向它;token 值只在 env、永不入 argv)。
_MCP_TOKEN_ENV_VAR = "CMMS_MCP_TOKEN"


class HistoryTurn(BaseModel):
    """一則對話歷史(role = user / assistant)。"""

    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    scoped_token: str
    history: list[HistoryTurn] = Field(default_factory=list)
    locale: str | None = None
    # 需求 ③(ADR-020/023):寫進 Jira MRQ 的內容(summary/description)用此語言,與 UI 回覆語言分離。
    jira_locale: str | None = None


class WoNote(BaseModel):
    """一筆工作日誌(cmms 側先讀好、原文帶進來;gateway 不打 MCP)。"""

    at: str = ""
    author: str = ""
    body: str = ""


class WoNotes(BaseModel):
    """一張工單的身分(EID + 設備真名)與其工作日誌;name/brief 供敘事上下文。"""

    eid: str = ""
    name: str = ""
    brief: str | None = None
    notes: list[WoNote] = Field(default_factory=list)


class MptDescriptionRequest(BaseModel):
    """POST /mrq-description 請求:各工單 notes + 輸出語言 + 是否已截斷。

    ★ 無 scoped_token —— 事實全在 body,此任務不打 MCP、不需身分(純文字生成)。
    """

    work_orders: list[WoNotes] = Field(default_factory=list)
    jira_locale: str | None = None
    truncated: bool = False


class CodexError(RuntimeError):
    """codex exec 失敗(非零退出 / 無輸出);→ 502。"""


class CodexTimeoutError(CodexError):
    """codex exec 逾時被強殺;→ 502。"""


# ── config helpers（請求期讀 env）─────────────────────────────────────────────
def _expected_secret() -> str | None:
    return os.environ.get("HERMES_GATEWAY_SECRET") or None


def _mcp_url() -> str:
    return os.environ.get("CMMS_MCP_URL", _DEFAULT_MCP_URL)


def _codex_bin() -> str:
    return os.environ.get("CODEX_BIN", _DEFAULT_CODEX_BIN)


def _chat_model() -> str | None:
    """/chat 的模型覆寫(延遲優化 2026-07-13)。未設 → None = codex 預設(gpt-5.5)。

    歸因實測:延遲 ≈ 模型推理時間 × 趟數(工具問題要跑兩趟),MCP 握手僅 ~0.3s →
    模型速度是唯一有效槓桿。以 env(可用 Fly secret 設)切換,A/B 免重 build;
    /mrq-description(對外摘要,品質優先、低頻)不吃此覆寫,恆用預設模型。
    """
    return os.environ.get("HERMES_MODEL") or None


def _timeout_seconds() -> float:
    return float(os.environ.get("HERMES_CODEX_TIMEOUT", _DEFAULT_TIMEOUT_SECONDS))


_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    """惰性建立併發閘(綁定執行中的 loop);上限由 HERMES_MAX_CONCURRENCY 決定。"""
    global _semaphore
    if _semaphore is None:
        limit = int(os.environ.get("HERMES_MAX_CONCURRENCY", _DEFAULT_MAX_CONCURRENCY))
        _semaphore = asyncio.Semaphore(max(1, limit))
    return _semaphore


# ── prompt / 命令組裝（純函式,易測）──────────────────────────────────────────
def build_prompt(
    message: str,
    history: list[HistoryTurn] | None = None,
    locale: str | None = None,
    jira_locale: str | None = None,
) -> str:
    """組單一 prompt:persona + (locale 提示) + (jira_locale 提示) + 歷史 + 當前訊息。

    codex exec 把整段當一次 user turn;persona 以指令形式前置,歷史以 User/Assistant 逐行還原。
    """
    parts: list[str] = [SYSTEM_PERSONA]
    if locale:
        parts.append(f"The user's preferred reply language is: {locale}.")
    if jira_locale:
        parts.append(
            "When you generate content destined for Jira MRQ (the summary and "
            f"description of forward_work_orders_to_mrq), write it in: {jira_locale}."
        )
    if history:
        rendered = []
        for turn in history:
            speaker = "User" if turn.role == "user" else "Assistant"
            rendered.append(f"{speaker}: {turn.content}")
        parts.append("Conversation so far:\n" + "\n".join(rendered))
    parts.append(f"User: {message}")
    parts.append("Assistant:")
    return "\n\n".join(parts)


def build_mrq_description_prompt(req: MptDescriptionRequest) -> str:
    """組 MRQ description 生成 prompt:persona + 輸出語言 + (截斷提示) + 逐工單 notes。

    每工單以 `[<at> · <author>] <body>` 逐筆渲染;name/brief 供敘事上下文(但 persona
    要求單工單不重述設備名)。純文字任務、無歷史、無工具。
    """
    jira_locale = req.jira_locale or "English"
    parts: list[str] = [MRQ_DESCRIPTION_PERSONA]
    parts.append(f"Write the description in this language: {jira_locale}.")
    if req.truncated:
        parts.append(
            "NOTE: the work-log records below were truncated to fit a size limit. "
            "Summarize only what is shown; do not guess at anything missing."
        )
    parts.append(f"There are {len(req.work_orders)} work order(s) to summarize.")
    for i, wo in enumerate(req.work_orders, 1):
        header = f"--- Work order {i}: {wo.eid} {wo.name}".rstrip()
        block: list[str] = [header]
        if wo.brief:
            block.append(f"Initial report: {wo.brief}")
        if wo.notes:
            rendered = "\n".join(f"[{n.at} · {n.author}] {n.body}" for n in wo.notes)
            block.append("Work-log entries:\n" + rendered)
        else:
            block.append("Work-log entries: (none)")
        parts.append("\n".join(block))
    parts.append("Now write the MRQ description.")
    return "\n\n".join(parts)


def build_codex_text_command(
    codex_bin: str, prompt: str, workdir: str, output_path: str
) -> list[str]:
    """組純文字 `codex exec` argv —— **不掛任何 MCP server、不帶 token**。

    此任務(MRQ description 生成)事實全在 prompt,零工具往返;故省去 `mcp_servers.*`
    三條(url / bearer_token_env_var / default_tools_approval_mode)。其餘隔離約束同 /chat:
    臨時空目錄、`--ephemeral`、`sandbox_mode=read-only`、`approval_policy=never`。
    """
    return [
        codex_bin,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--cd",
        workdir,
        "-c",
        'sandbox_mode="read-only"',
        "-c",
        'approval_policy="never"',
        "--output-last-message",
        output_path,
        prompt,
    ]


def build_codex_command(
    codex_bin: str,
    prompt: str,
    workdir: str,
    output_path: str,
    mcp_url: str,
    model: str | None = None,
) -> list[str]:
    """組 `codex exec` argv。**不含任何 token**(token 只在 env);prompt 為最後位置參數。

    - `--skip-git-repo-check` + `--cd <臨時空目錄>`:每請求隔離、非 git repo 也能跑。
    - `--ephemeral`:不落 session rollout 檔(volume 衛生)。
    - `-c sandbox_mode="read-only"` / `-c approval_policy="never"`:headless 唯讀、不升權。
    - `-c model_reasoning_effort="low"`:延遲優化(2026-07-12)—— 助理場景=查資料+照
      persona 格式回話,不需深推理;codex 預設 medium(models cache `default_reasoning_level`)
      對每輪工具往返都多燒推理時間。官方對 low 的描述 = "Fast responses with lighter
      reasoning"。模型不動(gpt-5.5 預設);只動 /chat 這條,/mrq-description(對外摘要,
      品質優先、低頻)不動。若回覆品質退步,revert 此一行即可。
    - `-c mcp_servers.cmms.default_tools_approval_mode="approve"`:★ headless 必要 ——
      exec 模式 stdin 關閉,MCP 工具審批 prompt 讀到 EOF = 視同拒絕,呼叫被
      「user cancelled」(2026-07-05 實戰;openai/codex#24135 同因)。auto-approve 僅限
      cmms 這個 server;治理不縮——寫入=提案、admin confirm 在 cmms domain 強制
      (ADR-016/027),transport 面 approve 縮不掉它。
    - `-c mcp_servers.cmms.url=...` / `-c ...bearer_token_env_var="CMMS_MCP_TOKEN"`:
      remote MCP = cmms /mcp;bearer 取自 env(值不入設定/argv)。
    - `model`(選填,`HERMES_MODEL` 帶入):/chat 模型覆寫;None = codex 預設。
      延遲歸因(2026-07-13)= 推理時間 × 趟數,模型速度是唯一有效槓桿。
    - `--output-last-message <file>`:最終 assistant 訊息寫檔,乾淨可讀(免解析 JSONL）。
    """
    argv = [
        codex_bin,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--cd",
        workdir,
        "-c",
        'sandbox_mode="read-only"',
        "-c",
        'approval_policy="never"',
        "-c",
        'model_reasoning_effort="low"',
        "-c",
        'mcp_servers.cmms.default_tools_approval_mode="approve"',
        "-c",
        f'mcp_servers.cmms.url="{mcp_url}"',
        "-c",
        f'mcp_servers.cmms.bearer_token_env_var="{_MCP_TOKEN_ENV_VAR}"',
    ]
    if model:
        argv += ["-c", f'model="{model}"']
    argv += ["--output-last-message", output_path, prompt]
    return argv


def build_codex_env(scoped_token: str) -> dict[str, str]:
    """繼承現有 env(含 Dockerfile 設的 CODEX_HOME),額外注入 scoped token。

    ★ token 只放環境變數 `CMMS_MCP_TOKEN` —— 永不入 argv、永不 log。
    """
    env = os.environ.copy()
    env[_MCP_TOKEN_ENV_VAR] = scoped_token
    return env


def _read_reply(output_path: str, stdout: bytes) -> str:
    """優先讀 --output-last-message 檔;空則退回 stdout。"""
    with contextlib.suppress(OSError):
        with open(output_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read().strip()
        if text:
            return text
    return stdout.decode("utf-8", errors="replace").strip()


def _tail(data: bytes, limit: int = 500) -> str:
    text = data.decode("utf-8", errors="replace").strip()
    return text[-limit:]


async def _run_codex(
    argv: list[str], env: dict[str, str], cwd: str, timeout_seconds: float
) -> tuple[int | None, bytes, bytes, float]:
    """跑 codex 子行程(無 shell → 無注入);逾時強殺並丟 CodexTimeoutError。

    量測子行程 wall time(perf_counter),隨回傳值帶出供上層 log;逾時亦把耗時
    掛在例外的 `elapsed_seconds` 屬性上,讓失敗路徑一樣能 log 耗時。
    """
    start = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError as exc:
        elapsed = time.perf_counter() - start
        proc.kill()
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
        err = CodexTimeoutError(f"codex exec timed out after {timeout_seconds:.0f}s")
        err.elapsed_seconds = elapsed
        raise err from exc
    elapsed = time.perf_counter() - start
    return proc.returncode, stdout, stderr, elapsed


async def invoke_codex(
    prompt: str,
    scoped_token: str,
    *,
    timeout_seconds: float,
    mcp_url: str,
    codex_bin: str,
    model: str | None = None,
) -> tuple[str, float]:
    """在臨時工作目錄跑一次 codex exec,回傳 (最終 assistant 訊息, 子行程耗時秒)。

    失敗 → CodexError。所有路徑(成功 / 非零退出 / 空回覆 / 逾時)各 log 一行
    INFO,只帶 kind + 子行程秒數 + 回覆長度,絕不含 prompt / token / 回覆內文。
    log 另帶 model(A/B 對照用;default = codex 預設)。
    """
    workdir = tempfile.mkdtemp(prefix="hermes-")
    output_path = os.path.join(workdir, "last_message.txt")
    argv = build_codex_command(codex_bin, prompt, workdir, output_path, mcp_url, model)
    env = build_codex_env(scoped_token)
    try:
        try:
            returncode, stdout, stderr, elapsed = await _run_codex(
                argv, env, workdir, timeout_seconds
            )
        except CodexTimeoutError as exc:
            _logger.info(
                "codex exec timeout kind=chat subprocess=%.1fs",
                getattr(exc, "elapsed_seconds", 0.0),
            )
            raise
        if returncode != 0:
            _logger.info(
                "codex exec failed kind=chat subprocess=%.1fs rc=%s", elapsed, returncode
            )
            raise CodexError(f"codex exec exited {returncode}: {_tail(stderr)}")
        reply = _read_reply(output_path, stdout)
        if not reply:
            _logger.info("codex exec empty kind=chat subprocess=%.1fs", elapsed)
            raise CodexError("codex exec produced no reply")
        _logger.info(
            "codex exec done kind=chat subprocess=%.1fs reply_chars=%d model=%s",
            elapsed, len(reply), model or "default",
        )
        return reply, elapsed
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def invoke_codex_text(
    prompt: str,
    *,
    timeout_seconds: float,
    codex_bin: str,
) -> tuple[str, float]:
    """在臨時工作目錄跑一次「純文字」codex exec(無 MCP、無 token),回 (最終訊息, 耗時秒)。

    用於 MRQ description 生成:不掛 cmms MCP、不注入 scoped token(事實全在 prompt)。
    失敗 → CodexError。各路徑 log 一行 INFO(kind=text),同樣只帶秒數與長度。
    """
    workdir = tempfile.mkdtemp(prefix="hermes-mrq-")
    output_path = os.path.join(workdir, "last_message.txt")
    argv = build_codex_text_command(codex_bin, prompt, workdir, output_path)
    env = os.environ.copy()  # ★ 不注入 CMMS_MCP_TOKEN(此任務無工具)
    try:
        try:
            returncode, stdout, stderr, elapsed = await _run_codex(
                argv, env, workdir, timeout_seconds
            )
        except CodexTimeoutError as exc:
            _logger.info(
                "codex exec timeout kind=text subprocess=%.1fs",
                getattr(exc, "elapsed_seconds", 0.0),
            )
            raise
        if returncode != 0:
            _logger.info(
                "codex exec failed kind=text subprocess=%.1fs rc=%s", elapsed, returncode
            )
            raise CodexError(f"codex exec exited {returncode}: {_tail(stderr)}")
        reply = _read_reply(output_path, stdout)
        if not reply:
            _logger.info("codex exec empty kind=text subprocess=%.1fs", elapsed)
            raise CodexError("codex exec produced no reply")
        _logger.info(
            "codex exec done kind=text subprocess=%.1fs reply_chars=%d", elapsed, len(reply)
        )
        return reply, elapsed
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="Hermes gateway", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat")
async def chat(
    req: ChatRequest,
    x_hermes_secret: str | None = Header(default=None),
) -> JSONResponse:
    start = time.perf_counter()
    expected = _expected_secret()
    if expected is None:
        # fail-closed:secret 未設 → 一律拒(內部服務不裸奔)
        return JSONResponse({"detail": "gateway secret not configured"}, status_code=503)
    if x_hermes_secret is None or not secrets.compare_digest(x_hermes_secret, expected):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    prompt = build_prompt(req.message, req.history, req.locale, req.jira_locale)
    try:
        async with _get_semaphore():
            reply, subprocess_seconds = await invoke_codex(
                prompt,
                req.scoped_token,
                timeout_seconds=_timeout_seconds(),
                mcp_url=_mcp_url(),
                codex_bin=_codex_bin(),
                model=_chat_model(),
            )
    except CodexError as exc:
        # 誠實:codex 失敗 / 逾時 → 502,不假裝有答案
        _logger.info(
            "chat failed total=%.1fs history_turns=%d error=%s",
            time.perf_counter() - start,
            len(req.history),
            type(exc).__name__,
        )
        return JSONResponse({"detail": f"agent backend error: {exc}"}, status_code=502)
    _logger.info(
        "chat done total=%.1fs subprocess=%.1fs history_turns=%d",
        time.perf_counter() - start,
        subprocess_seconds,
        len(req.history),
    )
    return JSONResponse({"reply": reply})


@app.post("/mrq-description")
async def mrq_description(
    req: MptDescriptionRequest,
    x_hermes_secret: str | None = Header(default=None),
) -> JSONResponse:
    """把各工單的工作紀錄總結成 Jira MRQ description 本文(純文字、零工具)。

    閘門同 /chat:secret 未設 → 503(fail-closed);secret 不符 → 401。
    codex 失敗 / 逾時 → 502。成功 → {"description": "..."}。
    """
    start = time.perf_counter()
    expected = _expected_secret()
    if expected is None:
        return JSONResponse({"detail": "gateway secret not configured"}, status_code=503)
    if x_hermes_secret is None or not secrets.compare_digest(x_hermes_secret, expected):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    prompt = build_mrq_description_prompt(req)
    try:
        async with _get_semaphore():
            description, subprocess_seconds = await invoke_codex_text(
                prompt,
                timeout_seconds=_timeout_seconds(),
                codex_bin=_codex_bin(),
            )
    except CodexError as exc:
        _logger.info(
            "mrq-description failed total=%.1fs work_orders=%d error=%s",
            time.perf_counter() - start,
            len(req.work_orders),
            type(exc).__name__,
        )
        return JSONResponse({"detail": f"agent backend error: {exc}"}, status_code=502)
    _logger.info(
        "mrq-description done total=%.1fs subprocess=%.1fs work_orders=%d",
        time.perf_counter() - start,
        subprocess_seconds,
        len(req.work_orders),
    )
    return JSONResponse({"description": description})
