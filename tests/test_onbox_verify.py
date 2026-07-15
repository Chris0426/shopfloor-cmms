"""ADR-017 on-box JWS 驗證的純函式測試(無 DB)。需 pyjwt[crypto]。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("jwt")
import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from cmms.domain.work_order.onbox import (  # noqa: E402
    ONBOX_PRINCIPAL,
    OnboxVerificationError,
    verify_onbox_jws,
)

_PRIV = Ed25519PrivateKey.generate()
_PUB = _PRIV.public_key()
_OTHER = Ed25519PrivateKey.generate()


def _resolver(kid: str):
    return _PUB if kid == "k1" else None


def _sign(claims: dict, *, kid: str = "k1", key: Ed25519PrivateKey | None = None) -> str:
    return jwt.encode(claims, key or _PRIV, algorithm="EdDSA", headers={"kid": kid})


def _claims(**over) -> dict:
    now = datetime.now(UTC)
    c = {
        "iss": "analytics",
        "sub": ONBOX_PRINCIPAL,
        "op": "open_reactive_work_order",
        "asset_id": "EID-70002",
        "idempotency_key": "onbox:WET01:EID-70002:1719000000:abc",
        "origin_station": "WET01",
        "evidence_ref": "onbox-evidence:v1:onbox:WET01:EID-70002:1719000000:abc",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "jti": "j1",
    }
    c.update(over)
    return c


def test_verify_ok() -> None:
    claims = verify_onbox_jws(_sign(_claims()), key_resolver=_resolver)
    assert claims.op == "open_reactive_work_order"
    assert claims.asset_id == "EID-70002"
    assert claims.idempotency_key.startswith("onbox:")
    assert claims.origin_station == "WET01"
    assert claims.evidence_ref.startswith("onbox-evidence:v1:")


def test_reject_bad_signature() -> None:
    with pytest.raises(OnboxVerificationError):
        verify_onbox_jws(_sign(_claims(), key=_OTHER), key_resolver=_resolver)


def test_reject_unknown_kid() -> None:
    with pytest.raises(OnboxVerificationError, match="unknown kid"):
        verify_onbox_jws(_sign(_claims(), kid="kX"), key_resolver=_resolver)


def test_reject_wrong_sub() -> None:
    with pytest.raises(OnboxVerificationError, match="sub"):
        verify_onbox_jws(_sign(_claims(sub="agent:evil")), key_resolver=_resolver)


def test_reject_op_not_profile_b() -> None:
    with pytest.raises(OnboxVerificationError, match="Profile B"):
        verify_onbox_jws(_sign(_claims(op="close_work_order")), key_resolver=_resolver)


def test_reject_expired() -> None:
    past = datetime.now(UTC) - timedelta(minutes=10)
    c = _claims(iat=int(past.timestamp()), exp=int((past + timedelta(minutes=1)).timestamp()))
    with pytest.raises(OnboxVerificationError, match="expired"):
        verify_onbox_jws(_sign(c), key_resolver=_resolver)


def test_reject_missing_asset_id() -> None:
    c = _claims()
    del c["asset_id"]
    with pytest.raises(OnboxVerificationError, match="asset_id"):
        verify_onbox_jws(_sign(c), key_resolver=_resolver)


def test_reject_empty_token() -> None:
    with pytest.raises(OnboxVerificationError, match="anonymous"):
        verify_onbox_jws("", key_resolver=_resolver)


# ---- 靜態 JWKS resolver(Analytics 裁決:公鑰以值交付,CMMS_ONBOX_JWKS_JSON)----

import json  # noqa: E402

from jwt.algorithms import OKPAlgorithm  # noqa: E402

from cmms.domain.work_order.onbox import (  # noqa: E402
    OnboxJwksConfigError,
    make_static_jwks_resolver,
)


def _jwks_json(*, kid: str | None = "k1", pub=None) -> str:
    """由 Ed25519 公鑰組 RFC7517 JWKS JSON(OKP/Ed25519/EdDSA)。"""
    jwk = json.loads(OKPAlgorithm.to_jwk(pub or _PUB))
    jwk["alg"] = "EdDSA"
    if kid is not None:
        jwk["kid"] = kid
    return json.dumps({"keys": [jwk]})


def test_static_resolver_parses_and_verifies() -> None:
    """靜態 JWKS 解析 分析平台形狀(OKP/Ed25519/kid)→ 可驗簽;未知 kid → None。"""
    resolver = make_static_jwks_resolver(_jwks_json(kid="k1"))
    claims = verify_onbox_jws(_sign(_claims(), kid="k1"), key_resolver=resolver)
    assert claims.asset_id == "EID-70002"
    assert resolver("kX") is None  # 未知 kid → None(同 URL 版)


def test_static_resolver_bad_json_fails_fast() -> None:
    with pytest.raises(OnboxJwksConfigError, match="invalid JWKS JSON"):
        make_static_jwks_resolver("{not valid json")


def test_static_resolver_empty_keys_fails_fast() -> None:
    with pytest.raises(OnboxJwksConfigError, match="no keys"):
        make_static_jwks_resolver('{"keys": []}')


def test_static_resolver_no_kid_fails_fast() -> None:
    with pytest.raises(OnboxJwksConfigError, match="no usable key"):
        make_static_jwks_resolver(_jwks_json(kid=None))


def test_onbox_resolver_prefers_static_json_over_url(monkeypatch) -> None:
    """API 層 `_onbox_key_resolver`:JSON 有值 → static(優先);不打 URL。"""
    from cmms.api.routes.work_orders import _onbox_key_resolver
    from cmms.config import get_settings

    monkeypatch.setenv("CMMS_ONBOX_JWKS_JSON", _jwks_json(kid="k1"))
    monkeypatch.setenv("CMMS_ONBOX_JWKS_URL", "https://example.invalid/jwks.json")
    get_settings.cache_clear()
    try:
        resolver = _onbox_key_resolver()
        # 走 static:k1 解析得到公鑰(若走 URL 會嘗試 fetch invalid → 不會回可用 key)
        assert resolver("k1") is not None
    finally:
        get_settings.cache_clear()
