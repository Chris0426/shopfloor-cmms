r"""助理回覆安全渲染器(ADR-020;防注入 —— 不信任 agent 輸出)。

`render_reply(text)` 把 Hermes 回覆的**受限 markdown 子集**轉成安全 HTML,窄聊天欄可讀:
1. 全段 HTML-escape(先),故任何 `<script>` / 屬性逃逸都失效。
2. `[label](/app/…)` → `<a>`,**href 白名單 = 僅 `/app` 或 `/app/…` 站內相對路徑**;
   拒 `javascript:` / `//host` / 外部 http / 非 /app 路徑 → 整段保留為純文字。
3. 裸 `EID-\d{5}`(不在既有連結內)→ 自動連到 `/app/equipment/EID-xxxxx`。
4. `**粗體**` → `<strong>`。
5. 換行**不**轉 `<br>`(泡泡沿用 CSS white-space: pre-wrap)。

輸出已由本模組 escape,模板以 `|safe` 輸出。**使用者訊息泡泡不走本渲染器**(維持純
autoescape) —— 只有 agent 回覆需要這些連結能力。
"""

from __future__ import annotations

import html
import re

# [label](href):label 不含 ']';href 不含 ')' 與空白
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
# 粗體(非貪婪,單行內);於已 escape 文字上操作(`*` 不受 escape 影響)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
# 裸 EID:前後不接 word char / `/` / `-`,避免已在連結內或誤切(於已 escape 文字上操作)
_EID_RE = re.compile(r"(?<![\w/-])(EID-\d{5})(?![\w-])")
# 站內路徑白名單:`/app` 或 `/app/…`,僅安全字元(不含 `:`、空白 → 天然排除 scheme)
_APP_PATH_RE = re.compile(r"/app(?:/[A-Za-z0-9/_\-.?=&]*)?")


def _safe_app_path(href: str) -> str | None:
    """回站內相對路徑(通過白名單)否則 None。僅收 `/app` 或 `/app/…`。"""
    href = href.strip()
    if href != "/app" and not href.startswith("/app/"):
        return None
    if not _APP_PATH_RE.fullmatch(href):
        return None
    return href


def _render_inline(segment: str) -> str:
    """非連結文字段:escape → 粗體 → 裸 EID 自動連結。輸入為原文,輸出安全 HTML。"""
    escaped = html.escape(segment)
    escaped = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", escaped)
    escaped = _EID_RE.sub(
        lambda m: f'<a href="/app/equipment/{m.group(1)}">{m.group(1)}</a>', escaped
    )
    return escaped


def render_reply(text: str) -> str:
    """把受限 markdown 子集轉安全 HTML(見模組 docstring)。空輸入 → 空字串。"""
    if not text:
        return ""
    out: list[str] = []
    pos = 0
    for m in _LINK_RE.finditer(text):
        out.append(_render_inline(text[pos : m.start()]))  # 連結前的一般文字
        label, href = m.group(1), m.group(2)
        safe = _safe_app_path(href)
        if safe is not None:
            out.append(f'<a href="{html.escape(safe, quote=True)}">{html.escape(label)}</a>')
        else:
            out.append(html.escape(m.group(0)))  # href 不合白名單 → 整段當純文字
        pos = m.end()
    out.append(_render_inline(text[pos:]))  # 尾段
    return "".join(out)
