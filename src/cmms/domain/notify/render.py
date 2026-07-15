"""通知內文渲染(純函式,無 DB;固定 zh-TW 模板)。

通知內文語言**刻意不做多語系**(Jordan 指定固定繁中):受眾為機台負責人 / 工程團隊 / 線管理者,
一律繁中。UI chrome(/admin/notify)另有 i18n,與此無關。時間一律以廠區台北時間(to_taipei_naive)
呈現。措辭純資訊性(「盡到通知義務」),無催促字眼。

render_* 接受具屬性的物件(WorkOrder / Asset,或測試用 SimpleNamespace),只讀不寫 → 可離線單測。
email 用 subject + body;Telegram 用 `telegram_text`(subject 當首行 + 空行 + body,純文字)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from cmms.domain.work_order.transform import to_taipei_naive

_DASH = "—"  # 空值佔位(em-dash)
_BRIEF_MAX = 40  # 主旨簡述截斷長度


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    subject: str
    body: str

    @property
    def telegram_text(self) -> str:
        """Telegram 純文字:主旨當首行 + 空行 + 內文(無 parse_mode,與 email 內容一致)。"""
        return f"{self.subject}\n\n{self.body}"


def _fmt_time(dt: datetime | None) -> str:
    """tz-aware datetime → 台北牆鐘 `YYYY-MM-DD HH:MM`;None → em-dash。"""
    if dt is None:
        return _DASH
    return to_taipei_naive(dt).strftime("%Y-%m-%d %H:%M")


def _truncate(text: str, limit: int = _BRIEF_MAX) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _asset_desc(asset) -> str:
    """asset.description(生資料不翻譯);None → 空字串。"""
    if asset is None:
        return ""
    return (getattr(asset, "description", None) or "").strip()


def _asset_line(asset) -> str | None:
    if asset is None:
        return None
    line = (getattr(asset, "line", None) or "").strip()
    return line or None


def _assignees_text(wo, assignees: list[str] | None) -> str:
    """負責人顯示字串(0031 多負責人):`assignees` 給定 → 「、」相接;None → 退回單值
    `wo.assigned_person`(相容,供直接呼叫 render_* 的舊測試)。皆空 → em-dash。"""
    names = assignees if assignees is not None else (
        [wo.assigned_person] if getattr(wo, "assigned_person", None) else []
    )
    names = [n.strip() for n in names if n and n.strip()]
    return "、".join(names) if names else _DASH


def _eid_title(asset_id: str, asset) -> str:
    """主旨用的「EID + 機台名」:`EID-70021 Aligner46`;無機台名 → 只 EID。"""
    desc = _asset_desc(asset)
    return f"{asset_id} {desc}".strip()


def _equipment_line(asset_id: str, asset) -> str:
    """內文「設備」列:`EID-70021 · Aligner46（ASSY 線）`;line 為空 → 省略括號。"""
    desc = _asset_desc(asset)
    head = f"{asset_id} · {desc}" if desc else asset_id
    line = _asset_line(asset)
    return f"{head}（{line} 線）" if line else head


def render_opened(
    wo, asset, *, base_url: str, assignees: list[str] | None = None
) -> RenderedMessage:
    """工單開立通知(REACTIVE→【報修】/ PM→【保養】)。`assignees`=全部負責人(0031)。"""
    is_pm = wo.work_type == "PM"
    prefix = "【保養】" if is_pm else "【報修】"
    brief = (wo.brief_description or "").strip()
    brief_subj = _truncate(brief) if brief else "未填簡述"
    subject = f"{prefix}{_eid_title(wo.asset_id, asset)} — {brief_subj}"

    intro = "有新的保養工單開立，通知如下。" if is_pm else "有新的維修單開立，通知如下。"
    brief_label = "工作內容" if is_pm else "故障簡述"
    link = f"{base_url.rstrip('/')}/app/work-orders/{wo.work_order_no}"
    body = "\n".join(
        [
            intro,
            "",
            f"- 工單編號：WO-{wo.work_order_no}",
            f"- 設備：{_equipment_line(wo.asset_id, asset)}",
            f"- {brief_label}：{brief or _DASH}",
            f"- 開單時間：{_fmt_time(wo.opened_at)}",
            f"- 開單帳號：{(wo.opened_by or '').strip() or _DASH}",
            f"- 負責人：{_assignees_text(wo, assignees)}",
            "",
            f"工單連結：{link}",
        ]
    )
    return RenderedMessage(subject=subject, body=body)


def render_closed(
    wo, asset, *, base_url: str, assignees: list[str] | None = None
) -> RenderedMessage:
    """工單結案通知(REACTIVE / PM 皆同一模板)。`assignees`=全部負責人(0031)。"""
    subject = f"【結案】{_eid_title(wo.asset_id, asset)} — WO-{wo.work_order_no} 已結案"
    brief = (wo.brief_description or "").strip()
    action = (wo.action_taken or "").strip()
    link = f"{base_url.rstrip('/')}/app/work-orders/{wo.work_order_no}"
    body = "\n".join(
        [
            "下列工單已結案。",
            "",
            f"- 工單編號：WO-{wo.work_order_no}",
            f"- 設備：{_equipment_line(wo.asset_id, asset)}",
            f"- 故障簡述：{brief or _DASH}",
            f"- 處置摘要：{action or _DASH}",
            f"- 負責人：{_assignees_text(wo, assignees)}",
            f"- 開單時間：{_fmt_time(wo.opened_at)}",
            f"- 結單時間：{_fmt_time(wo.closed_at)}",
            "",
            f"工單連結：{link}",
        ]
    )
    return RenderedMessage(subject=subject, body=body)


def render_message(
    event: str, wo, asset, *, base_url: str, assignees: list[str] | None = None
) -> RenderedMessage:
    """依事件分派 render(flush 用)。event ∈ {'opened','closed'}。`assignees`=全部負責人(0031)。"""
    if event == "opened":
        return render_opened(wo, asset, base_url=base_url, assignees=assignees)
    if event == "closed":
        return render_closed(wo, asset, base_url=base_url, assignees=assignees)
    raise ValueError(f"unknown notify event: {event!r}")
