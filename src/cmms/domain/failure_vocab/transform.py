"""failure_vocab 種子 CSV → 匯入 DTO 的純函式(無 DB,可單元測試)。

種子檔慣例(異於其他切片,新寫):
- 檔頭有大量 `#` 開頭的權威註解列,**CSV 表頭列在註解之後**;`strip_comment_lines`
  先剔除整行註解(第一個非空白字元為 '#'),再交 csv.DictReader。
- 編碼 = UTF-8(無 BOM;含繁中 semantic_zh);由 loader 讀檔時指定。

mfc(mes_failmode):
- 空 label 列 = 零旗標站的說明列(documentation rows)→ 跳過(不建詞彙),計入 skipped。
- entry_kind:signal_id 空且 label 以 'triage_' 開頭 → 'triage_category';否則 'fail_flag'。
  signal_id 空但 label 非 triage → **raise**(非預期形狀,誠實失敗,不臆測)。
- 自然鍵 (station, label) 重複 → **raise**(不靜默 last-wins)。

efc(equipment_failure_code):
- 常數欄(pdd_class/source_table/source_column/axis)逐列**驗證**須等於預期值,不符 → raise
  (drift guard:種子若換源頭/欄位偏移即刻炸,不靜默載入)。這四欄不入庫。
- station_hint 字面 'TODO' → None(4 個 SA 家族站別未解,見 08-failure-vocab §station_hint)。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# efc 逐列必帶的常數欄(不入庫;驗證用的漂移守門)。
EFC_EXPECTED_CONSTANTS: dict[str, str] = {
    "pdd_class": "dimEquipmentFailureCode",
    "source_table": "mes.EquipmentFailureEvent",
    "source_column": "FailureCode",
    "axis": "equipment",
}
# station_hint 的字面 sentinel:站別未解(4 SA 家族)→ 存 None。
EFC_TODO_HINT = "TODO"

TRIAGE_PREFIX = "triage_"


class FailureVocabError(Exception):
    """failure_vocab 種子解析錯誤(非預期形狀 / 重複自然鍵 / 常數漂移)。"""


def is_comment_line(line: str) -> bool:
    """整行註解:第一個非空白字元為 '#'。"""
    stripped = line.lstrip()
    return bool(stripped) and stripped.startswith("#")


def strip_comment_lines(lines: Iterable[str]) -> list[str]:
    """剔除整行 `#` 註解(表頭列在註解之後,保留);其餘原樣交 csv.DictReader。"""
    return [ln for ln in lines if not is_comment_line(ln)]


def clean(value: str | None) -> str | None:
    """去空白;空字串視為 None。"""
    if value is None:
        return None
    v = value.strip()
    return v or None


# ---- mfc(mes_failmode)----


@dataclass(frozen=True, slots=True)
class MesFailmodeImport:
    """一筆待匯入的 MES 失效模式(mfc 軸)。自然鍵 = (station, label)。"""

    station: str
    label: str
    signal_id: str | None
    entry_kind: str  # 'fail_flag' | 'triage_category'
    seg_class: str | None
    mes_variable: str | None
    material_class: str | None
    semantic_zh: str | None
    dominant_in_chronic: str | None
    source_adapter: str | None
    notes: str | None


@dataclass(frozen=True, slots=True)
class ParsedMesFailmodes:
    imports: list[MesFailmodeImport]
    skipped_doc_rows: int  # 空 label 的零旗標站說明列


def classify_entry_kind(signal_id: str | None, label: str) -> str:
    """entry_kind 分類。signal_id 空 + 非 triage label → raise(非預期形狀)。"""
    if signal_id:
        return "fail_flag"
    if label.startswith(TRIAGE_PREFIX):
        return "triage_category"
    raise FailureVocabError(
        f"mfc row has empty signal_id but non-triage label {label!r} (unexpected shape)"
    )


def row_to_mes_failmode(row: dict[str, str | None]) -> MesFailmodeImport | None:
    """單列 CSV(dict)→ MesFailmodeImport;空 label(說明列)回 None。"""
    label = clean(row.get("label"))
    if not label:
        return None  # 零旗標站說明列(documentation row)
    station = clean(row.get("station"))
    if not station:
        raise FailureVocabError(f"mfc row missing station (label={label!r})")
    signal_id = clean(row.get("signal_id"))
    entry_kind = classify_entry_kind(signal_id, label)
    return MesFailmodeImport(
        station=station,
        label=label,
        signal_id=signal_id,
        entry_kind=entry_kind,
        seg_class=clean(row.get("seg_class")),
        mes_variable=clean(row.get("mes_variable")),
        material_class=clean(row.get("material_class")),
        semantic_zh=clean(row.get("semantic_zh")),
        dominant_in_chronic=clean(row.get("dominant_in_chronic")),
        source_adapter=clean(row.get("source_adapter")),
        notes=clean(row.get("notes")),
    )


def parse_mes_failmodes(rows: Iterable[dict[str, str | None]]) -> ParsedMesFailmodes:
    """逐列解析 mfc 種子;跳過說明列、偵測重複自然鍵(raise)。"""
    imports: list[MesFailmodeImport] = []
    skipped = 0
    seen: set[tuple[str, str]] = set()
    for row in rows:
        imp = row_to_mes_failmode(row)
        if imp is None:
            skipped += 1
            continue
        key = (imp.station, imp.label)
        if key in seen:
            raise FailureVocabError(f"duplicate mfc natural key (station, label): {key}")
        seen.add(key)
        imports.append(imp)
    return ParsedMesFailmodes(imports=imports, skipped_doc_rows=skipped)


# ---- efc(equipment_failure_code)----


@dataclass(frozen=True, slots=True)
class EquipmentFailureCodeImport:
    """一筆待匯入的設備故障碼(efc 軸)。自然鍵 = code。常數欄不入庫。"""

    code: str
    descr: str | None
    station_hint: str | None
    recency_status: str | None


def row_to_efc(row: dict[str, str | None]) -> EquipmentFailureCodeImport:
    """單列 CSV(dict)→ EquipmentFailureCodeImport;常數欄漂移守門 + TODO hint → None。"""
    code = clean(row.get("code"))
    if not code:
        raise FailureVocabError("efc row missing code")
    for col, expected in EFC_EXPECTED_CONSTANTS.items():
        actual = clean(row.get(col))
        if actual != expected:
            raise FailureVocabError(
                f"efc {code}: constant column {col!r} expected {expected!r}, got {actual!r}"
            )
    hint = clean(row.get("station_hint"))
    if hint == EFC_TODO_HINT:
        hint = None  # 站別未解(4 SA 家族);'TODO' 字面 → None
    return EquipmentFailureCodeImport(
        code=code,
        descr=clean(row.get("descr")),
        station_hint=hint,
        recency_status=clean(row.get("recency_status")),
    )


def parse_efc_codes(rows: Iterable[dict[str, str | None]]) -> list[EquipmentFailureCodeImport]:
    """逐列解析 efc 種子;偵測重複 code(raise)。"""
    imports: list[EquipmentFailureCodeImport] = []
    seen: set[str] = set()
    for row in rows:
        imp = row_to_efc(row)
        if imp.code in seen:
            raise FailureVocabError(f"duplicate efc code: {imp.code}")
        seen.add(imp.code)
        imports.append(imp)
    return imports
