"""seed ORM models(對應 migration 0037)。

- `SeedWorld`:每個已重播世界一列。指紋(STATE_FINGERPRINT_KEYS 五鍵,字串原樣存以利逐字比對)
  + 進度(`last_seq` / `applied_until`)。只由 `SeedReplayService` 寫入。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from cmms.db import Base


class SeedWorld(Base):
    __tablename__ = "seed_world"

    world_id: Mapped[str] = mapped_column(String, primary_key=True)
    schema: Mapped[str] = mapped_column(String, nullable=False)
    generator_version: Mapped[str] = mapped_column(String, nullable=False)
    fleet_digest: Mapped[str] = mapped_column(String, nullable=False)
    # manifest 原字串(UTC Z);指紋比對逐字比,不轉型
    horizon_start: Mapped[str] = mapped_column(String, nullable=False)
    applied_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def fingerprint(self) -> dict[str, str]:
        return {
            "schema": self.schema,
            "world_id": self.world_id,
            "generator_version": self.generator_version,
            "fleet_digest": self.fleet_digest,
            "horizon_start": self.horizon_start,
        }
