"""One MCP code path. Mock or real GitHub, chosen by MCP_MODE.

Both speak the same dance:
  1. Hit the MCP endpoint with no token -> 401 + WWW-Authenticate.
  2. Read resource metadata -> find the authorization server.
  3. Read the authorization-server metadata -> authorize + token endpoints.
  4. Later, call a tool with the user's stored bearer token.
"""
from __future__ import annotations

import os
import re

import httpx

MODE = os.environ.get("MCP_MODE", "mock")
MOCK_URL = os.environ.get("MCP_MOCK_URL", "http://mockmcp:9003")
GITHUB_URL = os.environ.get("MCP_GITHUB_URL", "https://api.githubcopilot.com/mcp/")

BASE_URL = MOCK_URL if MODE == "mock" else GITHUB_URL


def _parse_www_auth(value: str) -> dict:
    return dict(re.findall(r'(\w+)="([^"]+)"', value or ""))


async def discover() -> dict:
    """Return {authorization_endpoint, token_endpoint, registration_endpoint?}."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as hc:
        probe = await hc.post(BASE_URL, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        params = _parse_www_auth(probe.headers.get("www-authenticate", ""))
        resource_meta_url = params.get("resource_metadata")

        as_url = None
        if resource_meta_url:
            rm = (await hc.get(resource_meta_url)).json()
            servers = rm.get("authorization_servers") or []
            if servers:
                as_url = servers[0]
        if not as_url:
            # fall back to the well-known on the base host
            as_url = BASE_URL.rstrip("/")

        as_url = as_url.rstrip("/")
        meta_url = as_url + "/.well-known/oauth-authorization-server"
        meta_resp = await hc.get(meta_url)
        try:
            meta = meta_resp.json()
            authorization_endpoint = meta["authorization_endpoint"]
            token_endpoint = meta["token_endpoint"]
            registration_endpoint = meta.get("registration_endpoint")
        except (ValueError, KeyError):
            # This authorization server was found by discovery (the
            # resource_metadata step above) but does not publish RFC 8414
            # metadata at the well-known path - GitHub's does not. Fall
            # back to GitHub's own documented endpoint names under the
            # same base. mockmcp always has real metadata, so this branch
            # only runs for MCP_MODE=github.
            authorization_endpoint = f"{as_url}/authorize"
            token_endpoint = f"{as_url}/access_token"
            registration_endpoint = None

        return {
            "authorization_endpoint": authorization_endpoint,
            "token_endpoint": token_endpoint,
            "registration_endpoint": registration_endpoint,
            "probe_status": probe.status_code,
            "www_authenticate": probe.headers.get("www-authenticate"),
        }


async def exchange_pkce(token_endpoint: str, code: str, verifier: str,
                        redirect_uri: str, client_id: str, client_secret: str | None) -> dict:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=15) as hc:
        r = await hc.post(token_endpoint, data=data, headers={"accept": "application/json"})
    r.raise_for_status()
    return r.json()


async def call_tool(github_token: str, tool: str, arguments: dict | None = None) -> dict:
    """Call one MCP tool with the user's bearer token."""
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }
    headers = {
        "authorization": f"Bearer {github_token}",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as hc:
        r = await hc.post(BASE_URL, json=body, headers=headers)
    if r.status_code == 401:
        return {"error": "MCP rejected the token (401)", "www_authenticate": r.headers.get("www-authenticate")}
    try:
        data = r.json()
    except Exception:  # noqa: BLE001  (github returns SSE frames)
        data = {"raw": r.text[:600]}
    return data
