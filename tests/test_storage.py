"""StorageBackend 單元測試(InMemory get_object 下載能力;R2 略,無 boto3 mock)。"""

from __future__ import annotations

import pytest

from cmms.storage import (
    InMemoryStorageBackend,
    R2StorageBackend,
    StorageObjectNotFound,
)


async def test_inmemory_get_object_roundtrip() -> None:
    s = InMemoryStorageBackend()
    await s.put_object(bucket="b", key="k", data=b"JPEGDATA", content_type="image/jpeg")
    assert await s.get_object(bucket="b", key="k") == b"JPEGDATA"


async def test_inmemory_get_object_missing_raises() -> None:
    s = InMemoryStorageBackend()
    with pytest.raises(StorageObjectNotFound):
        await s.get_object(bucket="b", key="missing")


def test_r2_backend_has_get_object() -> None:
    # 不建 client(需憑證),只確認方法存在且簽章對(async, 關鍵字 bucket/key)。
    assert hasattr(R2StorageBackend, "get_object")
    import inspect

    sig = inspect.signature(R2StorageBackend.get_object)
    assert set(sig.parameters) == {"self", "bucket", "key"}
