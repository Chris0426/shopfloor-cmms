"""failure_vocab(C2)種子解析純函式單元測試(無 DB)。

驗證兩軸種子:mfc 自然鍵 (station,label)、signal_id 跨站碰撞保留、說明列跳過、
entry_kind 分類;efc 常數欄漂移守門、TODO station_hint → None。

對真檔(`data/raw/`)的那幾條在公開 repo 沒有輸入檔 → skip(廠內真實資料不隨 repo 發布);
純函式那幾條永遠跑。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cmms.domain.failure_vocab.loader import read_efc_rows, read_mes_failmode_rows
from cmms.domain.failure_vocab.transform import (
    FailureVocabError,
    is_comment_line,
    parse_efc_codes,
    parse_mes_failmodes,
    row_to_efc,
    strip_comment_lines,
)

_DATA = Path(__file__).resolve().parents[1] / "data" / "raw"
_MFC_CSV = _DATA / "mes_failmode_seed.csv"
_EFC_CSV = _DATA / "efc_equipment_codes.csv"

_REASON = "real plant data is not shipped in the public repo"
requires_mfc = pytest.mark.skipif(not _MFC_CSV.exists(), reason=_REASON)
requires_efc = pytest.mark.skipif(not _EFC_CSV.exists(), reason=_REASON)


# ---- comment-line stripping ----


def test_is_comment_line() -> None:
    assert is_comment_line("# header note")
    assert is_comment_line("   # indented comment")
    assert not is_comment_line("signal_id,station,label")
    assert not is_comment_line("mes.failmode.x,sta1,seg,var,X,mc,,,adapter,")


def test_strip_comment_lines_keeps_header_after_comments() -> None:
    lines = [
        "# top comment\n",
        "#\n",
        "  # indented\n",
        "code,descr\n",  # header comes after comments
        "efcX,desc\n",
        "# mid-file comment block\n",
        "efcY,desc\n",
    ]
    kept = strip_comment_lines(lines)
    assert kept == ["code,descr\n", "efcX,desc\n", "efcY,desc\n"]


# ---- mfc real-file parse ----


@requires_mfc
def test_mfc_real_file_counts() -> None:
    rows = read_mes_failmode_rows(_MFC_CSV)
    parsed = parse_mes_failmodes(rows)
    fail = [i for i in parsed.imports if i.entry_kind == "fail_flag"]
    triage = [i for i in parsed.imports if i.entry_kind == "triage_category"]
    assert len(fail) == 113
    assert len(triage) == 3
    assert parsed.skipped_doc_rows == 10
    assert len(parsed.imports) == 116


@requires_mfc
def test_mfc_triage_categories_identified() -> None:
    parsed = parse_mes_failmodes(read_mes_failmode_rows(_MFC_CSV))
    triage_labels = {i.label for i in parsed.imports if i.entry_kind == "triage_category"}
    assert triage_labels == {"triage_A_equipment", "triage_B_incoming", "triage_C_platform"}
    # triage 列無 signal_id
    for i in parsed.imports:
        if i.entry_kind == "triage_category":
            assert i.signal_id is None


@requires_mfc
def test_mfc_cross_station_signal_id_collision_retained() -> None:
    """signal_id 跨站碰撞 → 以 (station,label) 各留為 distinct 條目(詞彙來源方鐵則)。"""
    parsed = parse_mes_failmodes(read_mes_failmode_rows(_MFC_CSV))
    sensorfail = {i.station for i in parsed.imports if i.label == "SensorFail"}
    assert sensorfail == {"prober", "sta3", "sta4"}  # 三站各一,不被去重
    # signal_id 三者相同(mes.failmode.sensorfail),但 (station,label) 不同 → 三條
    sigs = {i.signal_id for i in parsed.imports if i.label == "SensorFail"}
    assert sigs == {"mes.failmode.sensorfail"}
    # shortfail 亦跨 prober/sta3/sta4
    short = {i.station for i in parsed.imports if i.label == "ShortFail"}
    assert short == {"prober", "sta3", "sta4"}


@requires_mfc
def test_mfc_semantic_zh_preserved() -> None:
    parsed = parse_mes_failmodes(read_mes_failmode_rows(_MFC_CSV))
    by_key = {(i.station, i.label): i for i in parsed.imports}
    # 繁中語意正確解碼(UTF-8)
    assert by_key[("sta1", "CycleStop")].semantic_zh == "循環停止（機台停線訊號）"


def test_mfc_duplicate_natural_key_raises() -> None:
    rows = [
        {"signal_id": "mes.failmode.x", "station": "sta1", "label": "X"},
        {"signal_id": "mes.failmode.x2", "station": "sta1", "label": "X"},  # dup (sta1, X)
    ]
    with pytest.raises(FailureVocabError, match="duplicate mfc natural key"):
        parse_mes_failmodes(rows)


def test_mfc_empty_signal_non_triage_raises() -> None:
    rows = [{"signal_id": "", "station": "sta1", "label": "SomethingWeird"}]
    with pytest.raises(FailureVocabError, match="unexpected shape"):
        parse_mes_failmodes(rows)


def test_mfc_empty_label_is_doc_row_skipped() -> None:
    rows = [
        {"signal_id": "", "station": "sta4", "label": ""},  # doc row
        {"signal_id": "mes.failmode.x", "station": "sta1", "label": "X"},
    ]
    parsed = parse_mes_failmodes(rows)
    assert parsed.skipped_doc_rows == 1
    assert len(parsed.imports) == 1


# ---- efc real-file parse ----


@requires_efc
def test_efc_real_file_counts() -> None:
    imports = parse_efc_codes(read_efc_rows(_EFC_CSV))
    assert len(imports) == 107


@requires_efc
def test_efc_todo_station_hint_becomes_none() -> None:
    imports = parse_efc_codes(read_efc_rows(_EFC_CSV))
    none_hint = [i for i in imports if i.station_hint is None]
    # 4 SA 家族(SA1/SA2/SA3/SA4)× 3 碼 = 12 列站別未解
    assert len(none_hint) == 12
    prefixes = {i.code.split("_")[0] for i in none_hint}
    assert prefixes == {"efcSA1", "efcSA2", "efcSA3", "efcSA4"}
    # 其餘皆有推斷站別
    assert all(i.station_hint for i in imports if i not in none_hint)


@requires_efc
def test_efc_known_station_hint_kept() -> None:
    imports = {i.code: i for i in parse_efc_codes(read_efc_rows(_EFC_CSV))}
    assert imports["efcSTA7_AirPressure"].station_hint == "sta7"
    assert imports["efcBonder_HeaterFault"].station_hint == "wirebond"
    assert imports["efcAny_PLCRegisterDump"].station_hint == "any"


def test_efc_constant_mismatch_raises() -> None:
    good = {
        "code": "efcX_Y",
        "descr": "d",
        "station_hint": "sta1",
        "pdd_class": "dimEquipmentFailureCode",
        "source_table": "mes.EquipmentFailureEvent",
        "source_column": "FailureCode",
        "axis": "equipment",
        "recency_status": "source_alive_2026-07",
    }
    # baseline OK
    assert row_to_efc(dict(good)).code == "efcX_Y"
    # 每個常數欄逐一破壞 → raise
    for col in ("pdd_class", "source_table", "source_column", "axis"):
        bad = dict(good)
        bad[col] = "DRIFTED"
        with pytest.raises(FailureVocabError, match="constant column"):
            row_to_efc(bad)


def test_efc_duplicate_code_raises() -> None:
    base = {
        "descr": "d", "station_hint": "sta1",
        "pdd_class": "dimEquipmentFailureCode", "source_table": "mes.EquipmentFailureEvent",
        "source_column": "FailureCode", "axis": "equipment", "recency_status": "x",
    }
    rows = [{"code": "efcX_Y", **base}, {"code": "efcX_Y", **base}]
    with pytest.raises(FailureVocabError, match="duplicate efc code"):
        parse_efc_codes(rows)
