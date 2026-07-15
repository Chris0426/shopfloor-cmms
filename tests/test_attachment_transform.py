"""Attachment 純函式 + InMemory storage backend 測試(無 DB、無 config,本機可跑)。"""

from __future__ import annotations

from cmms.domain.attachment.transform import (
    OWNER_PREFIX,
    content_type_for,
    make_r2_key,
    parse_media_filename,
    sha256_hex,
)
from cmms.storage import InMemoryStorageBackend, media_bucket, url_ttl_seconds


def test_parse_with_caption() -> None:
    p = parse_media_filename("ec000032 dispenser 04-a_2.jpg")
    assert p is not None
    assert p.item_code == "EC000032"
    assert p.caption == "dispenser 04-a_2"
    assert p.ext == "jpg"


def test_parse_without_caption() -> None:
    p = parse_media_filename("ec000811.png")
    assert p is not None
    assert (p.item_code, p.caption, p.ext) == ("EC000811", None, "png")


def test_parse_uppercases_owner_and_lowercases_ext() -> None:
    p = parse_media_filename("es000001 Foo Bar.JPG")
    assert p is not None
    assert p.item_code == "ES000001"  # 前導 token 轉大寫(對齊 item_code PK)
    assert p.ext == "jpg"  # 副檔名轉小寫
    assert p.caption == "Foo Bar"  # caption 原樣(僅去頭尾空白)


def test_parse_multi_space_caption_preserved() -> None:
    p = parse_media_filename("ec000003 encap machine sta6 flex attach.jpg")
    assert p is not None
    assert p.caption == "encap machine sta6 flex attach"


def test_parse_no_extension_returns_none() -> None:
    assert parse_media_filename("ec000001") is None


def test_parse_empty_leading_token_returns_none() -> None:
    assert parse_media_filename(" .jpg") is None  # 前導 token 空 → None


def test_content_type_for() -> None:
    assert content_type_for("jpg") == "image/jpeg"
    assert content_type_for("JPEG") == "image/jpeg"
    assert content_type_for("png") == "image/png"
    assert content_type_for("xyz") == "application/octet-stream"  # 未知回退


def test_sha256_hex_stable() -> None:
    a = sha256_hex(b"hello")
    b = sha256_hex(b"hello")
    c = sha256_hex(b"world")
    assert a == b and a != c
    assert len(a) == 64


def test_make_r2_key_content_addressed() -> None:
    sha = sha256_hex(b"img-bytes")
    key = make_r2_key(OWNER_PREFIX["inventory_item"], "ec000001", sha, "jpg")
    assert key == f"inventory/EC000001/{sha[:8]}.jpg"  # <prefix>/<OWNER_ID>/<sha8>.<ext>


def test_owner_prefix_map() -> None:
    assert OWNER_PREFIX == {
        "inventory_item": "inventory",
        "work_order": "work_order",
        "work_order_note": "work_order_note",  # 工單日誌逐筆照片(§1.6,migration 0012)
        "asset": "asset",
    }


def test_media_config_defaults_without_config_wiring() -> None:
    # config 的 CMMS_R2_* Field 由 Integrate 接線;未接線時安全退回設計預設值。
    assert media_bucket() == "cmms-media"
    assert url_ttl_seconds() == 900


async def test_inmemory_backend_roundtrip() -> None:
    backend = InMemoryStorageBackend()
    await backend.put_object(bucket="b", key="k", data=b"xyz", content_type="image/png")
    assert await backend.object_exists(bucket="b", key="k") is True
    assert backend.objects[("b", "k")] == (b"xyz", "image/png")

    url = backend.presigned_get_url(bucket="b", key="k", ttl_seconds=900)
    assert url == "memory://b/k?ttl=900"  # 可辨識的假 URL,純本地

    await backend.delete_object(bucket="b", key="k")
    assert await backend.object_exists(bucket="b", key="k") is False
