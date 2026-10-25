"""SeedReplayService — 把 `tendline.seed.v1.1` 種子帳經既有 domain service 重播進本 instance。

本服務**不直接寫業務表**:資產 / 料號 / 工單 / 日誌 / 領料一律呼叫各切片的 governed 操作
(`AssetService.create_asset`、`InventoryService.create_item` + `adjust_on_hand`、
`WorkOrderService.open_work_order` / 狀態機 / `add_note` / `issue_part_to_work_order` /
`set_seed_field` / `set_confirmed_reason`),稽核欄由那些操作照常填(actor = 合成帳號
`human:synth.*`)。本服務自己只寫 `seed_world`(指紋 + 進度)。

交易粒度 = **一事件一交易**。理由:各 domain 操作自帶 `write()` 交易並在結束時 commit,包不進
更大的交易;因此把 `seed_world.last_seq` 的推進**先掛在同一 session 上**,讓該事件的 domain
commit 一併帶走 → 事件落地與進度推進原子一致,中斷後從 `last_seq + 1` 續跑不重放。事件被
domain 拒絕時 rollback、記行級錯誤,進度仍推進(該事件已「消化」,重跑也只會再被拒)。
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from cmms.audit import Actor
from cmms.domain.asset.models import AssetType, Line
from cmms.domain.asset.service import AssetError, AssetService
from cmms.domain.base import DomainService
from cmms.domain.identity.models import UserAccount
from cmms.domain.identity.service import AuthorizationError, IdentityError, IdentityService
from cmms.domain.inventory.models import StockTxnKind
from cmms.domain.inventory.service import InventoryError, InventoryService
from cmms.domain.seed.models import SeedWorld
from cmms.domain.seed.reader import Manifest, ReadResult, SeedEvent, SeedRejected
from cmms.domain.seed.transform import (
    ACTOR_MAP,
    Plan,
    Replayer,
    Tally,
    tally_plan,
    to_utc_z,
    window,
)
from cmms.domain.work_order.loader import (
    STOCK_TXN_KIND_LABELS,
    WO_HOLD_REASON_SEED,
    WO_STATUS_SEED,
)
from cmms.domain.work_order.models import WorkType
from cmms.domain.work_order.service import InvalidTransition, WorkOrderError, WorkOrderService

log = logging.getLogger("cmms.seed")

# 合成帳號 / 受控詞彙的建立者(CLI 維運者 bootstrap provenance,同 `cmms user-create`)
PROVISION_ACTOR = Actor.human("cli")
_DOMAIN_ERRORS = (WorkOrderError, AssetError, InventoryError, IdentityError)


@dataclass(slots=True)
class ReplayOutcome:
    manifest: Manifest
    until: datetime
    resumed_from_seq: int
    tally: Tally

    def summary(self) -> dict[str, Any]:
        return build_summary(self.manifest, self.until, self.resumed_from_seq, self.tally)


def build_summary(
    manifest: Manifest, until: datetime, resumed_from_seq: int, t: Tally
) -> dict[str, Any]:
    """CLI 輸出的單行 JSON 內容(dry-run 與實跑同形)。"""
    return {
        "schema": manifest.schema,
        "world_id": manifest.world_id,
        "generator_version": manifest.generator_version,
        "fleet_digest": manifest.fleet_digest,
        "horizon_start": manifest.horizon_start,
        "applied_until": to_utc_z(until),
        "events_applied": t.events_applied,
        "events_resumed_from_seq": resumed_from_seq,
        "skipped_kinds": dict(sorted(t.skipped_kinds.items())),
        "skipped_features": dict(sorted(t.skipped_features.items())),
        "line_errors": t.line_errors,
        "errors_by_code": dict(sorted(t.errors_by_code.items())),
        "warnings": dict(sorted(t.warnings.items())),
    }


def reader_errors_in_scope(read: ReadResult, after_seq: int) -> list[dict[str, Any]]:
    """讀取層錯誤中,未被前次重播消化的那些(seq 未知者一律保留)。"""
    return [e for e in read.errors if e["seq"] is None or e["seq"] > after_seq]


def log_line_error(rec: Mapping[str, Any]) -> None:
    log.warning(
        "seed line error %s",
        {"code": rec["code"], "seq": rec["seq"], "line": rec["line"], "detail": rec["detail"]},
    )


class SeedReplayService(DomainService):
    def __init__(self, session, *, area_map: Mapping[str, str]) -> None:
        super().__init__(session)
        self.area_map = dict(area_map)
        self._actors: dict[str, Actor] = {}

    # ---- 指紋 / 進度 ----

    async def get_world(self, world_id: str) -> SeedWorld | None:
        return await self.session.get(SeedWorld, world_id)

    async def check_world(self, manifest: Manifest, until: datetime) -> SeedWorld | None:
        """續讀前置:指紋不符 → state_mismatch;cut-off 倒退 → until_regressed。"""
        sw = await self.get_world(manifest.world_id)
        if sw is None:
            return None
        if sw.fingerprint() != manifest.fingerprint():
            raise SeedRejected(
                "state_mismatch",
                f"manifest fingerprint {manifest.fingerprint()} != stored {sw.fingerprint()}",
            )
        if sw.applied_until is not None and until < sw.applied_until:
            raise SeedRejected(
                "until_regressed",
                f"--until {to_utc_z(until)} < applied_until {to_utc_z(sw.applied_until)}",
            )
        return sw

    # ---- bootstrap ----

    async def _bootstrap_vocab(self) -> None:
        """工單 / 庫存 / 產線受控詞彙(idempotent,do-nothing upsert;既有值不覆寫)。
        全新 instance 沒跑過 legacy 載入器時,這些 lookup 是空的。"""
        wo = WorkOrderService(self.session)
        asset = AssetService(self.session)
        async with wo.write(PROVISION_ACTOR):
            for code in ("PM", "REACTIVE"):
                await wo.upsert_lookup(WorkType, code, code)
            for code, label, rank, is_terminal, is_downtime in WO_STATUS_SEED:
                await wo.upsert_status(
                    code, label, rank=rank, is_terminal=is_terminal, is_downtime=is_downtime
                )
            for code, label, is_downtime in WO_HOLD_REASON_SEED:
                await wo.upsert_hold_reason(code, label, is_downtime=is_downtime)
            for code, label in STOCK_TXN_KIND_LABELS.items():
                await wo.upsert_lookup(StockTxnKind, code, label)
            for line in sorted(set(self.area_map.values())):
                await asset.upsert_lookup(Line, line, line)

    async def _actor(self, role: str) -> Actor:
        """seed 角色 → 合成帳號 actor(首次使用時經 IdentityService 建帳;密碼隨機、不落地)。"""
        if role in self._actors:
            return self._actors[role]
        username, cmms_role = ACTOR_MAP[role]
        if await self.session.get(UserAccount, username) is None:
            await IdentityService(self.session).create_user(
                user_id=username,
                username=username,
                display_name=f"Synthetic {role.split(':', 1)[1]}",
                password=secrets.token_urlsafe(24),  # 無人登入此帳號;不印不記
                org="plant",
                role=cmms_role,
                actor=PROVISION_ACTOR,
            )
        actor = Actor.human(username)
        self._actors[role] = actor
        return actor

    # ---- 重播 ----

    async def replay(
        self, manifest: Manifest, read: ReadResult, *, until: datetime
    ) -> ReplayOutcome:
        sw = await self.check_world(manifest, until)
        after_seq = sw.last_seq if sw is not None else 0
        await self._bootstrap_vocab()
        if sw is None:
            fp = manifest.fingerprint()
            sw = SeedWorld(**fp, last_seq=0)
            self.session.add(sw)
            await self.session.commit()
        maps = await WorkOrderService(self.session).downtime_lookup_maps()
        rp = Replayer(self.area_map, downtime_maps=maps)
        t = Tally()
        for rec in reader_errors_in_scope(read, after_seq):
            t.error(rec["code"], rec["seq"], rec["line"], rec["detail"])
            log_line_error(rec)

        # 續跑:前次已消化的事件只推進預檢狀態(不重放、不計數)
        for ev in read.events:
            if ev.seq > after_seq:
                break
            if ev.kind != "asset.define":
                rp.take_links()
            plan = rp.plan(ev)
            if plan.op:
                rp.confirm(plan)

        for ev in window(read.events, after_seq=after_seq, until=until):
            if ev.kind != "asset.define":
                await self._flush_links(rp, t, manifest.world_id)
            await self._step(rp, t, ev, manifest.world_id)
        await self._flush_links(rp, t, manifest.world_id)

        sw = await self.get_world(manifest.world_id)
        assert sw is not None
        sw.applied_until = until
        sw.updated_at = datetime.now(UTC)
        await self.session.commit()
        return ReplayOutcome(manifest, until, after_seq + 1, t)

    async def _stage_progress(self, world_id: str, seq: int) -> None:
        """把進度推進掛在 session 上,由接下來的 domain commit 一併帶走(見模組 docstring)。"""
        sw = await self.get_world(world_id)
        assert sw is not None
        sw.last_seq = seq
        sw.updated_at = datetime.now(UTC)

    async def _step(self, rp: Replayer, t: Tally, ev: SeedEvent, world_id: str) -> None:
        plan = rp.plan(ev)
        applied = False
        if plan.op:
            try:
                actor = await self._actor(ev.actor)  # 建帳自帶 commit → 先建、後掛進度
                await self._execute(plan, actor, world_id)
                applied = True
                rp.confirm(plan)
            except AuthorizationError as e:
                await self._fail(plan, "forbidden", str(e), world_id)
            except InvalidTransition as e:
                await self._fail(plan, "invalid_transition", str(e), world_id)
            except _DOMAIN_ERRORS as e:
                await self._fail(plan, "domain_rejected", str(e), world_id)
            except IntegrityError as e:
                await self._fail(plan, "integrity_error", str(e.orig), world_id)
        else:
            await self._stage_progress(world_id, ev.seq)
        tally_plan(t, plan, applied=applied)
        for code, detail in plan.errors:
            log_line_error({"code": code, "seq": ev.seq, "line": ev.line, "detail": detail})
        await self.session.commit()  # 無 domain commit 的路徑(跳過 / 預檢丟棄 / no-op)也推進進度

    async def _fail(self, plan: Plan, code: str, detail: str, world_id: str) -> None:
        await self.session.rollback()
        plan.errors.append((code, detail))
        await self._stage_progress(world_id, plan.event.seq)

    async def _flush_links(self, rp: Replayer, t: Tally, world_id: str) -> None:
        links = rp.take_links()
        if not links:
            return
        actor = await self._actor("role:system")
        asset = AssetService(self.session)
        for lk in links:
            err = lk.error
            if err is None:
                try:
                    async with asset.write(actor):
                        await asset.link_containment(lk.parent, lk.child, actor)
                except (AssetError, IntegrityError) as e:
                    err = ("containment_rejected", str(e))
            if err is not None:
                t.error(err[0], lk.seq, lk.line, err[1])
                log_line_error({"code": err[0], "seq": lk.seq, "line": lk.line, "detail": err[1]})

    async def _execute(self, plan: Plan, actor: Actor, world_id: str) -> None:
        """執行一個 plan。進度推進在**最後一個** domain commit 之前掛上(多步操作的前幾步若成功、
        最後一步前中斷,續跑會重送該事件並由 domain 拒為重複 → 記錯,不靜默重做)。"""
        op, a, ev = plan.op, plan.args, plan.event
        idem = f"seed:{world_id}:{ev.seq}"
        wo = WorkOrderService(self.session)
        if op == "asset.create":
            asset = AssetService(self.session)
            async with asset.write(actor):  # station_cls 動態種為 asset_type(label 先 = code)
                await asset.upsert_lookup(AssetType, a["asset_type"], a["asset_type"])
            await self._stage_progress(world_id, ev.seq)
            await asset.create_asset(
                a["asset_id"],
                actor=actor,
                description=a["description"],
                asset_type=a["asset_type"],
                line=a["line"],
            )
            return
        if op == "item.create":
            inv = InventoryService(self.session)
            await inv.create_item(a["item_code"], actor=actor, name=a["name"])
            await self._stage_progress(world_id, ev.seq)
            await inv.adjust_on_hand(
                a["item_code"],
                new_quantity=a["on_hand"],
                reason=f"seed world {world_id}: initial on-hand",
                actor=actor,
                idempotency_key=f"{idem}:onhand",
                occurred_at=ev.at,
            )
            return
        await self._stage_progress(world_id, ev.seq)
        if op == "wo.open":
            await wo.open_work_order(
                asset_id=a["asset_id"],
                work_type=a["work_type"],
                actor=actor,
                brief_description=a["brief_description"],
                work_order_no=a["work_order_no"],
                at=ev.at,
                failure_started_at=a["failure_started_at"],
            )
        elif op == "wo.start":
            await wo.start_work(a["work_order_no"], actor, at=ev.at)
        elif op == "wo.resume":
            await wo.resume_work(a["work_order_no"], actor, at=ev.at)
        elif op == "wo.hold":
            await wo.hold_work(a["work_order_no"], a["hold_reason"], actor, at=ev.at)
        elif op == "wo.complete":
            await wo.complete_work(a["work_order_no"], actor, at=ev.at)
        elif op == "wo.close":
            await wo.close_work_order(a["work_order_no"], actor, at=ev.at)
        elif op == "wo.void":
            await wo.void_work_order(a["work_order_no"], actor, at=ev.at)
        elif op == "wo.cancel":
            await wo.cancel_reactive_report(a["work_order_no"], actor, at=ev.at)
        elif op == "wo.note":
            await wo.add_note(
                a["work_order_no"],
                entry_type=a["entry_type"],
                body=a["body"],
                actor=actor,
                occurred_at=ev.at,
                idempotency_key=idem,
            )
        elif op == "wo.part":
            await wo.issue_part_to_work_order(
                work_order_no=a["work_order_no"],
                item_code=a["item_code"],
                quantity=a["quantity"],
                actor=actor,
                at=ev.at,
                idempotency_key=idem,
            )
        elif op == "wo.field":
            await wo.set_seed_field(
                a["work_order_no"], a["field"], a["value"], actor=actor, at=ev.at
            )
        elif op == "wo.reason":
            await wo.set_confirmed_reason(a["work_order_no"], code=a["code"], actor=actor)
        else:  # pragma: no cover - Replayer 只產上列 op
            raise AssertionError(f"unknown op {op}")
