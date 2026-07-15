"""downtime 語意純函式(單一語意源;無 DB,可單元測試)。

`segment_is_downtime` 判定單一 status_history 段(以 `to_status` 為準)機台是否停產。
判定規則(Jordan 拍板;內部規格 判定表):

1. `to_status == "ON_HOLD"` → 依 hold_reason 的 is_downtime lookup;hold_reason 為 None 或
   查無 → **True**(沿用現行預設)。所有 work_type 一致。
2. `work_type == "PM"` 且 `to_status == "OPEN"` → **False**(PM 到期待做、機台照跑;工程師切
   IN_PROGRESS 才起算)。**這是唯一的 work_type 行為差異。**
3. 其餘一律沿用 `wo_status.is_downtime` lookup(OPEN/IN_PROGRESS=True、COMPLETED+四終態=False;
   查無 status → False,沿用現行 fallback)。**不**替 REACTIVE 或其他 work_type 硬編碼特例
   (護欄 #8:REACTIVE 的表列行為 == lookup 現值,直接 fall through)。

lookup 值域由呼叫端從 DB 載入傳進來(hold_reason 為 admin 可增改的受控詞彙,不 hardcode)。
"""

from __future__ import annotations

from collections.abc import Mapping


def segment_is_downtime(
    work_type: str,
    to_status: str,
    hold_reason: str | None,
    *,
    status_is_downtime: Mapping[str, bool],
    hold_is_downtime: Mapping[str, bool],
) -> bool:
    """單一 status_history 段(以 to_status 為準)機台是否停產。判定規則見模組 docstring。"""
    if to_status == "ON_HOLD":
        if hold_reason is None:
            return True  # 沿用現行預設:未標 hold_reason 視為停產
        return hold_is_downtime.get(hold_reason, True)  # 查無受控碼 → 保守視為停產
    if work_type == "PM" and to_status == "OPEN":
        return False  # PM 到期待做、機台照跑;工程師切 IN_PROGRESS 才起算(唯一行為差異)
    return status_is_downtime.get(to_status, False)  # 沿用 wo_status.is_downtime(查無→False)
