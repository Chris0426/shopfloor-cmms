"""測試共用設定。

依 CLAUDE.md 的 Method 規則：整合測試使用 testcontainers 啟動真實 PostgreSQL，
「Docker 不存在時 skip」。套件已安裝但本機沒有 Docker daemon 時，fixture setup 會
直接炸成 error；這裡在收集階段偵測 Docker，不可用就把用到 PostgresContainer 的
測試標成 skip。判準是模組命名空間內有 `PostgresContainer`，不看檔名。
"""

from __future__ import annotations

import functools

import pytest


@functools.lru_cache(maxsize=1)
def _docker_available() -> bool:
    """偵測 Docker daemon 是否可連線（只偵測一次）。任何例外都視為不可用。"""
    try:
        import docker

        client = docker.from_env(timeout=2)
        try:
            return bool(client.ping())
        finally:
            client.close()
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    """Docker 不可用時，跳過所有使用 testcontainers PostgresContainer 的測試。"""
    skip = None
    for item in items:
        module = getattr(item, "module", None)
        if getattr(module, "PostgresContainer", None) is None:
            continue
        if _docker_available():
            # 同一次執行結果不變，不必再檢查其餘項目
            break
        if skip is None:
            skip = pytest.mark.skip(reason="Docker daemon not available (testcontainers)")
        item.add_marker(skip)


@pytest.fixture
def docker_daemon() -> None:
    """給「同一檔混合 DB 與非 DB 測試」的 fixture 依賴:Docker 不可用就 skip 該測試。

    模組層沒有 `PostgresContainer`(在 fixture 內才 import)時,上面的收集階段判準抓不到,
    由 DB fixture 宣告依賴本 fixture 來補。
    """
    if not _docker_available():
        pytest.skip("Docker daemon not available (testcontainers)")
