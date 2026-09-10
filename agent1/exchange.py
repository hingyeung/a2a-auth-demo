"""RFC 8693 token exchange. Turn alice's user token into an OBO token for agent2."""
from __future__ import annotations

import os

import httpx

from oidc import CLIENT_ID, SECRET, TOKEN_URL

AUDIENCE = os.environ["AGENT2_AUDIENCE"]


class ExchangeError(Exception):
    pass


async def obo_token(user_access_token: str) -> dict:
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_id": CLIENT_ID,
        "client_secret": SECRET,
        "subject_token": user_access_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": AUDIENCE,
        # Ask for the optional scopes that mark this token for agent2.
        "scope": "openid github.act actor.agent1",
    }
    async with httpx.AsyncClient(timeout=15) as hc:
        r = await hc.post(TOKEN_URL, data=data)
    if r.status_code != 200:
        raise ExchangeError(f"{r.status_code} {r.text}")
    return r.json()
