"""C2 失效詞彙讀 API 契約的 golden fixture 測試(EXECUTION-PLAYBOOK §4-1;免 Docker,永遠跑)。

`tests/fixtures/contract_failure_vocab.v1.json` 是 **Analytics 消費契約的金樣本** ——
`GET /vocab/failure` 回應信封(`FailureVocabResponse`)的真實序列化。schema_id =
**contract_failure_vocab.v1**(分析平台對版用)。以固定資料建構 pydantic 物件 →
`model_dump(mode="json")` → 與 fixture 整體相等。契約形狀一變(欄位增/刪/改名/改型、
或兩軸被合併)→ 本測試**紅** = 機械觸發對 Analytics 的 push。

★★ 契約硬化制度(硬性義務,同 contract_wo_active_in):
- 本 fixture 變更**必須**先過 下游 consumer 登記表 Gate A + 產 push note(Analytics §19)。
- 形狀有破壞性變更時,**新檔名 bump**(contract_failure_vocab.**v2**.json),舊版保留,不可原地覆寫。

樣本鎖住的 wire 形分支:
- mfc ① fail_flag 全欄非 null(canonical 形)② triage_category(signal_id/seg_class/
  mes_variable = null)③ is_active=false(**退役列仍曝出** —— 下游解歷史碼需要)。
- efc ① 全欄非 null ② station_hint=null(種子 'TODO' → None 的代表)。
- 兩軸分列於 `mes_failmodes` / `equipment_failure_codes`(永不合併,詞彙來源方鐵則)。
- 值為合成代表值(非真資料),鎖形狀不鎖內容。
"""

from __future__ import annotations

import json
from pathlib import Path

from cmms.domain.failure_vocab.schemas import (
    EquipmentFailureCodeRead,
    FailureVocabResponse,
    MesFailmodeRead,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "contract_failure_vocab.v1.json"


def _sample_response() -> FailureVocabResponse:
    """形狀覆蓋樣本(依 service 排序:mfc 依 (station,label)、efc 依 code)。"""
    return FailureVocabResponse(
        mes_failmodes=[
            # ① fail_flag 全欄非 null(canonical 形)
            MesFailmodeRead(
                station="sta1",
                label="OPEN_CIRCUIT",
                signal_id="mes.failmode.open_circuit",
                entry_kind="fail_flag",
                seg_class="sta1ModuleSeg",
                mes_variable="mfcOpenCircuit",
                material_class="assyModule",
                semantic_zh="開路（無訊號）",
                dominant_in_chronic="y3",
                is_active=True,
            ),
            # ② triage_category(signal_id/seg_class/mes_variable = null)
            MesFailmodeRead(
                station="sta1",
                label="triage_A",
                signal_id=None,
                entry_kind="triage_category",
                seg_class=None,
                mes_variable=None,
                material_class="assyModule",
                semantic_zh="三分流大類 A",
                dominant_in_chronic="n",
                is_active=True,
            ),
            # ③ 退役列 is_active=false(仍曝出,鎖住此語意)
            MesFailmodeRead(
                station="sta3",
                label="SensorFail",
                signal_id="mes.failmode.sensorfail",
                entry_kind="fail_flag",
                seg_class="sta3PanelSeg",
                mes_variable="mfcSensorFail",
                material_class="assyModule",
                semantic_zh="感測器失效（已退役）",
                dominant_in_chronic="n",
                is_active=False,
            ),
        ],
        equipment_failure_codes=[
            # ① 全欄非 null
            EquipmentFailureCodeRead(
                code="efcSTA7_AirPressure",
                descr="STA7 air pressure out of range",
                station_hint="sta7",
                recency_status="source_alive_2026-07",
                is_active=True,
            ),
            # ② station_hint=null(種子 'TODO' → None 的代表)
            EquipmentFailureCodeRead(
                code="efcSA1_TcpComms",
                descr="SA1 PLC TCP comms fault",
                station_hint=None,
                recency_status="source_alive_2026-07",
                is_active=True,
            ),
        ],
    )


def test_failure_vocab_matches_golden_fixture() -> None:
    """FailureVocabResponse 序列化 == 金樣本(形狀契約)。不等 → 形狀漂移,見本檔頂部制度。"""
    serialized = _sample_response().model_dump(mode="json")
    expected = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert serialized == expected
