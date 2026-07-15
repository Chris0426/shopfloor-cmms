"""助理回覆安全渲染器單元測試(ADR-020;防注入 —— 不信任 agent 輸出)。

驗:XSS 全 escape、javascript:/外部/非 /app href 拒絕(整段當純文字)、合法 /app 連結、
裸 EID 自動連結、粗體、混合案例、換行不轉 <br>。純函式,無 DB。
"""

from __future__ import annotations

from cmms.web.assistant_render import render_reply


def test_empty_returns_empty():
    assert render_reply("") == ""
    assert render_reply(None) == ""  # type: ignore[arg-type]


def test_plain_text_is_escaped():
    out = render_reply("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out


def test_attribute_escape_in_label():
    out = render_reply('["onmouseover=x](/app/work-orders/1)')
    # label 內的引號被 escape;href 合法 → 產出 <a>,但 label 不逃逸屬性
    assert "<a href=\"/app/work-orders/1\">" in out
    assert "onmouseover=x" in out  # 純文字保留,不成屬性
    assert '"' not in out.split("</a>")[0].replace('href="/app/work-orders/1"', "")


def test_valid_app_link():
    out = render_reply("see [WO 123](/app/work-orders/123) now")
    assert '<a href="/app/work-orders/123">WO 123</a>' in out


def test_javascript_href_rejected_kept_as_text():
    out = render_reply("[click](javascript:alert(1))")
    assert "<a" not in out
    assert "javascript" in out  # 整段當純文字(escape 後)


def test_external_http_href_rejected():
    out = render_reply("[evil](https://evil.example/app/x)")
    assert "<a" not in out
    assert "evil.example" in out


def test_protocol_relative_href_rejected():
    out = render_reply("[x](//evil.example)")
    assert "<a" not in out


def test_non_app_path_rejected():
    out = render_reply("[home](/admin/secrets)")
    assert "<a" not in out
    assert "/admin/secrets" in out


def test_bare_eid_autolinked():
    out = render_reply("check EID-70021 please")
    assert '<a href="/app/equipment/EID-70021">EID-70021</a>' in out


def test_bare_eid_not_double_linked_inside_existing_link():
    # 明確連結內的 EID 不應再被裸 EID 規則二次包連結
    out = render_reply("[EID-70021](/app/equipment/EID-70021)")
    assert out.count("<a ") == 1


def test_bold():
    out = render_reply("this is **important** ok")
    assert "<strong>important</strong>" in out


def test_newlines_not_converted_to_br():
    out = render_reply("line1\nline2")
    assert "<br" not in out
    assert "line1\nline2" in out


def test_mixed_case():
    text = "Found **2** work orders:\n[101](/app/work-orders/101) on EID-70021\n<b>x</b>"
    out = render_reply(text)
    assert "<strong>2</strong>" in out
    assert '<a href="/app/work-orders/101">101</a>' in out
    assert '<a href="/app/equipment/EID-70021">EID-70021</a>' in out
    assert "&lt;b&gt;x&lt;/b&gt;" in out
    assert "<b>x</b>" not in out
