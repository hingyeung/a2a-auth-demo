"""The one time the repository agent (agent2) faces the user: GitHub consent.

The signed ticket carries the verified user identity (sub) from the token leg
into the browser leg. We never trust a cookie the repository agent set on its own.
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
from common import events


async def _ev(sub, step: str, **kw) -> None:
    await events.forward(sub, events.make(step, leg="CONSENT", **kw))


def _who_plays_github() -> str:
    # In mock mode one container plays two roles. Say so, so students do not
    # think the MCP server itself hands out GitHub tokens.
    if mcp_client.MODE == "mock":
        return (" (In mock mode, mockmcp plays GitHub's login server as well as the "
                "MCP server. With MCP_MODE=github they are two different hosts.)")
    return ""

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

    sub = data["sub"]
    await _ev(sub, "consent.ticket", kind="check", src="agent2",
              note="The user opens the consent link. The repository agent checks the ticket's signature "
                   "and age. It trusts the sub inside, not any cookie.",
              check={"name": "signed ticket", "claim": "ticket", "expected": "valid, < 5 min",
                     "actual": "valid", "ok": True})

    disc = await mcp_client.discover()
    await _ev(sub, "mcp.probe", kind="result", src="mcp", dst="agent2",
              note="The repository agent knocks on the MCP server with no token. It answers 401 and a "
                   "WWW-Authenticate header that points to its metadata.",
              http={"status": disc["probe_status"], "body": disc.get("www_authenticate") or ""})
    await _ev(sub, "mcp.discover", kind="info", src="agent2", dst="github",
              note="The repository agent reads the MCP server's metadata. It names the server that "
                   "gives out tokens for it: GitHub's login server, not Keycloak. The repository agent "
                   "then reads GitHub's metadata to find its authorize and token "
                   "endpoints." + _who_plays_github(),
              data={"resource_metadata": disc.get("resource_metadata"),
                    "authorization_server": disc.get("authorization_server"),
                    "authorization_endpoint": disc["authorization_endpoint"],
                    "token_endpoint": disc["token_endpoint"]})
    verifier, challenge = _pkce()
    _pending[data["nonce"]] = {"sub": data["sub"], "verifier": verifier,
                               "token_endpoint": disc["token_endpoint"],
                               "authorization_server": disc.get("authorization_server")}

    q = httpx.QueryParams({
        "response_type": "code",
        "client_id": GH_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "repo read:user",
        "state": ticket,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    await _ev(sub, "consent.authorize", kind="request", src="user", dst="github",
              note="The repository agent sends the browser to GitHub's login server. The user logs in "
                   "to GitHub (not Keycloak) and agrees to let the repository agent use their repos. "
                   "The link carries a PKCE challenge, and the ticket rides along as "
                   "state." + _who_plays_github(),
              data={"authorization_server": disc.get("authorization_server"),
                    "authorization_endpoint": disc["authorization_endpoint"],
                    "scope": "repo read:user", "code_challenge_method": "S256",
                    "mode": mcp_client.MODE})
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
    await _ev(pend["sub"], "consent.callback", kind="request", src="user", dst="agent2",
              note="The user said yes. GitHub sends the browser back to the repository agent with a "
                   "one-time code. The repository agent checks the ticket in state again and makes "
                   "sure it was not used before.",
              check={"name": "state ticket + one-time nonce", "claim": "state",
                     "expected": "valid ticket, unused nonce, same sub",
                     "actual": "ok", "ok": True})
    await _ev(pend["sub"], "consent.swap", kind="request", src="agent2", dst="github",
              note="The repository agent calls GitHub's token endpoint directly, server to server. It "
                   "sends the code plus the PKCE verifier. Only the app that started the "
                   "login knows that verifier, so a stolen code is useless.",
              data={"token_endpoint": pend["token_endpoint"],
                    "grant_type": "authorization_code", "sends": ["code", "code_verifier"]})

    tok = await mcp_client.exchange_pkce(
        pend["token_endpoint"], code, pend["verifier"],
        REDIRECT_URI, GH_CLIENT_ID, GH_CLIENT_SECRET,
    )
    await _ev(pend["sub"], "consent.token", kind="token", src="github", dst="agent2",
              note="GitHub gives the repository agent the user's own GitHub token. GitHub made it, not "
                   "Keycloak. It is opaque, not a JWT.",
              token=events.token_view(tok["access_token"], "GitHub token"),
              data={"scope": tok.get("scope"), "expires_in": tok.get("expires_in")})
    store.put_token(
        pend["sub"],
        tok["access_token"],
        tok.get("refresh_token"),
        tok.get("expires_in"),
        tok.get("scope"),
    )
    await _ev(pend["sub"], "consent.stored", kind="info", src="agent2",
              note="The repository agent stores the GitHub token under the user's sub. Close the tab "
                   "and ask again.")
    return HTMLResponse(
        f"<h3>GitHub connected</h3><p>Stored for user <code>{pend['sub']}</code>. "
        f"Close this tab and ask the orchestrator agent again.</p>"
    )
