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

from common import events, jwt_verify

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


async def _check(sub, name: str, ok: bool, note: str, *, claim: str = "",
                 expected=None, actual=None, status: int | None = None, body: str = "") -> None:
    """Tell the arcade page about one gate check. Display only."""
    await events.forward(sub, events.make(
        f"a2a.check.{name}", leg="A2A", kind="check", src="agent2", dst="agent2", note=note,
        check={"name": name, "claim": claim, "expected": expected, "actual": actual, "ok": ok},
        http={"status": status, "body": body} if status else None,
    ))


def _unverified_sub(token: str):
    # Only to route arcade events to the right user. Never used for a decision.
    try:
        return jwt_verify.unverified(token).get("sub")
    except Exception:  # noqa: BLE001
        return None


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
        # A repeat send (see agent1's raw_a2a_call) gets the same checks, silently.
        sub = None if request.headers.get("x-arcade-repeat") else _unverified_sub(token)
        await _check(sub, "bearer", True, "Gate 1: is there a Bearer token? Yes.",
                     claim="Authorization", expected="Bearer <token>", actual="Bearer ...")

        try:
            claims = jwt_verify.verify(
                token, audience=AUDIENCE, issuer=ISSUER, jwks_url=JWKS_URL
            )
        except jwt_verify.TokenError as e:
            await _check(sub, "jwt", False, f"Gate 2 failed: {e.detail}.",
                         claim="signature, iss, aud, exp", expected=AUDIENCE,
                         actual=e.detail, status=e.status, body=e.detail)
            return JSONResponse(
                {"error": e.detail},
                status_code=e.status,
                headers={"WWW-Authenticate": _realm_challenge()},
            )

        await _check(sub, "jwt", True,
                     "Gate 2: signed by Keycloak (checked with its JWKS), right issuer, "
                     f"aud includes {AUDIENCE}, not expired. Pass.",
                     claim="aud", expected=AUDIENCE, actual=claims.aud)

        caller = claims.azp or claims.actor_sub
        if caller not in ALLOWED_CALLERS:
            await _check(sub, "caller", False, f"Gate 3 failed: caller {caller!r} is not on the allowlist.",
                         claim="azp / act.sub", expected=sorted(ALLOWED_CALLERS), actual=caller,
                         status=403, body=f"caller {caller!r} is not in ALLOWED_CALLERS")
            return JSONResponse(
                {"error": f"caller {caller!r} is not in ALLOWED_CALLERS"},
                status_code=403,
            )

        await _check(sub, "caller", True,
                     f"Gate 3: the caller is {caller}. It is on agent2's allowlist. Pass.",
                     claim="azp / act.sub", expected=sorted(ALLOWED_CALLERS), actual=caller)

        if REQUIRED_SCOPE not in claims.scopes:
            await _check(sub, "scope", False,
                         f"Gate 4 failed: the token has no {REQUIRED_SCOPE} scope. The user is "
                         "real, but not allowed to use GitHub through agent2.",
                         claim="scope", expected=REQUIRED_SCOPE, actual=claims.scope,
                         status=403, body=f"missing scope {REQUIRED_SCOPE}")
            return JSONResponse(
                {"error": f"missing scope {REQUIRED_SCOPE}"},
                status_code=403,
            )

        await _check(sub, "scope", True, f"Gate 4: scope includes {REQUIRED_SCOPE}. Pass. "
                     "agent2 now reads the user from sub.",
                     claim="scope", expected=REQUIRED_SCOPE, actual=claims.scope)

        current_claims.set(claims)
        request.state.claims = claims
        return await call_next(request)
