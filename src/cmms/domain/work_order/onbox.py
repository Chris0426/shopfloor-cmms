"""ADR-017 Profile B:設備端 on-box 報修的通道 principal 簽章驗證(`wr.onbox_principal_sig.v1`)。

Analytics 以 Ed25519 對通道 principal 簽 compact JWS;cmms 驗:**簽章(經 JWKS kid)+ sub=
`agent:analytics-onbox` + op ∈ Profile B + 未過期 + 已知 kid**,並回 claims 供 service 取
`asset_id`(完整性靠顯式 claim,**不 parse** idempotency_key,維持解耦)。失敗/無簽/過期 = 拒。

key 解析(kid → 公鑰)由呼叫端注入:prod 走 JWKS(`/.well-known/onbox-jwks.json`,見
`make_jwks_resolver`);測試注入固定公鑰。≠ evidence 解析簽章(那是 Analytics 下游、誰能取 blob)。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt

SCHEME = "wr.onbox_principal_sig.v1"  # 通道 principal 簽章方案名(文件用;非驗證欄)
ONBOX_PRINCIPAL = "agent:analytics-onbox"  # 期望的 sub(機台/站別歸屬,非個人)
PROFILE_B_OPS = frozenset({"open_reactive_work_order", "cancel_reactive_report"})

# kid -> 公鑰(PEM/JWK 物件)。回 None = 未知 kid。
KeyResolver = Callable[[str], Any]


class OnboxVerificationError(Exception):
    """on-box JWS 驗證失敗(拒收)。"""


@dataclass(frozen=True, slots=True)
class OnboxClaims:
    op: str
    asset_id: str  # = 機台 EID;cmms 用此 claim,不 parse idempotency_key
    idempotency_key: str  # onbox:<station>:<EID>:<edge_ts>:<nonce>(verbatim)
    origin_station: str | None
    evidence_ref: str | None
    sub: str
    jti: str | None


def verify_onbox_jws(token: str, *, key_resolver: KeyResolver) -> OnboxClaims:
    """驗 Analytics on-box JWS,回已驗證 claims;任何不符 → OnboxVerificationError(拒收)。"""
    if not token:
        raise OnboxVerificationError("missing token (anonymous rejected)")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise OnboxVerificationError(f"malformed JWS header: {e}") from e
    kid = header.get("kid")
    if not kid:
        raise OnboxVerificationError("missing kid")
    try:
        key = key_resolver(kid)
    except Exception as e:  # resolver(如 JWKS 取不到)→ 視為未知 kid
        raise OnboxVerificationError(f"key resolution failed for kid={kid}: {e}") from e
    if key is None:
        raise OnboxVerificationError(f"unknown kid: {kid}")
    try:
        claims = jwt.decode(token, key, algorithms=["EdDSA"], options={"require": ["exp", "iat"]})
    except jwt.ExpiredSignatureError as e:
        raise OnboxVerificationError("expired") from e
    except jwt.PyJWTError as e:
        raise OnboxVerificationError(f"signature/claims invalid: {e}") from e

    if claims.get("sub") != ONBOX_PRINCIPAL:
        raise OnboxVerificationError(f"unexpected sub: {claims.get('sub')!r}")
    op = claims.get("op")
    if op not in PROFILE_B_OPS:
        raise OnboxVerificationError(f"op not in Profile B: {op!r}")
    asset_id = claims.get("asset_id")
    if not asset_id:
        raise OnboxVerificationError("missing asset_id claim")
    idem = claims.get("idempotency_key")
    if not idem:
        raise OnboxVerificationError("missing idempotency_key claim")
    return OnboxClaims(
        op=op,
        asset_id=asset_id,
        idempotency_key=idem,
        origin_station=claims.get("origin_station"),
        evidence_ref=claims.get("evidence_ref"),
        sub=claims["sub"],
        jti=claims.get("jti"),
    )


def make_jwks_resolver(jwks_url: str) -> KeyResolver:
    """prod:由 Analytics JWKS(`/.well-known/onbox-jwks.json`)取公鑰(kid 輪替、快取)。

    ⚠ 待 Analytics 部署 JWKS 網域(下游契約登記);未配置則 API 層回 503。
    """
    client = jwt.PyJWKClient(jwks_url)

    def resolve(kid: str) -> Any:
        return client.get_signing_key(kid).key

    return resolve


class OnboxJwksConfigError(Exception):
    """靜態 JWKS 設定錯誤(壞 JSON / 無可用 key)。**fail-fast** —— 建構時即拋,不默默 503。"""


def make_static_jwks_resolver(jwks_json: str) -> KeyResolver:
    """Analytics 裁決:公鑰以**值**交付(RFC7517 JWKS JSON,`CMMS_ONBOX_JWKS_JSON`),不走 URL fetch。

    解析 JWKS(OKP/Ed25519/EdDSA;`kid` → 公鑰,經 `jwt.PyJWK`)成 kid→key 對映。壞 JSON /
    非物件 / 無 keys / 無帶 kid 的可用 key → **建構時**拋 `OnboxJwksConfigError`(fail-fast,
    不默默 503)。輪替 = 分析平台重交一次值(換 secret + 重啟)。resolve 未知 kid → None(同 URL 版)。
    """
    try:
        data = json.loads(jwks_json)
    except (ValueError, TypeError) as e:
        raise OnboxJwksConfigError(f"invalid JWKS JSON: {e}") from e
    keys = data.get("keys") if isinstance(data, dict) else None
    if not keys:
        raise OnboxJwksConfigError("JWKS has no keys")
    by_kid: dict[str, Any] = {}
    for jwk in keys:
        kid = jwk.get("kid") if isinstance(jwk, dict) else None
        if not kid:
            continue  # 無 kid 無法解析(驗證靠 header.kid);略過該 key
        try:
            by_kid[kid] = jwt.PyJWK(jwk).key
        except Exception as e:  # 壞 JWK(型別/曲線不符)→ fail-fast
            raise OnboxJwksConfigError(f"invalid JWK (kid={kid}): {e}") from e
    if not by_kid:
        raise OnboxJwksConfigError("JWKS has no usable key with a kid")

    def resolve(kid: str) -> Any:
        return by_kid.get(kid)

    return resolve
