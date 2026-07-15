"""Alembic 環境(async)。

- DSN 從 `CMMS_DATABASE_URL` 取(經 cmms.config),不寫死於 alembic.ini。
- target_metadata = cmms.db.Base.metadata;各切片的 ORM model 必須在下方 import,
  autogenerate 才看得到它們。目前為空 baseline。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from cmms.config import get_settings
from cmms.db import Base
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401

# 切片 model 在此 import(讓 Base.metadata 收錄)
from cmms.domain.assistant import models as _assistant_models  # noqa: E402, F401
from cmms.domain.attachment import models as _attachment_models  # noqa: E402, F401
from cmms.domain.contacts import models as _contacts_models  # noqa: E402, F401
from cmms.domain.failure_vocab import models as _failure_vocab_models  # noqa: E402, F401
from cmms.domain.feedback import models as _feedback_models  # noqa: E402, F401
from cmms.domain.identity import models as _identity_models  # noqa: E402, F401
from cmms.domain.inventory import models as _inventory_models  # noqa: E402, F401
from cmms.domain.notify import models as _notify_models  # noqa: E402, F401
from cmms.domain.pm_schedule import models as _pm_schedule_models  # noqa: E402, F401
from cmms.domain.procurement import models as _procurement_models  # noqa: E402, F401
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.telegram_bridge import models as _telegram_bridge_models  # noqa: E402, F401
from cmms.domain.work_order import models as _work_order_models  # noqa: E402, F401

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
