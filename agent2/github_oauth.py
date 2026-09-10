"""The one time agent2 faces the user: GitHub consent.

The signed ticket carries the verified user identity (sub) from the token leg
into the browser leg. We never trust a cookie agent2 set on its own.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets

import httpx
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

import mcp_client
import store

TICKET_KEY = os.environ["AGENT2_TICKET_KEY"]
BASE_URL = os.environ["AGENT2_BASE_URL"]
REDIRECT_URI = f"{BASE_URL}/github/callback"
TICKET_TTL = 300  # 5 minutes

GH_CLIENT_ID = os.environ.get("GITHUB_APP_CLIENT_ID") or "agent2-mock-client"
GH_CLIENT_SECRET = os.environ.get("GITHUB_APP_CLIENT_SECRET") or None

_ticket_signer = URLSafeTimedSerializer(TICKET_KEY, salt="github-consent")
_used_nonces: set[str] = set()
_pending: dict[str, dict] = {}  # nonce -> {sub, verifier}


def mint_ticket(sub: str) -> str:
    nonce = secrets.token_urlsafe(12)
    return _ticket_signer.dumps({"sub": sub, "nonce": nonce})


def consent_url(ticket: str) -> str:
    return f"{BASE_URL}/github/login?ticket={ticket}"


def _read_ticket(ticket: str) -> dict:
    try:
        return _ticket_signer.loads(ticket, max_age=TICKET_TTL)
    except SignatureExpired as e:
        raise ValueError("ticket expired") from e
    except BadSignature as e:
        raise ValueError("ticket signature is not valid") from e


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


async def github_login(request: Request):
    ticket = request.query_params.get("ticket", "")
    try:
        data = _read_ticket(ticket)
    except ValueError as e:
        return HTMLResponse(f"<h3>Refused</h3><p>{e}</p>", status_code=400)

    disc = await mcp_client.discover()
    verifier, challenge = _pkce()
    _pending[data["nonce"]] = {"sub": data["sub"], "verifier": verifier,
                               "token_endpoint": disc["token_endpoint"]}

    q = httpx.QueryParams({
        "response_type": "code",
        "client_id": GH_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "repo read:user",
        "state": ticket,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return RedirectResponse(f"{disc['authorization_endpoint']}?{q}")


async def github_callback(request: Request):
    code = request.query_params.get("code", "")
    ticket = request.query_params.get("state", "")
    try:
        data = _read_ticket(ticket)
    except ValueError as e:
        return HTMLResponse(f"<h3>Refused</h3><p>{e}</p>", status_code=400)

    nonce = data["nonce"]
    if nonce in _used_nonces:
        return HTMLResponse("<h3>Refused</h3><p>ticket already used</p>", status_code=400)
    pend = _pending.pop(nonce, None)
    if not pend or pend["sub"] != data["sub"]:
        return HTMLResponse("<h3>Refused</h3><p>no pending request for this ticket</p>", status_code=400)
    _used_nonces.add(nonce)

    tok = await mcp_client.exchange_pkce(
        pend["token_endpoint"], code, pend["verifier"],
        REDIRECT_URI, GH_CLIENT_ID, GH_CLIENT_SECRET,
    )
    store.put_token(
        pend["sub"],
        tok["access_token"],
        tok.get("refresh_token"),
        tok.get("expires_in"),
        tok.get("scope"),
    )
    return HTMLResponse(
        f"<h3>GitHub connected</h3><p>Stored for user <code>{pend['sub']}</code>. "
        f"Close this tab and ask agent1 again.</p>"
    )
