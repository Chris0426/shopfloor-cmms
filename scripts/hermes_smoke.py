"""Hermes Anthropic 後端煙霧測試:打**一次**真的 API,確認金鑰 / 模型 / 計價 / 帳本串得起來。

由擁有者手動執行(會真的計費,金額見輸出的 cost 行):

    export ANTHROPIC_API_KEY=...        # 只放環境變數,不要寫進任何檔案
    .venv/bin/python scripts/hermes_smoke.py                 # Haiku(text 路徑預設模型)
    .venv/bin/python scripts/hermes_smoke.py --tier deep     # Sonnet(deep 模型)
    .venv/bin/python scripts/hermes_smoke.py --ledger /data/ledger.sqlite   # 記到真帳本

- 走 gateway 實際使用的程式碼:`llm_anthropic.make_client()`(金鑰只讀 `ANTHROPIC_API_KEY`)、
  `llm_anthropic.complete_text()`(純文字、零工具、零 MCP)、`ledger.estimate_usd()` 與
  `ledger.Ledger.record()`。模組以檔案路徑載入,與 tests/ 相同。
- `--ledger` 預設寫到暫存檔,不污染真帳本;給真路徑時會先過預算閘(與 gateway 相同,fail-closed)。
- 金鑰不印、不記、不截取片段;錯誤只印例外類別與訊息(SDK 訊息本身不含金鑰),不印請求標頭。
- 結束碼:0 成功、1 API / 預算錯誤、2 未設金鑰。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import pathlib
import sqlite3
import sys
import tempfile

_APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "infra" / "hermes" / "app"

PERSONA = "You are a connectivity smoke test. Follow the user's instruction exactly."
PROMPT = "Reply with the single word OK."
ENDPOINT = "smoke"


def _load(name: str):
    """以檔案路徑載入 gateway 同目錄模組(鍵名 `hermes_<name>`,與 tests/ 共用、已載入就重用)。"""
    key = f"hermes_{name}"
    module = sys.modules.get(key)
    if module is None:
        spec = importlib.util.spec_from_file_location(key, _APP_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return module


# 先載 ledger:llm_anthropic 以檔案路徑載入時會從 sys.modules 取 `hermes_ledger`。
ledger = _load("ledger")
llm = _load("llm_anthropic")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hermes Anthropic backend smoke test (one call).")
    parser.add_argument(
        "--tier", choices=["text", "deep"], default="text",
        help="text = gateway text-path model (default); deep = deep model",
    )
    parser.add_argument(
        "--ledger", default=None,
        help="ledger sqlite path (default: a fresh temp file)",
    )
    return parser.parse_args(argv)


def _ledger_path(arg: str | None) -> str:
    if arg:
        return arg
    return str(pathlib.Path(tempfile.mkdtemp(prefix="hermes-smoke-")) / "ledger.sqlite")


def _last_row(path: str) -> str:
    """帳本最後一列 → 可讀的一行字。"""
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT ts, endpoint, model, input_tokens, output_tokens, cache_read, cache_write,"
            " usd, outcome FROM calls ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return "(no row)"
    ts, endpoint, model, inp, out, cr, cw, usd, outcome = row
    return (
        f"{ts} endpoint={endpoint} model={model} in={inp} out={out} "
        f"cache_read={cr} cache_write={cw} usd={usd:.6f} outcome={outcome}"
    )


def _record(book, usage, outcome: str) -> None:
    """與 gateway `_record` 相同語意:有實際 API 呼叫才記帳。"""
    if usage is None or not usage.api_calls:
        return
    book.record(
        endpoint=ENDPOINT, model=usage.model, input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens, cache_read=usage.cache_read,
        cache_write=usage.cache_write, usd=usage.usd, outcome=outcome,
    )


async def _call(model: str):
    client = llm.make_client()
    try:
        return await llm.complete_text(client, persona=PERSONA, prompt=PROMPT, model=model)
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            await close()


def _describe(exc: BaseException) -> str:
    """例外類別 + 訊息;若是 SDK 例外轉譯而來,附上原始 SDK 例外(仍只有類別與訊息)。"""
    text = f"{type(exc).__name__}: {exc}"
    cause = exc.__cause__
    if cause is not None:
        text += f" (cause {type(cause).__name__}: {cause})"
    return text


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("請先 export ANTHROPIC_API_KEY=...,金鑰不要寫進任何檔案", file=sys.stderr)
        return 2

    model = llm.model_deep() if args.tier == "deep" else llm.model_text()
    path = _ledger_path(args.ledger)
    book = ledger.Ledger(
        path,
        daily_budget_usd=float(os.environ.get("HERMES_DAILY_BUDGET_USD", "0.5")),
        monthly_budget_usd=float(os.environ.get("HERMES_MONTHLY_BUDGET_USD", "10")),
    )
    try:
        book.check_budget()  # 與 gateway 相同:預算用罄就不花錢
    except ledger.BudgetExhausted as exc:
        print(f"budget gate: {exc} (ledger {path})", file=sys.stderr)
        return 1

    print(f"model:   {model}")
    print(f"ledger:  {path}")
    try:
        result = asyncio.run(_call(model))
    except (llm.LLMConfigError, llm.LLMBackendError) as exc:
        _record(book, getattr(exc, "usage", None), "error")
        print(f"error:   {_describe(exc)}", file=sys.stderr)
        return 1

    u = result.usage
    _record(book, u, "ok")
    usd = ledger.estimate_usd(u.model, u.input_tokens, u.output_tokens, u.cache_read, u.cache_write)
    print(f"reply:   {result.text!r}")
    print(
        f"usage:   input={u.input_tokens} output={u.output_tokens} "
        f"cache_read={u.cache_read} cache_write={u.cache_write} api_calls={u.api_calls}"
    )
    print(f"cost:    ~${usd:.6f} USD (ledger.estimate_usd)")
    print(f"row:     {_last_row(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
