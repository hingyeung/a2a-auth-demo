"""Starlette middleware in front of the A2A app.

Two jobs, kept apart:
  - Caller control: is this really agent1? (signature, aud, azp allowlist, scope)
  - User identity: who is the user? (sub, straight out of the verified token)
"""
from __future__ import annotations

import contextvars
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from common import jwt_verify

ISSUER = os.environ["KEYCLOAK_ISSUER"]
INTERNAL = os.environ["KEYCLOAK_INTERNAL_URL"]
REALM = os.environ["KEYCLOAK_REALM"]
AUDIENCE = os.environ["AGENT2_AUDIENCE"]
JWKS_URL = f"{INTERNAL}/realms/{REALM}/protocol/openid-connect/certs"

ALLOWED_CALLERS = set(
    c.strip() for c in os.environ.get("ALLOWED_CALLERS", "agent1-orchestrator").split(",") if c.strip()
)
REQUIRED_SCOPE = os.environ.get("REQUIRED_SCOPE", "github.act")

# Executor reads this. Set by the middleware in the same async task.
current_claims: contextvars.ContextVar = contextvars.ContextVar("current_claims", default=None)

OPEN_PREFIXES = ("/.well-known", "/health", "/github", "/debug")


def _realm_challenge() -> str:
    return f'Bearer realm="{REALM}", error="invalid_token"'


class OBOAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method == "GET" or any(path.startswith(p) for p in OPEN_PREFIXES):
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "missing bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": _realm_challenge()},
            )
        token = header.split(None, 1)[1].strip()

        try:
            claims = jwt_verify.verify(
                token, audience=AUDIENCE, issuer=ISSUER, jwks_url=JWKS_URL
            )
        except jwt_verify.TokenError as e:
            return JSONResponse(
                {"error": e.detail},
                status_code=e.status,
                headers={"WWW-Authenticate": _realm_challenge()},
            )

        caller = claims.azp or claims.actor_sub
        if caller not in ALLOWED_CALLERS:
            return JSONResponse(
                {"error": f"caller {caller!r} is not in ALLOWED_CALLERS"},
                status_code=403,
            )

        if REQUIRED_SCOPE not in claims.scopes:
            return JSONResponse(
                {"error": f"missing scope {REQUIRED_SCOPE}"},
                status_code=403,
            )

        current_claims.set(claims)
        request.state.claims = claims
        return await call_next(request)
