"""Scaffold 冒煙測試:確認骨架可 import、API/CLI 可起、稽核慣例正確。

切片進來後,各自加 migration + domain service + API + MCP + tests。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from cmms import __version__
from cmms.api.app import app
from cmms.audit import Actor


def test_health() -> None:
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": __version__}


def test_actor_string_conventions() -> None:
    # source_actor / proposed_by / confirmed_by 的字串慣例(ADR-005/016)
    assert Actor.human("jordan.lee").value == "human:jordan.lee"
    assert Actor.agent("analytics").value == "agent:analytics"
    assert Actor.mes_pipeline().value == "mes-pipeline"
    assert Actor.agent("analytics").is_agent()
    assert Actor.human("x").is_human()


def test_asset_models_registered() -> None:
    # Asset 切片後,model 已收錄進 metadata(Alembic 以此產 migration)
    from cmms.db import Base

    tables = set(Base.metadata.tables)
    assert {"asset", "asset_external_id", "asset_type", "department", "line"} <= tables


def test_task_models_registered() -> None:
    # Task 切片後,task 表已收錄進 metadata
    import cmms.domain.task.models  # noqa: F401  確保 model 已 import
    from cmms.db import Base

    assert "task" in set(Base.metadata.tables)


def test_pm_schedule_models_registered() -> None:
    # ScheduledActivity 切片後,pm_schedule + lookups 已收錄進 metadata
    import cmms.domain.pm_schedule.models  # noqa: F401
    from cmms.db import Base

    assert {"pm_schedule", "freq_unit", "vendor"} <= set(Base.metadata.tables)


def test_work_order_models_registered() -> None:
    # WorkOrder 切片後,work_order + lookups 已收錄進 metadata
    import cmms.domain.work_order.models  # noqa: F401
    from cmms.db import Base

    assert {"work_order", "work_type", "wo_status"} <= set(Base.metadata.tables)


def test_inventory_models_registered() -> None:
    # Inventory 切片後,inventory_item + lookups + junctions 已收錄進 metadata
    import cmms.domain.inventory.models  # noqa: F401
    from cmms.db import Base

    assert {
        "inventory_item",
        "item_category",
        "asset_subtype",
        "inventory_item_asset_subtype",
        "inventory_item_alternative",
        "inventory_item_kit",
    } <= set(Base.metadata.tables)
