"""pair_part_issue_times 純函式單元測試(無 DB):work_order_part ↔ 起始 ISSUE 帳配對。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace as NS

from cmms.domain.work_order.service import (
    AMEND_INCREASE_REASON,
    BACKFILL_ACTOR,
    pair_part_issue_times,
)


def _ts(d: int) -> datetime:
    return datetime(2026, 7, d, 1, 0, tzinfo=UTC)


def _part(pid, item):
    return NS(id=pid, item_code=item)


def _txn(tid, item, day, reason=None, actor="human:jlee", kind="ISSUE"):
    return NS(txn_id=tid, item_code=item, kind=kind, reason=reason, source_actor=actor,
              occurred_at=_ts(day))


def test_pairs_in_order_per_item():
    parts = [_part(1, "A"), _part(2, "B"), _part(3, "A")]
    txns = [_txn(10, "A", 1), _txn(11, "B", 2), _txn(12, "A", 3)]
    assert pair_part_issue_times(parts, txns) == {1: _ts(1), 2: _ts(2), 3: _ts(3)}


def test_amend_increase_and_returns_are_not_issue_origins():
    parts = [_part(1, "A")]
    txns = [_txn(10, "A", 1), _txn(11, "A", 2, reason=AMEND_INCREASE_REASON),
            _txn(12, "A", 3, kind="RETURN")]
    assert pair_part_issue_times(parts, txns) == {1: _ts(1)}


def test_backfill_reason_text_is_never_treated_as_amend():
    parts = [_part(1, "A")]
    txns = [_txn(10, "A", 1, reason=AMEND_INCREASE_REASON, actor=BACKFILL_ACTOR.value)]
    assert pair_part_issue_times(parts, txns) == {1: _ts(1)}


def test_count_mismatch_leaves_item_unpaired():
    parts = [_part(1, "A"), _part(2, "A"), _part(3, "B")]
    txns = [_txn(10, "A", 1), _txn(11, "B", 2)]
    assert pair_part_issue_times(parts, txns) == {3: _ts(2)}  # A 配不齊 → 整組退回 created_at
