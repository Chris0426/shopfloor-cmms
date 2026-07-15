"""SOP 說明中心註冊表(內部規格)。

每份 SOP 一筆 `HelpDoc`:清單頁列出、內頁 include 對應片段模板。`summary` 是繁中濃縮操作步驟
(3–6 行,自足)—— 清單頁一句話用,將來 MCP `list_help_docs` 也餵給助理做摘要式回答。

新增 SOP = 加一個片段模板檔(`templates/help/<slug>.zh-TW.html`)+ 這裡註冊一行。零 migration。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HelpDoc:
    slug: str  # URL 片段(/app/help/<slug>);穩定識別
    title: str  # 清單頁 + 內頁標題
    summary: str  # 繁中濃縮步驟(清單頁一句話 + MCP 餵助理)
    template: str  # include 片段路徑(相對 templates 目錄)
    updated: str  # 最後更新日期(YYYY-MM-DD)


HELP_DOCS: tuple[HelpDoc, ...] = (
    HelpDoc(
        slug="telegram-assistant",
        title="Telegram 助理:綁定與使用",
        summary=(
            "① 在 Telegram 搜尋 @shopfloor_cmms_bot,開啟後按「開始 / Start」。"
            "② 登入 cmms 設定頁 →「Telegram 助理」→ 產生綁定碼(10 分鐘內有效、只顯示一次)。"
            "③ 回 Telegram 對 bot 傳 /start <綁定碼>(或直接按頁面「用 Telegram 開啟」)。"
            "綁定後可直接用中文問工單、設備、備品;你負責的機台一開單或結案也會主動通知你。"
        ),
        template="help/telegram-assistant.zh-TW.html",
        updated="2026-07-12",
    ),
    HelpDoc(
        slug="jira-pat",
        title="Jira 權杖:讓 CMMS 用你的身分開 MRQ",
        summary=(
            "① 開 jira.example.com,右上角頭像 → Profile → 左側 "
            "Personal Access Tokens → Create token。"
            "② 名稱填 cmms、有效期建議改成 365 天,按 Create。"
            "③ 複製那串只顯示一次的權杖(離開頁面就看不到了)。"
            "④ 貼進 cmms 設定頁的「Jira 個人存取權杖」欄、按儲存。"
            "之後在任一工單按「轉發到 MRQ」即以你本人身分開請購單。"
        ),
        template="help/jira-pat.zh-TW.html",
        updated="2026-07-12",
    ),
)


def get_help_doc(slug: str) -> HelpDoc | None:
    """slug → HelpDoc;查無回 None(route 據此 404)。"""
    for doc in HELP_DOCS:
        if doc.slug == slug:
            return doc
    return None
