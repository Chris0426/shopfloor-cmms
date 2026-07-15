"""failure_vocab(C2)唯讀 lookup API(thin client,只呼叫 FailureVocabService)。

Analytics 消費契約 = **contract_failure_vocab.v1**(golden fixture
`tests/fixtures/contract_failure_vocab.v1.json`;形狀漂移 → 契約測試紅 → push Analytics)。

受既有 static bearer 保護:`/vocab` **非** `src/cmms/api/auth.py` 的豁免路徑,故
read_api_bearer_middleware 自動覆蓋(缺/錯 token → 401;production 未設 token → 503)。
**不要**為此改 auth.py。

★ 詞彙來源方鐵則:兩軸(mfc / efc)永不合併,回應信封分列兩個 list。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from cmms.api.deps import get_session
from cmms.domain.failure_vocab.schemas import (
    EquipmentFailureCodeRead,
    FailureVocabResponse,
    MesFailmodeRead,
)
from cmms.domain.failure_vocab.service import FailureVocabService
from cmms.domain.work_order.schemas import HoldReasonRead, HoldReasonVocabResponse
from cmms.domain.work_order.service import WorkOrderService

router = APIRouter(tags=["vocab"])


@router.get("/vocab/failure", response_model=FailureVocabResponse)
async def get_failure_vocab(
    session: AsyncSession = Depends(get_session),
) -> FailureVocabResponse:
    """C2 兩軸失效詞彙全量 lookup(mfc ~116 列 + efc 107 列)。

    v1 無過濾參數 —— 全量 dump(查詢參數日後 additive)。**含退役列**
    (`is_active=false`;下游解歷史碼需要,旗標誠實)。兩軸分列、永不合併(詞彙來源方鐵則)。
    service 兩個 list 方法皆已 deterministic 排序(mfc 依 (station,label)、efc 依 code)。
    """
    service = FailureVocabService(session)
    mfcs = await service.list_mes_failmodes()
    efcs = await service.list_equipment_failure_codes()
    return FailureVocabResponse(
        mes_failmodes=[MesFailmodeRead.model_validate(m) for m in mfcs],
        equipment_failure_codes=[EquipmentFailureCodeRead.model_validate(e) for e in efcs],
    )


@router.get("/vocab/hold-reason", response_model=HoldReasonVocabResponse)
async def get_hold_reason_vocab(
    session: AsyncSession = Depends(get_session),
) -> HoldReasonVocabResponse:
    """hold_reason 受控詞彙全量 lookup(code + label + 機器可讀 is_downtime)。

    Analytics 消費此契約算 true downtime —— 判斷哪些 ON_HOLD 區段計入停機
    (`is_downtime=true`=機台停產;`false`=試跑/等機台空檔,機台仍運轉)。
    單一扁平 list(非失效詞彙的兩軸;無「永不合併」鐵則)。**比照 failure vocab 不曝 MCP**
    (讀取面唯 HTTP;寫入=admin 治理走 /admin/vocab)。排序沿用 `list_hold_reasons`
    (WAITING_* 優先、其餘依 code;deterministic)。
    """
    reasons = await WorkOrderService(session).list_hold_reasons()
    return HoldReasonVocabResponse(
        hold_reasons=[HoldReasonRead.model_validate(r) for r in reasons],
    )
