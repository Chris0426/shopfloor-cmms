"""hold reason += WAITING_MACHINE_TIME(等機台空檔;機台運轉中 → 不計 downtime)

Revision ID: 0019_hold_reason_machine_time
Revises: 0018_rfq_procurement
Create Date: 2026-07-03

data-only(無 schema 變更):`wo_hold_reason` 補一個受控值 —— Jordan 2026-07-03:
「設備問題已知但等機台有時間 pull down 再處理」是常見延誤原因,期間機台仍在生產,
故 `is_downtime=false`(downtime 精算引擎自動不計該區段);供 Analytics 盯 downtime 時
給出合理解釋。冪等(ON CONFLICT DO NOTHING);loader 種子(WO_HOLD_REASON_SEED)同步加入,
但 prod 不重跑 loader,故以本 migration 送達。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019_hold_reason_machine_time"
down_revision: str | None = "0018_rfq_procurement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODE = "WAITING_MACHINE_TIME"


def upgrade() -> None:
    op.execute(
        "INSERT INTO wo_hold_reason (code, label, is_downtime) "
        f"VALUES ('{_CODE}', 'Waiting for Machine Window(等機台空檔,機台運轉中)', false) "
        "ON CONFLICT (code) DO NOTHING"
    )


def downgrade() -> None:
    # 僅在未被引用時移除;有引用(work_order / status_history)時保留該列(無條件 DELETE
    # 會撞 FK 讓整條 downgrade 中止,故以 NOT EXISTS 守門改為 no-op)。
    op.execute(
        f"DELETE FROM wo_hold_reason WHERE code = '{_CODE}' "
        f"AND NOT EXISTS (SELECT 1 FROM work_order WHERE hold_reason = '{_CODE}') "
        "AND NOT EXISTS (SELECT 1 FROM work_order_status_history "
        f"WHERE hold_reason = '{_CODE}')"
    )
