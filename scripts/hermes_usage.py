#!/usr/bin/env python3
"""讀取 hermes gateway 的 GET /health,印出今日用量摘要(維運用,唯讀、僅標準庫)。

預設 URL 為公開 compose 疊加後 host 端發佈的 http://127.0.0.1:8797
(docker-compose.public.yml 的 hermes ports)。/health 不含任何金鑰或來源雜湊,
本腳本也不送任何憑證。/health 目前不公開「每來源每日題數上限」,所以不顯示該欄位。

退出碼:0 正常;1 連線/解析失敗;2 questions 為 unavailable 或缺欄位。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8797"


def _fmt_budget_entry(name: str, entry: object) -> str:
    """格式化單一預算項目(spent_usd / limit_usd / state);缺欄位就略過。"""
    if not isinstance(entry, dict):
        return f"  {name}: {entry}"
    parts = []
    if "spent_usd" in entry or "limit_usd" in entry:
        parts.append(f"${entry.get('spent_usd', '?')} / ${entry.get('limit_usd', '?')}")
    if "state" in entry:
        parts.append(str(entry["state"]))
    return f"  {name}: " + "  ".join(parts)


def format_usage(health: dict) -> tuple[str, int]:
    """把 /health 的 payload 轉成人類可讀摘要,回傳 (文字, 退出碼)。純函式、不連網。"""
    lines: list[str] = []
    code = 0
    backend = health.get("backend")
    if backend is not None:
        lines.append(f"backend: {backend}")

    q = health.get("questions")
    if q is None:
        lines.append("questions: 缺少此欄位(非 anthropic 後端,或 gateway 版本過舊)")
        code = 2
    elif not isinstance(q, dict):  # "unavailable" 或其他非預期型別
        lines.append(f"questions: {q}(帳本打不開,無法讀取用量)")
        code = 2
    else:
        lines.append(f"day: {q.get('day', '?')} (UTC)   total questions: {q.get('total', '?')}")
        for target, v in (q.get("by_target") or {}).items():
            v = v if isinstance(v, dict) else {}
            lines.append(
                f"  {target}: {v.get('questions', '?')} questions, "
                f"{v.get('clients', '?')} distinct clients"
            )

    b = health.get("budget")
    if isinstance(b, dict):
        lines.append("budget:")
        if "monthly" in b:
            lines.append(_fmt_budget_entry("monthly", b["monthly"]))
        daily = b.get("daily")
        if isinstance(daily, dict):
            for target, entry in daily.items():
                lines.append(_fmt_budget_entry(f"daily/{target}", entry))
    elif b is not None:
        lines.append(f"budget: {b}")
    return "\n".join(lines), code


def fetch_health(url: str, timeout: float = 5.0) -> dict:
    """唯讀 GET <url>/health,回傳解析後的 JSON;失敗丟 OSError / ValueError。"""
    req = urllib.request.Request(url.rstrip("/") + "/health", method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 維運自選 URL
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("unexpected /health payload")
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Show hermes gateway daily usage from GET /health.")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"gateway base URL (default {DEFAULT_URL})")
    ap.add_argument("--json", action="store_true", help="print the raw `questions` field as JSON")
    ap.add_argument("--timeout", type=float, default=5.0)
    args = ap.parse_args(argv)
    try:
        health = fetch_health(args.url, args.timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"error: cannot read {args.url}/health: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(health.get("questions"), ensure_ascii=False))
        return 0 if isinstance(health.get("questions"), dict) else 2
    text, code = format_usage(health)
    print(text, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
