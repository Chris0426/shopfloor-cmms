"""UI 版本解析(新版介面 v2 為預設,逐頁回落 v1)。

- 新版模板放 `templates/v2/<name>`;`_render()` 渲染 `<name>` 時,`v2/<name>` 存在且不在例外清單
  ⇒ 用 v2,否則整頁回落 v1(原名)。唯一呼叫點 = `routes._render()`(admin_routes 也經它)。
- cookie `cmms_ui=v1` 可強制整站走 v1。不提供任何切換入口(無 `?ui=`、無設定頁選項);
  這是測試用的釘選手段:v1 的 web 測試套件以它固定在舊版標記上,斷言保持不變。
"""

from __future__ import annotations

from fastapi import Request
from jinja2 import Environment, TemplateNotFound

UI_COOKIE = "cmms_ui"
UI_CHOICES: tuple[str, ...] = ("v1", "v2")
DEFAULT_UI = "v2"

#: 永遠走 v1 的 template:登入頁(獨立殼、不經 base)。`help/*` 片段是內容、無 v2 同名檔 ⇒
#: 解析自然回落原名。
_V1_ONLY_NAMES = frozenset({"login.html"})


def ui_version(request: Request) -> str:
    """cookie → `v1` / `v2`;缺省或非法 → `DEFAULT_UI`。"""
    value = request.cookies.get(UI_COOKIE)
    return value if value in UI_CHOICES else DEFAULT_UI


def _template_exists(env: Environment, name: str) -> bool:
    if env.loader is None:
        return False
    try:
        env.loader.get_source(env, name)
    except TemplateNotFound:
        return False
    return True


def resolve_template(env: Environment, name: str, version: str) -> str:
    """v2 且 `v2/<name>` 存在且非例外 ⇒ `v2/<name>`;否則原名(回落 v1)。"""
    if version != "v2" or name in _V1_ONLY_NAMES:
        return name
    candidate = f"v2/{name}"
    return candidate if _template_exists(env, candidate) else name
