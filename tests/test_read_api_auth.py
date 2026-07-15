"""讀取 JSON API static bearer token middleware 測試(峰會裁決 消費端需求;免 Docker,永遠跑)。

`get_settings` 是 lru_cache → 改環境變數後必 `cache_clear()`,並在 teardown 還原(不污染其他測試)。
env_prefix=CMMS_:token = CMMS_READ_API_TOKEN、app_env = CMMS_APP_ENV。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cmms.api.app import app
from cmms.config import get_settings

# raise_server_exceptions=False:放行到需 DB 的端點時,錯誤化為 5xx 回應而非往上拋(不炸測試)。
client = TestClient(app, raise_server_exceptions=False)

_TOKEN = "s3cr3t-read-token"


@pytest.fixture(autouse=True)
def _isolate_settings():
    """每個測試前後清 settings 快取,確保讀到當下 monkeypatch 的環境變數、且不外溢。"""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def token_set(monkeypatch):
    monkeypatch.setenv("CMMS_READ_API_TOKEN", _TOKEN)
    monkeypatch.setenv("CMMS_APP_ENV", "production")  # 已設 token → 正常驗證(與環境無關)
    get_settings.cache_clear()
    return _TOKEN


# ---- 1. token 已設:驗證行為 ----

def test_protected_no_header_401(token_set):
    r = client.get("/work-orders")
    assert r.status_code == 401
    assert r.json() == {"detail": "unauthorized"}
    assert r.headers.get("www-authenticate") == "Bearer"


def test_protected_wrong_token_401(token_set):
    r = client.get("/work-orders", headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"


def test_vocab_failure_protected_no_header_401(token_set):
    """C2 失效詞彙讀 API 非豁免路徑 → 受 static bearer 保護(分析平台帶 token 拉)。"""
    r = client.get("/vocab/failure")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"


def test_protected_correct_token_passes_auth(token_set):
    # /openapi.json 受保護但不碰 DB → 對 token 直接 200,證明通過 auth 層。
    r = client.get("/openapi.json", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert r.status_code == 200
    assert r.status_code not in (401, 503)


# ---- 2. token 已設:豁免路徑不受影響 ----

@pytest.mark.parametrize("path", ["/health", "/app/login", "/static/app.css", "/"])
def test_exempt_paths_not_blocked(token_set, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code != 401
    assert r.status_code != 503


# ---- 3. on-box 前綴不疊 bearer(ADR-017 自帶 JWS)----

def test_onbox_not_blocked_by_bearer(token_set):
    # 不帶 bearer:middleware 放行 → 進 JWS 路由(JWKS 未配置 → 503 或缺 body → 422),重點是非 401。
    r = client.post("/work-orders/on-box/reactive", json={"jws_token": "x"})
    assert r.status_code != 401


# ---- 4. token 未設 + production → fail-closed 503 ----

def test_fail_closed_503_in_production(monkeypatch):
    monkeypatch.delenv("CMMS_READ_API_TOKEN", raising=False)
    monkeypatch.setenv("CMMS_APP_ENV", "production")
    get_settings.cache_clear()
    r = client.get("/work-orders")
    assert r.status_code == 503
    assert r.json() == {"detail": "read API token not configured"}


# ---- 5. token 未設 + 非 production → 放行(本機/CI 友善)----

def test_passthrough_when_unset_and_local(monkeypatch):
    monkeypatch.delenv("CMMS_READ_API_TOKEN", raising=False)
    monkeypatch.setenv("CMMS_APP_ENV", "local")
    get_settings.cache_clear()
    # /openapi.json 受保護但免 DB:未設 token + local → 放行 → 200(既有測試/本機不受影響)。
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert r.status_code not in (401, 503)


# ---- 6. schema 端點也受保護 ----

def test_openapi_requires_token(token_set):
    r = client.get("/openapi.json")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"
