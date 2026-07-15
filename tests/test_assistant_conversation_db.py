"""AssistantService DB 測試(ADR-020 對話落 DB;testcontainers)。無 Docker 自動 skip。

驗:兩段式送出(add_user_message phase 1 落 user + 惰性建會話 / add_assistant_message
phase 2 落 assistant)/ 擁有權隔離(A 拿不到 B 的對話與訊息、不能續寫)/ close /
開啟中上限 / recent_history 裁輪 + before_message_id 排除當輪 user 訊息 /
next_message_after 冪等查詢。assistant FK → user_account,故 create_all 需連 identity。
"""

from __future__ import annotations

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.assistant import models as _assistant_models  # noqa: E402, F401
from cmms.domain.assistant.service import AssistantError, AssistantService  # noqa: E402
from cmms.domain.identity import models as _identity_models  # noqa: E402, F401
from cmms.domain.identity.service import IdentityService  # noqa: E402


@pytest.fixture
async def session():
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            for uid in ("alice", "bob"):
                await IdentityService(s).create_user(
                    user_id=uid, username=uid, display_name=uid.title(),
                    password="password8", org="plant", actor=Actor.human("cli"),
                )
            yield s
        await engine.dispose()


async def _turn(svc: AssistantService, *, user_id, conversation_id, u, a):
    """完整一輪(phase 1 落 user + phase 2 落 assistant),回對話。conversation_id=None → 惰性建。"""
    conv, _ = await svc.add_user_message(
        user_id=user_id, conversation_id=conversation_id, content=u, actor=Actor.human(user_id)
    )
    await svc.add_assistant_message(
        user_id=user_id, conversation_id=conv.id, content=a, actor=Actor.human(user_id)
    )
    return conv


async def test_two_phase_lazy_creates_and_persists(session) -> None:
    svc = AssistantService(session)
    # phase 1:落 user 訊息 + 惰性建會話(尚無 assistant)
    conv, umsg = await svc.add_user_message(
        user_id="alice", conversation_id=None,
        content="pump on line 2 is leaking coolant everywhere help",
        actor=Actor.human("alice"),
    )
    assert conv.id is not None
    assert umsg.id is not None and umsg.role == "user"
    assert conv.title.startswith("pump on line 2")            # 標題 = 首則訊息截短
    msgs = await svc.get_messages("alice", conv.id)
    assert [m.role for m in msgs] == ["user"]                 # phase 1 只有 user
    # phase 2:gateway 成功後落 assistant
    amsg = await svc.add_assistant_message(
        user_id="alice", conversation_id=conv.id,
        content="I found EID-70021.", actor=Actor.human("alice"),
    )
    assert amsg.role == "assistant"
    msgs = await svc.get_messages("alice", conv.id)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].content == "I found EID-70021."
    assert [c.id for c in await svc.list_open_conversations("alice")] == [conv.id]


async def test_ownership_isolation(session) -> None:
    svc = AssistantService(session)
    conv = await _turn(
        svc, user_id="alice", conversation_id=None, u="secret alice thing", a="ok"
    )
    cid = conv.id  # 先抓 id:bob 的失敗寫入會 rollback → expire 掉 conv 物件
    # bob 看不到 alice 的對話 / 訊息
    assert await svc.get_conversation("bob", cid) is None
    assert await svc.get_messages("bob", cid) == []
    assert await svc.recent_history("bob", cid) == []
    assert await svc.list_open_conversations("bob") == []
    # bob 不能對 alice 的對話續寫(phase 1 / phase 2 皆拒)
    with pytest.raises(AssistantError):
        await svc.add_user_message(
            user_id="bob", conversation_id=cid, content="inject", actor=Actor.human("bob")
        )
    with pytest.raises(AssistantError):
        await svc.add_assistant_message(
            user_id="bob", conversation_id=cid, content="x", actor=Actor.human("bob")
        )
    # bob 不能結束 alice 的對話
    with pytest.raises(AssistantError):
        await svc.close_conversation("bob", cid, Actor.human("bob"))
    # 皆未污染:alice 的對話仍 2 則、仍開啟
    assert len(await svc.get_messages("alice", cid)) == 2
    assert (await svc.get_conversation("alice", cid)).closed_at is None


