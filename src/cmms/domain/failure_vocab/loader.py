"""failure_vocab 種子載入器(C2;migration 之後的 operator 步驟)。

經 FailureVocabService 單一寫入路徑(ADR-001),idempotent(可重跑)。
- 編碼 UTF-8(無 BOM;含繁中 semantic_zh)。
- 讀檔時先 `strip_comment_lines` 剔除檔頭 `#` 註解(表頭列在註解之後)。
- mfc 與 efc 各自一個 `service.write()` 交易(比照 contacts loader)。
- additive-only:upsert 只更新內容欄,不動 is_active(退役由 admin 治理)。
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from cmms.audit import Actor
from cmms.domain.failure_vocab.service import FailureVocabService
from cmms.domain.failure_vocab.transform import (
    parse_efc_codes,
    parse_mes_failmodes,
    strip_comment_lines,
)

MIGRATION_ACTOR = Actor.human("migration")


@dataclass(frozen=True, slots=True)
class MesFailmodeLoadResult:
    read: int  # 非註解資料列(不含表頭)
    loaded: int  # upsert 的詞彙列(fail_flag + triage)
    fail_flags: int
    triage_categories: int
    skipped_doc_rows: int  # 空 label 的零旗標站說明列


@dataclass(frozen=True, slots=True)
class EfcLoadResult:
    read: int  # 非註解資料列(不含表頭)
    loaded: int  # upsert 的設備故障碼


def read_mes_failmode_rows(path: Path) -> list[dict[str, str | None]]:
    """讀 mes_failmode_seed.csv(UTF-8);剔除 `#` 註解列後以表頭名對應欄位。"""
    with path.open(encoding="utf-8", newline="") as fh:
        lines = strip_comment_lines(fh)
    return list(csv.DictReader(lines))


def read_efc_rows(path: Path) -> list[dict[str, str | None]]:
    """讀 efc_equipment_codes.csv(UTF-8);剔除 `#` 註解列後以表頭名對應欄位。"""
    with path.open(encoding="utf-8", newline="") as fh:
        lines = strip_comment_lines(fh)
    return list(csv.DictReader(lines))


async def load_mes_failmodes(
    rows: Iterable[dict[str, str | None]], session: AsyncSession
) -> MesFailmodeLoadResult:
    rows = list(rows)
    parsed = parse_mes_failmodes(rows)
    fail_flags = sum(1 for i in parsed.imports if i.entry_kind == "fail_flag")
    triage = sum(1 for i in parsed.imports if i.entry_kind == "triage_category")

    service = FailureVocabService(session)
    async with service.write(MIGRATION_ACTOR):
        for imp in parsed.imports:
            await service.upsert_mes_failmode(imp, MIGRATION_ACTOR)

    return MesFailmodeLoadResult(
        read=len(rows),
        loaded=len(parsed.imports),
        fail_flags=fail_flags,
        triage_categories=triage,
        skipped_doc_rows=parsed.skipped_doc_rows,
    )


async def load_efc_codes(
    rows: Iterable[dict[str, str | None]], session: AsyncSession
) -> EfcLoadResult:
    rows = list(rows)
    imports = parse_efc_codes(rows)

    service = FailureVocabService(session)
    async with service.write(MIGRATION_ACTOR):
        for imp in imports:
            await service.upsert_equipment_failure_code(imp, MIGRATION_ACTOR)

    return EfcLoadResult(read=len(rows), loaded=len(imports))
