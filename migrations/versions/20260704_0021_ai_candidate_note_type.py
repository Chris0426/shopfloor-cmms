"""wo_note_type += ai_candidate(AI 產物候選;內部設計評審 D11 + ADR-027)

Revision ID: 0021_ai_candidate_note_type
Revises: 0020_soft_delete_columns
Create Date: 2026-07-04

2026-07-04 內部設計評審裁決 D11(全收):AI agent(Analytics Hermes 等)產出的維修/診斷
建議落 `work_order_note`,但必須與人類確認產物在 UI 時間線上「一眼可辨、標明未確認」。
作法 = `wo_note_type`(governed lookup)新增受控值 `ai_candidate`;note 作者原生記
`source_actor=agent:<name>`,UI 時間線對此值渲染「AI 候選(未確認)」badge。
candidate 永不當 confirmed 回流(governance 語意,見 ADR-027 agent 憲法)。

evidence 佐證採「note 內文標準前綴行」約定(v1)`evidence: <ref>`——純文字約定,
本 migration 不新增欄位、不做解析(v1 範圍外)。

data-only(無 schema 變更):僅對既有 `wo_note_type` bulk_insert 一列受控值。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_ai_candidate_note_type"
down_revision: str | None = "0020_soft_delete_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODE = "ai_candidate"
_LABEL = "AI 候選(未確認;evidence: 前綴行帶佐證)"


def upgrade() -> None:
    op.bulk_insert(
        sa.table("wo_note_type", sa.column("code", sa.String), sa.column("label", sa.String)),
        [{"code": _CODE, "label": _LABEL}],
    )


def downgrade() -> None:
    # FK 守門(比照 0019/0020 downgrade 慣例,但更嚴):work_order_note.entry_type FK→
    # wo_note_type.code。若已有 note 引用 ai_candidate,無條件 DELETE 會撞 FK 中止整條
    # downgrade;靜默 no-op 又會留下 UI 已渲染但 lookup 消失的孤兒渲染路徑。故明確 raise,
    # 逼 operator 先處置引用列(candidate 永不回流,不應被機械式移除)。
    bind = op.get_bind()
    referenced = bind.execute(
        sa.text("SELECT count(*) FROM work_order_note WHERE entry_type = :c"), {"c": _CODE}
    ).scalar_one()
    if referenced:
        raise RuntimeError(
            f"downgrade 中止:仍有 {referenced} 筆 work_order_note 引用 note type "
            f"'{_CODE}'。請先處置這些 AI 候選 note 再 downgrade(candidate 永不回流,"
            "不得機械式硬移除;見峰會 D11 / ADR-027)。"
        )
    op.execute(f"DELETE FROM wo_note_type WHERE code = '{_CODE}'")
