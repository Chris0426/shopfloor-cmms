"""PRG 結果碼(`?msg=<code>` 等)→ banner 類別的單一真相(新版介面 v2)。

v1 把這張對照表散寫在各 template 裡(`{% if msg == '…' %}<div class="banner banner--…">`
與 `work_order_detail.html` 的 `{% set banner_css = {...} %}`);v2 不再逐頁手抄,改由
`_render()` 注入 `msg_banner`(= `banner_map_for(<v1 template 名>)`),`v2/base.html`
的 banner 區據此決定 class。

類別 = v1 的四類:`ok` 成功(綠)· `warn` 警告但已處理(琥珀)· `err` 擋下 / 失敗(紅)·
`info` 中性(灰)。v1 的 `equipment_detail` / `supplier_detail` / `equipment_new` 用
`.adm-banner` + inline style 畫色;映射按其色 token 歸類:`--st-done` → ok、`--st-red` → err、
`--ink-faint` → info。

來源 = 本 repo 的 v1 templates(逐字):equipment*.html、inventory*.html、supplier*.html、
work_order_detail.html(`banner_css` dict)、work_order_forward.html、settings.html。
不在此表:`admin_*` 的 `?err=`(原始錯誤字串,值域開放)—— `v2/admin_base.html` 一律畫成 err。
"""

from __future__ import annotations

BANNER_CLASSES: tuple[str, ...] = ("ok", "warn", "err", "info")

#: 全站 `msg` 結果碼 → banner 類別。
MSG_BANNER: dict[str, str] = {
    # ---- 通用(equipment / inventory / suppliers / *_detail / *_new)----
    "adminonly": "info",
    "saved": "ok",
    "err": "err",
    # ---- work_order_detail.html `banner_css`(逐字)----
    "part_ok": "ok",
    "part_dup": "info",
    "part_err": "err",
    "part_upd": "ok",
    "part_unknown": "err",
    "part_cancelled": "info",
    "link_ok": "ok",
    "link_err": "err",
    "link_removed": "info",
    "link_needpat": "warn",
    "assign_ok": "ok",
    "assign_err": "err",
    "voidreq_ok": "ok",
    "voidreq_err": "err",
    "brief_ok": "ok",
    "brief_err": "err",
    "reason_ok": "ok",
    "reason_err": "err",
    "cancel_err": "err",
    "void_err": "err",
    "note_err": "err",
    "note_deleted": "info",
    "hours_err": "err",
    "transition_err": "err",
    "photo_err": "warn",
    "forward_ok": "ok",
    "forward_exists": "info",
}

#: 非 `msg` 參數的結果碼:{v1 template: (參數名, {值: 類別})}。v2 頁在頂層
#: `{% set msg = <參數> %}` 後走同一個 banner 區;此表的碼併進該頁的 `msg_banner`。
#:   · `work_order_forward.html`:`error` ∈ empty / pat / config / jira,v1 一律畫紅。
#:   · `inventory_detail.html`:`rfq_css` dict 逐字。★ `photo_err` 在本頁 = err,工單頁 = warn。
#:   · `inventory_rfq_batch.html`:`sent` → ok、`drafted` → warn、`failed` → err。
#:   · `equipment_detail.html`:`issue`(直領結果)ok / dup / upd / cancelled / err。
#:   · `settings.html`:`changed` 是布林旗標 ⇒ 以 URL 上的值 `"1"` 為碼。
PARAM_BANNER: dict[str, tuple[str, dict[str, str]]] = {
    "work_order_forward.html": (
        "error",
        {"empty": "err", "pat": "err", "config": "err", "jira": "err"},
    ),
    "inventory_detail.html": (
        "rfq",
        {
            "sent": "ok",
            "drafted": "warn",
            "failed": "err",
            "nosupplier": "info",
            "adminonly": "info",
            "saved": "ok",
            "editerr": "err",
            "nochange": "info",
            "proposed": "ok",
            "properr": "err",
            "subtypeerr": "err",
            "photo_ok": "ok",
            "photo_err": "err",
        },
    ),
    "inventory_rfq_batch.html": (
        "sent",
        {"sent": "ok", "drafted": "warn", "failed": "err"},
    ),
    "equipment_detail.html": (
        "issue",
        {"ok": "ok", "dup": "info", "upd": "ok", "cancelled": "info", "err": "err"},
    ),
    "settings.html": (
        "changed",
        {"1": "ok"},
    ),
}


def banner_map_for(template_name: str) -> dict[str, str]:
    """某 v1 template(可帶 `v2/` 前綴)在 v2 殼裡該用的 `msg` → 類別對照表。"""
    param = PARAM_BANNER.get(template_name.removeprefix("v2/"))
    if not param:
        return MSG_BANNER
    return {**MSG_BANNER, **param[1]}
