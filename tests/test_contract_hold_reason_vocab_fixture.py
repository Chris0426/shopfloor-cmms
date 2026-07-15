"""hold_reason 讀 API 契約的 golden fixture 測試(EXECUTION-PLAYBOOK §4-1;免 Docker,永遠跑)。

`tests/fixtures/contract_hold_reason_vocab.v1.json` 是 **Analytics 消費契約的金樣本** ——
`GET /vocab/hold-reason` 回應信封(`HoldReasonVocabResponse`)的真實序列化。schema_id =
**contract_hold_reason_vocab.v1**(分析平台對版用)。以固定資料建構 pydantic 物件 →
`model_dump(mode="json")` → 與 fixture 整體相等。契約形狀一變(欄位增/刪/改名/改型)→
本測試**紅** = 機械觸發對 Analytics 的 push。

★★ 契約硬化制度(硬性義務,同 contract_failure_vocab):
- 本 fixture 變更**必須**先過 下游 consumer 登記表 Gate A + 產 push note(Analytics §19)。
- 形狀有破壞性變更時,**新檔名 bump**(contract_hold_reason_vocab.**v2**.json),舊版保留,不可原地覆寫。

樣本鎖住的 wire 形分支:
- ① is_downtime=true(等料=機台停產,計入 downtime)② is_downtime=false(試跑=機台運轉,不計)。
- 單一扁平 list `hold_reasons`(非失效詞彙的兩軸;無「永不合併」鐵則)。
- `WoHoldReason` 無 is_active 欄 → 不曝退役旗標(不虛構欄位)。
- 值為真種子代表值(TEST_RUN / WAITING_PARTS),鎖形狀不鎖內容。
"""

from __future__ import annotations

import json
from pathlib import Path

from cmms.domain.work_order.schemas import HoldReasonRead, HoldReasonVocabResponse

_FIXTURE = Path(__file__).parent / "fixtures" / "contract_hold_reason_vocab.v1.json"


def _sample_response() -> HoldReasonVocabResponse:
    """形狀覆蓋樣本(涵蓋 is_downtime true / false 兩分支)。"""
    return HoldReasonVocabResponse(
        hold_reasons=[
            # ① is_downtime=true(等料=機台停產,計入 downtime)
            HoldReasonRead(
                code="WAITING_PARTS",
                label="Waiting for Parts(等待零件)",
                is_downtime=True,
            ),
            # ② is_downtime=false(試跑=機台運轉中,不計 downtime)
            HoldReasonRead(
                code="TEST_RUN",
                label="Test Run(試跑,機台運轉中)",
                is_downtime=False,
            ),
        ],
    )


def test_hold_reason_vocab_matches_golden_fixture() -> None:
    """HoldReasonVocabResponse 序列化 == 金樣本(形狀契約)。不等 → 形狀漂移,見本檔頂部制度。"""
    serialized = _sample_response().model_dump(mode="json")
    expected = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert serialized == expected
