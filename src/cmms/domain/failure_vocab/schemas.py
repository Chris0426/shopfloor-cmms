"""failure_vocab(C2)讀取 DTO(pydantic v2)。API 回傳這些,不直接吐 ORM 物件。

schema_id = **contract_failure_vocab.v1**(Analytics 對版用;golden fixture
`tests/fixtures/contract_failure_vocab.v1.json`)。

★ 詞彙來源方鐵則:**兩軸永不合併**。wire 形上亦分列 —— `FailureVocabResponse` 是
`mes_failmodes`(mfc 軸)+ `equipment_failure_codes`(efc 軸)兩個各自的 list,
絕不揉成單一異質陣列。

★ 退役列(`is_active=false`)**仍曝出**:下游解歷史工單/訊號裡引用的舊碼時需要它,
旗標誠實表達「此碼已退役、勿新用」,而非讓下游查無。

**刻意不曝**(內部欄,additive 日後可補,不列即不承諾):`id`、audit 欄
(`created_*`/`updated_*`/`source_actor`/`proposed_by`/`confirmed_by`)、
`source_adapter`、`notes`(內部 provenance)。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class MesFailmodeRead(BaseModel):
    """mfc 軸(產品/良率):料為何被判退。自然鍵 (station, label)。"""

    model_config = ConfigDict(from_attributes=True)

    station: str
    label: str
    signal_id: str | None  # 三分流列留空;跨站碰撞,不當唯一鍵(面值保存)
    entry_kind: str  # fail_flag / triage_category
    seg_class: str | None
    mes_variable: str | None
    material_class: str | None
    semantic_zh: str | None
    dominant_in_chronic: str | None  # 面值原樣(y+數字 / n / TODO(校準) / raw90d:<n>)
    is_active: bool  # 退役列仍曝出(旗標誠實)


class EquipmentFailureCodeRead(BaseModel):
    """efc 軸(設備):機台為何故障。自然鍵 code。"""

    model_config = ConfigDict(from_attributes=True)

    code: str
    descr: str | None
    station_hint: str | None  # 前綴推斷、**非權威**;種子 'TODO' → None(站別未解)
    recency_status: str | None
    is_active: bool  # 退役列仍曝出(旗標誠實)


class FailureVocabResponse(BaseModel):
    """C2 兩軸 lookup 回應信封。**兩軸分列,永不合併**(詞彙來源方鐵則)。"""

    mes_failmodes: list[MesFailmodeRead]
    equipment_failure_codes: list[EquipmentFailureCodeRead]
