"""ADR-018 資產組成圖:dependent-equipment export 分類純函式單元測試(無 DB)。

驗證 Analytics 分類規則:同一 child 多 parent → shared_dependency(direction 翻轉);
否則 contains_module。self-loop / 重複邊去除。對齊 下游契約登記 的 raw 資料形狀
(雙向邊、重複列、self_ref)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cmms.domain.asset.loader import read_dependent_equipment_rows
from cmms.domain.asset.transform import (
    CONTAINS_MODULE,
    SHARED_DEPENDENCY,
    classify_dependent_equipment,
)

_DEP_CSV = (
    Path(__file__).resolve().parents[1] / "data" / "raw" / "MES-dependent-equipment-export.csv"
)


def _as_set(rels):
    return {(r.from_asset_id, r.to_asset_id, r.relationship_type) for r in rels}


def test_single_parent_is_containment_direction_parent_to_child() -> None:
    rels = classify_dependent_equipment([("EID-70019", "EID-70020")])
    assert _as_set(rels) == {("EID-70019", "EID-70020", CONTAINS_MODULE)}


def test_multi_parent_child_is_shared_dependency_direction_flipped() -> None:
    # Aligner(child)在 3 母機底下 → 共用資源;direction = 資源(child) → 機台(parent)
    edges = [("EID-70012", "EID-70021"), ("EID-70013", "EID-70021"), ("EID-70010", "EID-70021")]
    rels = classify_dependent_equipment(edges)
    assert _as_set(rels) == {
        ("EID-70021", "EID-70012", SHARED_DEPENDENCY),
        ("EID-70021", "EID-70013", SHARED_DEPENDENCY),
        ("EID-70021", "EID-70010", SHARED_DEPENDENCY),
    }


def test_self_loop_dropped() -> None:
    # 下游交付:self_ref(EID-70010→70010)。
    assert classify_dependent_equipment([("EID-70010", "EID-70010")]) == []


def test_duplicate_rows_deduped() -> None:
    # 下游交付:同 parent→child 多列(如 EID-70012→Aligner9 ×5)。
    edges = [("EID-70012", "EID-70006")] * 5
    rels = classify_dependent_equipment(edges)
    assert len(rels) == 1
    assert _as_set(rels) == {("EID-70012", "EID-70006", CONTAINS_MODULE)}


def test_mixed_machine_modules_and_shared_resource() -> None:
    edges = [
        ("EID-70019", "EID-70020"),  # STA1 Gen2 ⊃ Module1
        ("EID-70019", "EID-70024"),  # STA1 Gen2 ⊃ Module4
        ("EID-70012", "EID-70021"),  # Aligner 服務 ASMB-1
        ("EID-70013", "EID-70021"),  # Aligner 服務 ASMB-2(→ 多 parent → shared)
    ]
    rels = classify_dependent_equipment(edges)
    assert _as_set(rels) == {
        ("EID-70019", "EID-70020", CONTAINS_MODULE),
        ("EID-70019", "EID-70024", CONTAINS_MODULE),
        ("EID-70021", "EID-70012", SHARED_DEPENDENCY),
        ("EID-70021", "EID-70013", SHARED_DEPENDENCY),
    }


@pytest.mark.skipif(
    not _DEP_CSV.exists(), reason="real plant data is not shipped in the public repo"
)
def test_real_export_classifies_to_known_counts() -> None:
    """對帳:真實 Analytics 匯出檔(repo `data/raw/`)經 classify 的數應穩定(下游交付 兩邊 closed)。

    raw 502 → 去重/去 self-loop/去 null 後 distinct 216(151 contains + 65 shared)。
    這是「落 DB 前」的對帳;落 DB(綁 105 / dropped 111)見 test_asset_composition_db。
    CSV 變更或 classify 邏輯漂移時此測試會失敗示警。
    """
    edges = read_dependent_equipment_rows(_DEP_CSV)
    assert len(edges) == 502

    rels = classify_dependent_equipment(edges)
    assert len(rels) == 216
    cm = sum(1 for r in rels if r.relationship_type == CONTAINS_MODULE)
    sd = sum(1 for r in rels if r.relationship_type == SHARED_DEPENDENCY)
    assert (cm, sd) == (151, 65)


def test_bidirectional_edge_does_not_loop_and_is_classified_per_child() -> None:
    # 下游交付 雙向邊:Curer9(70005) <-> Aligner9(70006)。各列依「該 child 的 parent 數」獨立分類。
    # 70006 的 parent={70005}(此資料集內) → contains_module 70005→70006
    # 70005 的 parent={70006} → contains_module 70006→70005
    # (兩條同向相反的 contains_module;DB 層的成環由 service 守門擋,見 db 測試)
    edges = [("EID-70005", "EID-70006"), ("EID-70006", "EID-70005")]
    rels = classify_dependent_equipment(edges)
    assert len(rels) == 2
    assert _as_set(rels) == {
        ("EID-70005", "EID-70006", CONTAINS_MODULE),
        ("EID-70006", "EID-70005", CONTAINS_MODULE),
    }