async def test_close_removes_from_open_and_blocks_add(session) -> None:
    svc = AssistantService(session)
    conv = await _turn(svc, user_id="alice", conversation_id=None, u="q1", a="a1")
    cid = conv.id  # 先抓 id:續寫已結束對話的失敗寫入會 rollback → expire 物件
    await svc.close_conversation("alice", cid, Actor.human("alice"))
    assert (await svc.get_conversation("alice", cid)).closed_at is not None
    assert await svc.list_open_conversations("alice") == []
    # 已結束對話不可續寫(user 與 assistant 皆拒)
    with pytest.raises(AssistantError):
        await svc.add_user_message(
            user_id="alice", conversation_id=cid, content="q2", actor=Actor.human("alice")
        )
    with pytest.raises(AssistantError):
        await svc.add_assistant_message(
            user_id="alice", conversation_id=cid, content="a2", actor=Actor.human("alice")
        )
    # close 冪等:再關 no-op(不 raise)
    await svc.close_conversation("alice", cid, Actor.human("alice"))


async def test_open_conversation_limit(session) -> None:
    svc = AssistantService(session)
    for i in range(svc.MAX_OPEN_CONVERSATIONS):
        await svc.add_user_message(
            user_id="alice", conversation_id=None, content=f"chat {i}",
            actor=Actor.human("alice"),
        )
    assert await svc.count_open_conversations("alice") == svc.MAX_OPEN_CONVERSATIONS
    # 第 N+1 個新對話 → 惰性建路徑拒建(phase 1 守門)
    with pytest.raises(AssistantError):
        await svc.add_user_message(
            user_id="alice", conversation_id=None, content="one too many",
            actor=Actor.human("alice"),
        )
    # 結束一個後可再開
    open_ids = [c.id for c in await svc.list_open_conversations("alice")]
    await svc.close_conversation("alice", open_ids[0], Actor.human("alice"))
    conv, _ = await svc.add_user_message(
        user_id="alice", conversation_id=None, content="now ok", actor=Actor.human("alice")
    )
    assert conv.id is not None


async def test_recent_history_caps_turns(session) -> None:
    svc = AssistantService(session)
    conv = await _turn(svc, user_id="alice", conversation_id=None, u="t0", a="a0")
    for i in range(1, 12):  # 再加 11 輪 → 共 12 輪
        await _turn(svc, user_id="alice", conversation_id=conv.id, u=f"t{i}", a=f"a{i}")
    hist = await svc.recent_history("alice", conv.id, max_turns=8)
    assert len(hist) == 16                          # 8 輪 × (user+assistant)
    assert hist[0]["role"] == "user"
    assert hist[-1] == {"role": "assistant", "content": "a11"}   # 最新輪
    assert {"role": "user", "content": "t0"} not in hist         # 最舊被裁掉


async def test_recent_history_before_message_id_excludes_current_turn(session) -> None:
    """phase 2 組 gateway 歷史時,before_message_id 排除當輪 user 訊息(避免與 message 參數重複)。"""
    svc = AssistantService(session)
    conv = await _turn(svc, user_id="alice", conversation_id=None, u="q1", a="a1")
    # 新一輪 phase 1:落 user 訊息(尚無 assistant)
    _, umsg = await svc.add_user_message(
        user_id="alice", conversation_id=conv.id, content="q2 current", actor=Actor.human("alice")
    )
    # 不帶 before → 含當輪 user 訊息
    full = await svc.recent_history("alice", conv.id)
    assert {"role": "user", "content": "q2 current"} in full
    # 帶 before=當輪 user id → 排除它(與其後皆不取),只留較早的 q1/a1
    hist = await svc.recent_history("alice", conv.id, before_message_id=umsg.id)
    assert {"role": "user", "content": "q2 current"} not in hist
    assert {"role": "user", "content": "q1"} in hist
    assert {"role": "assistant", "content": "a1"} in hist


async def test_next_message_after_and_get_message_scoped(session) -> None:
    """next_message_after 供 phase 2 冪等:user 訊息之後第一則 = assistant 回覆;擁有權隔離。"""
    svc = AssistantService(session)
    conv, umsg = await svc.add_user_message(
        user_id="alice", conversation_id=None, content="q", actor=Actor.human("alice")
    )
    # 尚無後續 → None(phase 2 會真的打 gateway)
    assert await svc.next_message_after("alice", conv.id, umsg.id) is None
    amsg = await svc.add_assistant_message(
        user_id="alice", conversation_id=conv.id, content="a", actor=Actor.human("alice")
    )
    nxt = await svc.next_message_after("alice", conv.id, umsg.id)
    assert nxt is not None and nxt.id == amsg.id and nxt.role == "assistant"
    # get_message 擁有權隔離:bob 拿不到 alice 的訊息
    assert await svc.get_message("alice", umsg.id) is not None
    assert await svc.get_message("bob", umsg.id) is None
    assert await svc.next_message_after("bob", conv.id, umsg.id) is None
