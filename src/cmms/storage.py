"""物件儲存抽象(R2,S3 相容)。app 程序的媒體上傳 / presign 走這裡(ADR-019)。

- StorageBackend:介面 Protocol(put / presign / delete / exists)。
- R2StorageBackend:boto3 S3 client + endpoint_url=R2(仿 infra/backup 的 S3 相容用法)。
  presign 純本地簽章(SigV4,無 I/O);put/delete 包 asyncio.to_thread 不阻塞 event loop。
- InMemoryStorageBackend:測試 / 本機無 R2 用的 fake(bytes 存 dict、回 memory:// 假 URL);
  隨 app 出貨(非僅測試),讓本機 / CI 無 R2 也能跑 loader 與測試。

★ 與 backup 區隔:backup 用 shell `aws` + bucket `db-backups`;本模組是 app 端、
  獨立 media bucket + 獨立最小權限 token(見 infra/secrets-manifest.md;不可重用 db-backup-rw)。
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Protocol, runtime_checkable

from cmms.config import get_settings

# 媒體設定預設值(Integrate 會在 cmms.config 加對應 CMMS_R2_* Field 供 env 覆寫;
# 此處以 getattr 安全消費 → config 尚未接線時不 AttributeError,接線後透明採用其值)。
DEFAULT_MEDIA_BUCKET = "cmms-media"
DEFAULT_URL_TTL_SECONDS = 900


def media_bucket() -> str:
    """媒體 bucket 名(CMMS_R2_MEDIA_BUCKET 覆寫;未配置 → 預設 cmms-media)。"""
    return getattr(get_settings(), "r2_media_bucket", None) or DEFAULT_MEDIA_BUCKET


def url_ttl_seconds() -> int:
    """presigned GET 預設 TTL 秒(CMMS_ATTACHMENT_URL_TTL_SECONDS 覆寫;預設 900)。"""
    return getattr(get_settings(), "attachment_url_ttl_seconds", None) or DEFAULT_URL_TTL_SECONDS


class StorageObjectNotFound(Exception):
    """下載時物件不存在(get_object)。呼叫端可據此誠實標記失敗,不假成功。"""


@runtime_checkable
class StorageBackend(Protocol):
    async def put_object(
        self, *, bucket: str, key: str, data: bytes, content_type: str
    ) -> None: ...

    def presigned_get_url(self, *, bucket: str, key: str, ttl_seconds: int) -> str: ...

    async def get_object(self, *, bucket: str, key: str) -> bytes: ...

    async def delete_object(self, *, bucket: str, key: str) -> None: ...

    async def object_exists(self, *, bucket: str, key: str) -> bool: ...


class R2StorageBackend:
    """真實 R2(Cloudflare S3 相容)。憑證由 cmms.config 注入(CMMS_R2_*)。"""

    def __init__(self) -> None:
        import boto3  # 延遲 import:未用媒體切片不付啟動成本 / 不強制安裝 boto3

        s = get_settings()
        self._client = boto3.client(
            "s3",
            endpoint_url=getattr(s, "r2_endpoint", None),
            aws_access_key_id=getattr(s, "r2_access_key_id", None),
            aws_secret_access_key=getattr(s, "r2_secret_access_key", None),
            region_name="auto",  # R2 慣例
        )

    async def put_object(self, *, bucket: str, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    def presigned_get_url(self, *, bucket: str, key: str, ttl_seconds: int) -> str:
        # 純本地 SigV4 簽章,無網路;故不需 to_thread。
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=ttl_seconds
        )

    async def get_object(self, *, bucket: str, key: str) -> bytes:
        def _get() -> bytes:
            from botocore.exceptions import ClientError

            try:
                resp = self._client.get_object(Bucket=bucket, Key=key)
            except ClientError as exc:  # NoSuchKey / 403 → 誠實轉譯,不假成功
                raise StorageObjectNotFound(f"{bucket}/{key}") from exc
            return resp["Body"].read()

        return await asyncio.to_thread(_get)

    async def delete_object(self, *, bucket: str, key: str) -> None:
        await asyncio.to_thread(self._client.delete_object, Bucket=bucket, Key=key)

    async def object_exists(self, *, bucket: str, key: str) -> bool:
        def _head() -> bool:
            from botocore.exceptions import ClientError

            try:
                self._client.head_object(Bucket=bucket, Key=key)
                return True
            except ClientError:
                return False

        return await asyncio.to_thread(_head)


class InMemoryStorageBackend:
    """測試 / 本機 fake。bytes 存記憶體;presign 回可辨識的 memory:// 假 URL。"""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}

    async def put_object(self, *, bucket: str, key: str, data: bytes, content_type: str) -> None:
        self.objects[(bucket, key)] = (data, content_type)

    def presigned_get_url(self, *, bucket: str, key: str, ttl_seconds: int) -> str:
        return f"memory://{bucket}/{key}?ttl={ttl_seconds}"

    async def get_object(self, *, bucket: str, key: str) -> bytes:
        try:
            return self.objects[(bucket, key)][0]
        except KeyError as exc:
            raise StorageObjectNotFound(f"{bucket}/{key}") from exc

    async def delete_object(self, *, bucket: str, key: str) -> None:
        self.objects.pop((bucket, key), None)

    async def object_exists(self, *, bucket: str, key: str) -> bool:
        return (bucket, key) in self.objects


@lru_cache
def get_storage_backend() -> StorageBackend:
    """單例(比照 get_settings / get_sessionmaker)。未配置 R2 → 回 InMemory(本機 / CI 友善)。

    以 getattr 安全讀取(config 的 CMMS_R2_* Field 由 Integrate 接線;未接線時三者皆 None →
    優雅退回 InMemory,不 AttributeError)。
    """
    s = get_settings()
    endpoint = getattr(s, "r2_endpoint", None)
    access_key = getattr(s, "r2_access_key_id", None)
    secret_key = getattr(s, "r2_secret_access_key", None)
    if endpoint and access_key and secret_key:
        return R2StorageBackend()
    return InMemoryStorageBackend()
