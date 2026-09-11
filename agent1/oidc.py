"""Authorization code flow with PKCE against Keycloak."""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from urllib.parse import urlencode

import httpx

ISSUER = os.environ["KEYCLOAK_ISSUER"]
INTERNAL = os.environ["KEYCLOAK_INTERNAL_URL"]
REALM = os.environ["KEYCLOAK_REALM"]
CLIENT_ID = os.environ["AGENT1_CLIENT_ID"]
SECRET = os.environ["AGENT1_SECRET"]
BASE_URL = os.environ["AGENT1_BASE_URL"]

REDIRECT_URI = f"{BASE_URL}/callback"
# Browser must reach these, so they use the public issuer URL.
AUTHORIZE_URL = f"{ISSUER}/protocol/openid-connect/auth"
LOGOUT_URL = f"{ISSUER}/protocol/openid-connect/logout"
# Back-channel calls use the internal URL.
TOKEN_URL = f"{INTERNAL}/realms/{REALM}/protocol/openid-connect/token"
JWKS_URL = f"{INTERNAL}/realms/{REALM}/protocol/openid-connect/certs"


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(state: str, challenge: str, login_hint: str | None = None) -> str:
    q = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "scope": "openid profile email",
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # Keycloak keeps its own SSO session in the browser, separate from
        # agent1's. Without this, clicking Login while that session is still
        # alice's silently hands back alice again - no form, login_hint
        # ignored, nothing to redirect to. prompt=login forces the real
        # credential form every time, regardless of any existing session.
        "prompt": "login",
    }
    if login_hint:
        # Only prefills the username field on Keycloak's login form - the
        # person still has to type their own password there.
        q["login_hint"] = login_hint
    return f"{AUTHORIZE_URL}?{urlencode(q)}"


async def exchange_code(code: str, verifier: str) -> dict:
    data = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": SECRET,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }
    async with httpx.AsyncClient(timeout=15) as hc:
        r = await hc.post(TOKEN_URL, data=data)
    r.raise_for_status()
    return r.json()
