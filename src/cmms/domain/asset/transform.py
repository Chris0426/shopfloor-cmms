"""assets.csv → 匯入資料的純函式(無 DB,可單元測試)。

處理 migration 資料品質問題(01-assets §5):line_no 大小寫正規化、Yes/No 轉 bool、
空字串轉 None。CSV 為 utf-8-sig 乾淨檔,欄位以表頭名對應(非位置)。
"""

from __future__ import annotations

from dataclasses import dataclass

# 產線受控值(01-assets §1.2)。大小寫不一(Wet Loop / Wet loop)在此收斂。
CANONICAL_LINES: tuple[str, ...] = (
    "1K",
    "10K",
    "Wet Loop",
    "EOL",
    "ASSY",
    "IQC",
    "Others",
    "Calibration Center",
    "Warehouse",
)
_LINE_BY_LOWER = {v.lower(): v for v in CANONICAL_LINES}
# legacy 別名:舊 eMaint CSV 寫 "01K" 是遷就其字母排序的 workaround();
# 重灌 assets.csv 時自動收斂到 1K(migration 0033 已更名既有資料)。
_LINE_BY_LOWER["01k"] = "1K"


def line_sort_key(code: str) -> tuple[int, int, str]:
    """產線 code 的自然排序鍵:數字開頭者依 (數值, 其餘字串) 排、非數字開頭排在其後按字串排。

    為什麼不能直接用字串排序:純字串比較下 '1' < '1' 但 '10K' 的第二字元 '0' < '1K' 的 'K'
    (ASCII '0'=48 < 'K'=75),會把 "10K" 排在 "1K" 之前。取 leading digits 轉整數比較,
    即可得 "1K" < "10K" < "EOL" < "Wet Loop" 的直覺順序。大小寫不敏感(rest/字串鍵用 lower)。
    """
    digits = ""
    for ch in code:
        if ch.isdigit():
            digits += ch
        else:
            break
    if digits:
        return (0, int(digits), code[len(digits):].lower())
    return (1, 0, code.lower())

# 已知資料修正(不改 raw CSV,修正留在 git 可稽核):
# EID-70029「Calibrator Burn-in Rig」原始 department 空 → Jordan 確認屬 EQ(2026-06-20)。
KNOWN_DEPARTMENT_OVERRIDES: dict[str, str] = {"EID-70029": "EQ"}


@dataclass(frozen=True, slots=True)
class AssetImport:
    """一筆待匯入 asset(只含 assets.csv 能提供的 9 欄;[UI] 欄位匯入後仍為空)。"""

    asset_id: str
    description: str
    asset_type: str
    asset_subtype: str | None
    department: str | None  # 實務必填,但 legacy 1 筆(EID-70029)空,待 Jordan
    line: str | None
    model_no: str | None
    serial_no: str | None
    available_for_service: bool


def clean(value: str | None) -> str | None:
    """去空白;空字串視為 None。"""
    if value is None:
        return None
    v = value.strip()
    return v or None


def parse_yes_no(value: str | None) -> bool:
    """eMaint `available` 欄:Yes→True、No→False;空值保守視為可用(True)。"""
    return clean(value) != "No"


def normalize_line(value: str | None) -> str | None:
    """產線正規化:已知值收斂到受控大小寫;未知值原樣保留(去空白);空值→None。"""
    v = clean(value)
    if v is None:
        return None
    return _LINE_BY_LOWER.get(v.lower(), v)


def row_to_import(row: dict[str, str | None]) -> AssetImport:
    """單列 CSV(dict,鍵為表頭)→ AssetImport。"""
    asset_id = clean(row.get("compid"))
    if not asset_id:
        raise ValueError("row missing compid (asset_id)")
    description = clean(row.get("comp_desc")) or asset_id
    asset_type = clean(row.get("assettype"))
    if not asset_type:
        raise ValueError(f"{asset_id}: asset_type 為必填,不可空")
    return AssetImport(
        asset_id=asset_id,
        description=description,
        asset_type=asset_type,
        asset_subtype=clean(row.get("assetsubtp")),
        # 空值先套已知修正(EID-70029→EQ);仍 nullable,未知缺漏載入為 NULL
        department=clean(row.get("department")) or KNOWN_DEPARTMENT_OVERRIDES.get(asset_id),
        line=normalize_line(row.get("line_no")),
        model_no=clean(row.get("model_no")),
        serial_no=clean(row.get("serial_no")),
        available_for_service=parse_yes_no(row.get("available")),
    )


# ---- 資產組成圖(ADR-018)----

# relationship_type 受控值 + provenance(source)受控值。見 ARCHITECTURE.md ADR-018。
CONTAINS_MODULE = "contains_module"
SHARED_DEPENDENCY = "shared_dependency"
RELATIONSHIP_TYPES: tuple[str, ...] = (CONTAINS_MODULE, SHARED_DEPENDENCY)
SOURCE_MES_DEP = "mes_dependent_equipment"
SOURCE_CURATED = "cmms_curated"


@dataclass(frozen=True, slots=True)
class RelationshipImport:
    """一條待匯入的組成邊(已分類 + 已定 direction)。"""

    from_asset_id: str
    to_asset_id: str
    relationship_type: str


def classify_dependent_equipment(
    edges: list[tuple[str, str]],
) -> list[RelationshipImport]:
    """把 MES 相依設備匯出的 (parent, child) 邊分類為組成關係(ADR-018 決策 9)。

    Analytics 規則:**同一 child 有多個 parent → `shared_dependency`**(共用資源,如 Aligner
    服務多母機);否則 → `contains_module`(母機內含模組)。direction 依型別翻轉:
    - `contains_module`:from=parent(機台)、to=child(模組)。
    - `shared_dependency`:from=child(共用資源)、to=parent(被服務機台)。

    輸入為 `(parent_eid, child_eid)`(self-loop 防禦性丟棄;分析平台已去自參照)。
    純函式、不查 DB、不驗 EID 是否存在(由 service 的 Q6 守門);輸出去重後的邊。
    """
    parents_of: dict[str, set[str]] = {}
    for parent, child in edges:
        if not parent or not child or parent == child:
            continue
        parents_of.setdefault(child, set()).add(parent)

    out: list[RelationshipImport] = []
    seen: set[tuple[str, str]] = set()
    for parent, child in edges:
        if not parent or not child or parent == child:
            continue
        if (parent, child) in seen:
            continue
        seen.add((parent, child))
        if len(parents_of[child]) >= 2:
            # child 是共用資源(多母機),direction 翻轉:資源(child) → 機台(parent)
            out.append(RelationshipImport(child, parent, SHARED_DEPENDENCY))
        else:
            out.append(RelationshipImport(parent, child, CONTAINS_MODULE))
    return out
