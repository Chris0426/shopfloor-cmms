"""JiraForwarder — 工單→Jira MRQ 轉發的 adapter port + live 實作(ADR-020;mirror storage.py 可插拔)。

★ 2026-07-06 決策 1 修訂:cmms **改為直呼 Jira REST**(`HttpJiraForwarder`),用「連結建立者」的
per-user Jira PAT(ADR-022 vault)。原「gateway 側經 atlassian-mcp」路線廢止(schema blocked
+ 事件驅動同步做不到)。能力硬限縮(決策 8):forwarder 只有 create MRQ issue / append comment
兩動作,project/issue-type 由 config 寫死。此檔:port(Protocol)+ live `HttpJiraForwarder` +
兩個 fake(InMemory / Null)供離線測試「note→comment 冪等映射」契約(決策 7)。冪等由呼叫端
outbox 保證(Jira REST 無原生冪等);`append_mrq_comment` 的 `idempotency_key`(= note 的)供
InMemory fake 模擬 1:1 映射,live 實作忽略(呼叫端 flush 前查 status=sent 跳過)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx


@dataclass(frozen=True, slots=True)
class MptComment:
    comment_id: str
    body: str
    idempotency_key: str | None


@runtime_checkable
class JiraForwarder(Protocol):
    async def create_mrq(
        self, *, summary: str, body: str, idempotency_key: str | None = None
    ) -> str:
        """開一張 MRQ issue,回 external_key(MRQ-<n>)。"""
        ...

    async def append_mrq_comment(
        self, *, external_key: str, body: str, idempotency_key: str | None = None
    ) -> str:
        """對既有 MRQ 追加 comment(idempotency_key 防重),回 comment_id。"""
        ...

    async def upload_attachment(
        self, *, external_key: str, filename: str, data: bytes, content_type: str
    ) -> str:
        """上傳附件到 MRQ,回 attachment id/ref。"""
        ...


class NullJiraForwarder:
    """no-op forwarder(未接 agent 時的預設;不產生外部效果)。"""

    async def create_mrq(
        self, *, summary: str, body: str, idempotency_key: str | None = None
    ) -> str:
        return "MRQ-0"

    async def append_mrq_comment(
        self, *, external_key: str, body: str, idempotency_key: str | None = None
    ) -> str:
        return "null-comment"

    async def upload_attachment(
        self, *, external_key: str, filename: str, data: bytes, content_type: str
    ) -> str:
        return "null-attachment"


class InMemoryJiraForwarder:
    """記憶體 fake(測試用):模擬 MRQ + comment 冪等映射(note_id↔comment via idempotency_key)。"""

    def __init__(self) -> None:
        self._issue_seq = 0
        self._comment_seq = 0
        self.issues: dict[str, dict] = {}
        self.comments: dict[str, list[MptComment]] = {}
        # {external_key: [(filename, data, content_type), …]} 供測試斷言「先上傳、不重上」。
        self.attachments: dict[str, list[tuple[str, bytes, str]]] = {}
        self._issue_idem: dict[str, str] = {}
        self._comment_idem: dict[str, str] = {}

    async def create_mrq(
        self, *, summary: str, body: str, idempotency_key: str | None = None
    ) -> str:
        if idempotency_key and idempotency_key in self._issue_idem:
            return self._issue_idem[idempotency_key]  # 冪等
        self._issue_seq += 1
        key = f"MRQ-{self._issue_seq}"
        self.issues[key] = {"summary": summary, "body": body}
        self.comments[key] = []
        if idempotency_key:
            self._issue_idem[idempotency_key] = key
        return key

    async def append_mrq_comment(
        self, *, external_key: str, body: str, idempotency_key: str | None = None
    ) -> str:
        if idempotency_key and idempotency_key in self._comment_idem:
            return self._comment_idem[idempotency_key]  # 冪等 no-op:回既有 comment_id
        self._comment_seq += 1
        cid = f"C-{self._comment_seq}"
        self.comments.setdefault(external_key, []).append(MptComment(cid, body, idempotency_key))
        if idempotency_key:
            self._comment_idem[idempotency_key] = cid
        return cid

    async def upload_attachment(
        self, *, external_key: str, filename: str, data: bytes, content_type: str
    ) -> str:
        # 記錄供斷言;回 Jira 實存的檔名(= 送出的檔名)= comment 內嵌引用鍵。
        self.attachments.setdefault(external_key, []).append((filename, data, content_type))
        return filename


class JiraForwardError(RuntimeError):
    """Jira REST 呼叫失敗(非 2xx / 連線 / 逾時)。含 status + 截斷 body;**PAT 絕不入訊息**。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class HttpJiraForwarder:
    """live 轉發:直呼 Jira REST v2(ADR-020 決策 1 修訂;決策 8 能力硬限縮 = 兩動作)。

    **假設 Jira Data Center / Server**:認證 `Authorization: Bearer <PAT>`(personal access token)。
    端點:POST `/rest/api/2/issue`(建 MRQ)、POST `/rest/api/2/issue/{key}/comment`(加 comment)。
    project/issue-type 由 config 寫死(不由呼叫端傳,防寫到 MRQ 以外;決策 8 縱深)。

    ★ 若實際部署是 **Jira Cloud**(認證為 Basic `email:api_token`),只需把 `_auth_headers` 的
    `Authorization` 換成 `Basic base64(email:token)` 一處即可 —— operator 對接時確認 base_url
    形態(`*.atlassian.net` = Cloud;自架網域 = 多為 Data Center)。

    冪等:Jira REST 無原生冪等鍵,`idempotency_key` 在此**忽略**;防重由呼叫端 outbox
    (flush 前查 status=sent 跳過 + create 的 forward_idem_key 防重)保證。非 2xx / 連線失敗
    → `JiraForwardError`(含 status + 截斷 body;**PAT 只在 header、絕不入例外/log**)。
    """

    def __init__(
        self,
        *,
        base_url: str,
        pat: str,
        project_key: str,
        issue_type: str = "Task",
        timeout_seconds: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._pat = pat  # 只在記憶體 + Authorization header;絕不入 log / 例外 / DB
        self._project_key = project_key
        self._issue_type = issue_type
        self._timeout = timeout_seconds

    def _auth_headers(self) -> dict[str, str]:
        # Jira Data Center:Bearer PAT。Cloud 改此處為 Basic base64(email:token)。
        return {
            "Authorization": f"Bearer {self._pat}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _tail(body: str, limit: int = 300) -> str:
        return body.strip()[:limit]

    async def create_mrq(
        self, *, summary: str, body: str, idempotency_key: str | None = None
    ) -> str:
        """建一張 MRQ issue(project/issue-type 由 config 寫死),回 external_key(如 MRQ-1234)。"""
        payload = {
            "fields": {
                "project": {"key": self._project_key},
                "issuetype": {"name": self._issue_type},
                "summary": summary,
                "description": body,
            }
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/rest/api/2/issue",
                    headers=self._auth_headers(),
                    json=payload,
                )
        except httpx.HTTPError as exc:  # 連線/逾時:type 名不含 PAT
            raise JiraForwardError(f"jira create request failed: {type(exc).__name__}") from exc
        if resp.status_code >= 300:
            raise JiraForwardError(
                f"jira create failed: {resp.status_code} {self._tail(resp.text)}",
                status=resp.status_code,
            )
        key = (resp.json() or {}).get("key")
        if not key:
            raise JiraForwardError("jira create returned no issue key", status=resp.status_code)
        return str(key)

    async def append_mrq_comment(
        self, *, external_key: str, body: str, idempotency_key: str | None = None
    ) -> str:
        """對既有 MRQ 追加 comment,回 comment_id。`idempotency_key` 忽略(呼叫端 outbox 防重)。"""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/rest/api/2/issue/{external_key}/comment",
                    headers=self._auth_headers(),
                    json={"body": body},
                )
        except httpx.HTTPError as exc:
            raise JiraForwardError(f"jira comment request failed: {type(exc).__name__}") from exc
        if resp.status_code >= 300:
            raise JiraForwardError(
                f"jira comment failed: {resp.status_code} {self._tail(resp.text)}",
                status=resp.status_code,
            )
        cid = (resp.json() or {}).get("id")
        if not cid:
            raise JiraForwardError("jira comment returned no id", status=resp.status_code)
        return str(cid)

    async def upload_attachment(
        self, *, external_key: str, filename: str, data: bytes, content_type: str
    ) -> str:
        """上傳附件到 MRQ,回 Jira 實存檔名(comment 內嵌引用鍵)。

        POST `/rest/api/2/issue/{key}/attachments`,multipart(單一 `file` part)。Jira 附件端點
        **必帶** `X-Atlassian-Token: no-check`(否則 XSRF 403)。**不手設 Content-Type** —— 交給
        httpx 依 multipart 自動帶 boundary。逾時放寬(照片較大)。非 2xx / 連線失敗 → JiraForwardError
        (含 status + 截斷 body;**PAT 只在 header、絕不入例外/log**)。回應是 list,取第一個 filename。
        """
        headers = {
            "Authorization": f"Bearer {self._pat}",
            "Accept": "application/json",
            "X-Atlassian-Token": "no-check",
        }
        files = {"file": (filename, data, content_type)}
        try:
            async with httpx.AsyncClient(timeout=max(self._timeout, 30.0)) as client:
                resp = await client.post(
                    f"{self._base_url}/rest/api/2/issue/{external_key}/attachments",
                    headers=headers,
                    files=files,
                )
        except httpx.HTTPError as exc:
            raise JiraForwardError(
                f"jira attachment request failed: {type(exc).__name__}"
            ) from exc
        if resp.status_code >= 300:
            raise JiraForwardError(
                f"jira attachment failed: {resp.status_code} {self._tail(resp.text)}",
                status=resp.status_code,
            )
        body = resp.json()
        if not isinstance(body, list) or not body:
            raise JiraForwardError(
                "jira attachment returned no entries", status=resp.status_code
            )
        stored = body[0].get("filename")
        if not stored:
            raise JiraForwardError(
                "jira attachment returned no filename", status=resp.status_code
            )
        return str(stored)


def build_jira_forwarder(pat: str) -> JiraForwarder | None:
    """依 config + per-user PAT 建 live forwarder;未配置(base_url/project_key 缺)→ **None**。

    PAT 為 per-user(不能全域單例)→ 工廠採「呼叫時傳 PAT」形式,每次轉發依連結建立者的 PAT 建
    一個 forwarder。回 None = config 未齊 → 呼叫端誠實 fail(outbox 標 config-missing,不假成功)。
    測試以注入 fake forwarder factory 取代(不走此工廠;fakes 測試注入 pattern 保留)。
    """
    from cmms.config import get_settings

    s = get_settings()
    if not s.jira_forwarder_configured or not pat:
        return None
    return HttpJiraForwarder(
        base_url=s.jira_base_url,  # type: ignore[arg-type]  # configured 已保證非 None
        pat=pat,
        project_key=s.jira_mrq_project_key,  # type: ignore[arg-type]
        issue_type=s.jira_mrq_issue_type,
    )


_forwarder: JiraForwarder | None = None


def get_jira_forwarder() -> JiraForwarder:
    """回預設 no-op forwarder(NullJiraForwarder;mirror storage.py fallback）。

    ★ 保留供 legacy/測試;正式轉發路徑改用 `build_jira_forwarder(pat)`(per-user PAT）。
    """
    global _forwarder
    if _forwarder is None:
        _forwarder = NullJiraForwarder()
    return _forwarder
