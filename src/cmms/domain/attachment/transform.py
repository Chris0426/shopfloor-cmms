"""媒體檔名 / key / content-type / hash 的純函式(無 DB、無 I/O,可單元測試)。

檔名慣例(data/media/README.md):
  <item_code> <caption>.<ext>   有 caption
  <item_code>.<ext>             無 caption
前導 token = item_code(轉大寫,對齊 inventory_item.item_code 全大寫 PK);其餘 = caption。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# 副檔名 → MIME;未知回退 octet-stream(未來 work_order 可加 pdf 等)。
CONTENT_TYPE_BY_EXT: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "pdf": "application/pdf",
}

# owner_type → R2 key 前綴(與 data/media 子夾對齊)。
OWNER_PREFIX: dict[str, str] = {
    "inventory_item": "inventory",
    "work_order": "work_order",
    "work_order_note": "work_order_note",  # 工單日誌逐筆照片(owner_id = note.id)
    "asset": "asset",
}


@dataclass(frozen=True, slots=True)
class ParsedMediaFile:
    item_code: str  # 前導 token,已轉大寫
    caption: str | None  # 其餘(無則 None)
    ext: str  # 小寫副檔名(無前導點)


def parse_media_filename(filename: str) -> ParsedMediaFile | None:
    """`ec000032 dispenser 04-a_2.jpg` → (EC000032, "dispenser 04-a_2", jpg)。

    `ec000811.png` → (EC000811, None, png)。無副檔名或空前導 token → None(loader 跳過 + log)。
    """
    name = filename.strip()
    if "." not in name:
        return None
    stem, ext = name.rsplit(".", 1)
    ext = ext.lower()
    parts = stem.split(" ", 1)
    item_code = parts[0].strip().upper()
    if not item_code:
        return None
    caption = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    return ParsedMediaFile(item_code=item_code, caption=caption, ext=ext)


def content_type_for(ext: str) -> str:
    return CONTENT_TYPE_BY_EXT.get(ext.lower(), "application/octet-stream")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_r2_key(owner_prefix: str, owner_id: str, sha256: str, ext: str) -> str:
    """組 R2 key:<owner_prefix>/<OWNER_ID>/<sha8>.<ext>(owner_id 大寫;content-addressed)。"""
    return f"{owner_prefix}/{owner_id.upper()}/{sha256[:8]}.{ext.lower()}"
