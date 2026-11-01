"""scripts/hermes_usage.py 的格式化函式測試(純函式,不連網)。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "hermes_usage", Path(__file__).resolve().parents[1] / "scripts" / "hermes_usage.py"
)
hermes_usage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hermes_usage)

NORMAL = {
    "status": "ok",
    "backend": "anthropic",
    "budget": {
        "monthly": {"spent_usd": 1.5, "limit_usd": 20.0, "state": "ok"},
        "daily": {"cmms": {"spent_usd": 0.2, "limit_usd": 1.0, "state": "ok"}},
    },
    "questions": {
        "day": "2026-11-01",
        "total": 7,
        "by_target": {
            "cmms": {"questions": 5, "clients": 3},
            "board": {"questions": 2, "clients": 1},
        },
    },
}


def test_normal_payload():
    text, code = hermes_usage.format_usage(NORMAL)
    assert code == 0
    assert "2026-11-01" in text and "total questions: 7" in text
    assert "cmms: 5 questions, 3 distinct clients" in text
    assert "board: 2 questions, 1 distinct clients" in text
    assert "monthly: $1.5 / $20.0  ok" in text


def test_unavailable():
    text, code = hermes_usage.format_usage(
        {"backend": "anthropic", "budget": "unavailable", "questions": "unavailable"}
    )
    assert code == 2
    assert "unavailable" in text


def test_missing_field():
    text, code = hermes_usage.format_usage({"status": "ok", "backend": "codex"})
    assert code == 2
    assert "缺少" in text


def test_partial_target_entry_does_not_crash():
    text, code = hermes_usage.format_usage({"questions": {"by_target": {"cmms": {}}}})
    assert code == 0
    assert "cmms: ? questions" in text


def test_connection_error_exit_code(capsys):
    assert hermes_usage.main(["--url", "http://127.0.0.1:1", "--timeout", "1"]) == 1
    assert "cannot read" in capsys.readouterr().err
