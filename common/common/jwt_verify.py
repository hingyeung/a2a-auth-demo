"""JWKS cache plus JWT verify. Returns the claims we care about.

The issuer in tokens is the browser-facing Keycloak URL. The JWKS is fetched
over the internal Docker URL. So the two URLs are passed separately.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWKClient


class TokenError(Exception):
    """Raised when a token fails any check. Carries an HTTP status."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class Claims:
    sub: str
    azp: str | None
    act: dict[str, Any] | None
    scope: str
    aud: list[str]
    iss: str
    raw: dict[str, Any]

    @property
    def actor_sub(self) -> str | None:
        if self.act and isinstance(self.act, dict):
            return self.act.get("sub")
        return None

    @property
    def scopes(self) -> list[str]:
        return self.scope.split() if self.scope else []


_jwks_clients: dict[str, PyJWKClient] = {}
_jwks_born: dict[str, float] = {}
_JWKS_TTL = 300.0


def _client(jwks_url: str) -> PyJWKClient:
    now = time.time()
    if jwks_url not in _jwks_clients or now - _jwks_born.get(jwks_url, 0) > _JWKS_TTL:
        _jwks_clients[jwks_url] = PyJWKClient(jwks_url, cache_keys=True)
        _jwks_born[jwks_url] = now
    return _jwks_clients[jwks_url]


def verify(
    token: str,
    *,
    audience: str,
    issuer: str,
    jwks_url: str,
) -> Claims:
    """Verify signature, issuer, audience and expiry. Raise TokenError on any miss."""
    try:
        signing_key = _client(jwks_url).get_signing_key_from_jwt(token)
    except Exception as exc:  # noqa: BLE001
        raise TokenError(401, f"cannot resolve signing key: {exc}") from exc

    try:
        data = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError(401, "token expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise TokenError(401, f"wrong audience, need {audience}") from exc
    except jwt.InvalidIssuerError as exc:
        raise TokenError(401, "wrong issuer") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError(401, f"bad token: {exc}") from exc

    aud = data.get("aud", [])
    if isinstance(aud, str):
        aud = [aud]

    return Claims(
        sub=data["sub"],
        azp=data.get("azp"),
        act=data.get("act"),
        scope=data.get("scope", ""),
        aud=aud,
        iss=data.get("iss", ""),
        raw=data,
    )


def unverified(token: str) -> dict[str, Any]:
    """Decode without checks. For the trace panel only."""
    return jwt.decode(token, options={"verify_signature": False})
