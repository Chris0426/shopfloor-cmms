"""notify render 純函式單測(無 DB)。固定 zh-TW 模板 + 台北時間 + 空值 em-dash / 省略。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from cmms.domain.notify.render import render_closed, render_message, render_opened

BASE = "https://cmms.example.com"


def _wo(**kw) -> SimpleNamespace:
    base = dict(
        work_type="REACTIVE", work_order_no=30160, asset_id="EID-70021",
        brief_description="馬達過熱", opened_by="assy-ipad",
        assigned_person="Alice Fang",
        opened_at=datetime(2026, 7, 11, 6, 32, tzinfo=UTC),  # 台北 14:32
        closed_at=datetime(2026, 7, 11, 9, 5, tzinfo=UTC),  # 台北 17:05
        action_taken="更換軸承",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _asset(description="Aligner46", line="ASSY") -> SimpleNamespace:
    return SimpleNamespace(description=description, line=line)


def test_opened_reactive_full() -> None:
    m = render_opened(_wo(), _asset(), base_url=BASE)
    assert m.subject == "【報修】EID-70021 Aligner46 — 馬達過熱"
    assert m.body.splitlines()[0] == "有新的維修單開立，通知如下。"
    assert "- 工單編號：WO-30160" in m.body
    assert "- 設備：EID-70021 · Aligner46（ASSY 線）" in m.body
    assert "- 故障簡述：馬達過熱" in m.body
    assert "- 開單時間：2026-07-11 14:32" in m.body
    assert "- 開單帳號：assy-ipad" in m.body
    assert "- 負責人：Alice Fang" in m.body
    assert f"工單連結：{BASE}/app/work-orders/30160" in m.body


def test_opened_multi_assignees_joined() -> None:
    """0031:assignees(全部負責人)以「、」相接;None → 退回單值 assigned_person。"""
    m = render_opened(_wo(), _asset(), base_url=BASE, assignees=["Alice Fang", "Ben Yeh"])
    assert "- 負責人：Alice Fang、Ben Yeh" in m.body
    # assignees=[] → em-dash(即使 denormalized 欄有值,以顯式清單為準)
    m2 = render_opened(_wo(), _asset(), base_url=BASE, assignees=[])
    assert "- 負責人：—" in m2.body
    # assignees=None → 退回 wo.assigned_person(相容)
    m3 = render_closed(_wo(), _asset(), base_url=BASE)
    assert "- 負責人：Alice Fang" in m3.body


def test_opened_pm_labels() -> None:
    m = render_opened(_wo(work_type="PM", brief_description="季度保養"), _asset(), base_url=BASE)
    assert m.subject.startswith("【保養】EID-70021 Aligner46 — 季度保養")
    assert m.body.splitlines()[0] == "有新的保養工單開立，通知如下。"
    assert "- 工作內容：季度保養" in m.body  # PM 用「工作內容」而非「故障簡述」


def test_opened_missing_brief_and_line() -> None:
    m = render_opened(_wo(brief_description=None), _asset(line=None), base_url=BASE)
    assert m.subject == "【報修】EID-70021 Aligner46 — 未填簡述"
    assert "- 設備：EID-70021 · Aligner46\n" in m.body  # 無 line → 省略括號
    assert "（" not in m.body
    assert "- 故障簡述：—" in m.body


def test_opened_missing_opened_by_and_assignee() -> None:
    m = render_opened(_wo(opened_by=None, assigned_person=None), _asset(), base_url=BASE)
    assert "- 開單帳號：—" in m.body
    assert "- 負責人：—" in m.body


def test_opened_brief_truncated() -> None:
    long = "字" * 60
    m = render_opened(_wo(brief_description=long), _asset(), base_url=BASE)
    # 主旨截斷 ~40 + …;內文保留全文
    assert m.subject.endswith("…") and ("字" * 40 + "…") in m.subject
    assert ("字" * 60) in m.body


def test_closed_full() -> None:
    m = render_closed(_wo(), _asset(), base_url=BASE)
    assert m.subject == "【結案】EID-70021 Aligner46 — WO-30160 已結案"
    assert m.body.splitlines()[0] == "下列工單已結案。"
    assert "- 處置摘要：更換軸承" in m.body
    assert "- 開單時間：2026-07-11 14:32" in m.body
    assert "- 結單時間：2026-07-11 17:05" in m.body


def test_closed_missing_action_taken() -> None:
    m = render_closed(_wo(action_taken=None), _asset(), base_url=BASE)
    assert "- 處置摘要：—" in m.body


def test_no_asset_description_falls_back_to_eid_only() -> None:
    m = render_opened(_wo(), _asset(description=""), base_url=BASE)
    assert m.subject == "【報修】EID-70021 — 馬達過熱"  # 無機台名 → 只 EID
    assert "- 設備：EID-70021（ASSY 線）" in m.body


def test_telegram_text_is_subject_blankline_body() -> None:
    m = render_opened(_wo(), _asset(), base_url=BASE)
    assert m.telegram_text == f"{m.subject}\n\n{m.body}"


def test_render_message_dispatch() -> None:
    assert render_message("opened", _wo(), _asset(), base_url=BASE).subject.startswith("【報修】")
    assert render_message("closed", _wo(), _asset(), base_url=BASE).subject.startswith("【結案】")
    try:
        render_message("bogus", _wo(), _asset(), base_url=BASE)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
