"""RFC 8693 token exchange. Turn the logged-in user's token into an OBO token for agent2."""
from __future__ import annotations

import os

import httpx

from oidc import CLIENT_ID, SECRET, TOKEN_URL

AUDIENCE = os.environ["AGENT2_AUDIENCE"]
GITHUB_CALLER_ROLE = "github-caller"


class ExchangeError(Exception):
    pass


def has_github_caller_role(user_claims: dict) -> bool:
    """Keycloak does not gate an optional client scope by user role on its
    own - any user agent1 asks for github.act on behalf of, gets it. So the
    decision has to be made here, by agent1, before it asks: only request
    github.act if the user's own token carries this realm role."""
    roles = (user_claims.get("realm_access") or {}).get("roles", [])
    return GITHUB_CALLER_ROLE in roles


async def obo_token(user_access_token: str, *, include_github_scope: bool) -> dict:
    # agent2-audience and actor.agent1 are requested for every user: audience
    # (who the token is for) and actor (who is calling) are not permissions,
    # they are just facts about this call. github.act (what the caller is
    # allowed to do there) is the one gated by include_github_scope.
    scope = "openid agent2-audience actor.agent1"
    if include_github_scope:
        scope += " github.act"

    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_id": CLIENT_ID,
        "client_secret": SECRET,
        "subject_token": user_access_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": AUDIENCE,
        "scope": scope,
    }
    async with httpx.AsyncClient(timeout=15) as hc:
        r = await hc.post(TOKEN_URL, data=data)
    if r.status_code != 200:
        raise ExchangeError(f"{r.status_code} {r.text}")
    return r.json()
