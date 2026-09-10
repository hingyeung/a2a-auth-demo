"""Fake MCP server. Same auth shape as the real GitHub MCP server.

  - No token -> 401 + WWW-Authenticate with resource_metadata.
  - Serves oauth-protected-resource and oauth-authorization-server metadata.
  - authorize + token with PKCE (S256).
  - tools/list and tools/call with canned data.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time

from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

PUBLIC_URL = os.environ.get("MOCKMCP_BASE_URL", "http://localhost:9003")
INTERNAL_URL = os.environ.get("MOCKMCP_INTERNAL_URL", "http://mockmcp:9003")

_codes: dict[str, dict] = {}   # code -> {challenge, redirect_uri}
_tokens: set[str] = set()

REPOS = [
    {"name": "alice/notes", "private": True, "stars": 2},
    {"name": "alice/webapp", "private": False, "stars": 41},
]
ISSUES = [
    {"repo": "alice/webapp", "number": 12, "title": "Login button misaligned"},
    {"repo": "alice/webapp", "number": 15, "title": "Add dark mode"},
]


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()


def _needs_token_response():
    return JSONResponse(
        {"error": "unauthorized"},
        status_code=401,
        headers={
            "WWW-Authenticate": f'Bearer resource_metadata="{INTERNAL_URL}/.well-known/oauth-protected-resource"'
        },
    )


async def protected_resource(_request):
    return JSONResponse({
        "resource": INTERNAL_URL,
        "authorization_servers": [INTERNAL_URL],
    })


async def auth_server_meta(_request):
    return JSONResponse({
        "issuer": INTERNAL_URL,
        "authorization_endpoint": f"{PUBLIC_URL}/authorize",
        "token_endpoint": f"{INTERNAL_URL}/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
    })


async def authorize(request):
    q = request.query_params
    redirect_uri = q.get("redirect_uri")
    if not redirect_uri or q.get("code_challenge_method") != "S256":
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    code = secrets.token_urlsafe(16)
    _codes[code] = {"challenge": q.get("code_challenge"), "redirect_uri": redirect_uri}
    # No real consent screen. Just bounce straight back.
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={q.get('state', '')}")


async def token(request):
    form = await request.form()
    code = form.get("code")
    verifier = form.get("code_verifier", "")
    rec = _codes.pop(code, None)
    if not rec:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if rec["challenge"] != _s256(verifier):
        return JSONResponse({"error": "invalid_grant", "detail": "PKCE mismatch"}, status_code=400)
    tok = "mock-gh-" + secrets.token_urlsafe(20)
    _tokens.add(tok)
    return JSONResponse({
        "access_token": tok,
        "token_type": "bearer",
        "expires_in": 3600,
        "scope": "repo read:user",
        "issued_at": int(time.time()),
    })


async def mcp_rpc(request):
    auth = request.headers.get("authorization", "")
    tok = auth.split(None, 1)[1] if auth.lower().startswith("bearer ") else ""
    if not tok or (tok not in _tokens and not tok.startswith("gho_")):
        return _needs_token_response()

    body = await request.json()
    method = body.get("method")
    rid = body.get("id")

    if method == "tools/list":
        result = {"tools": [
            {"name": "list_repos", "description": "List the user's repos"},
            {"name": "list_issues", "description": "List the user's issues"},
        ]}
    elif method == "tools/call":
        name = body.get("params", {}).get("name")
        data = REPOS if name == "list_repos" else ISSUES if name == "list_issues" else []
        result = {"content": [{"type": "text", "text": str(data)}], "data": data}
    elif method == "ping":
        result = {"ok": True}
    else:
        return JSONResponse({"jsonrpc": "2.0", "id": rid,
                             "error": {"code": -32601, "message": "method not found"}})

    return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result})


app = Starlette(routes=[
    Route("/", mcp_rpc, methods=["POST"]),
    Route("/.well-known/oauth-protected-resource", protected_resource, methods=["GET"]),
    Route("/.well-known/oauth-authorization-server", auth_server_meta, methods=["GET"]),
    Route("/authorize", authorize, methods=["GET"]),
    Route("/token", token, methods=["POST"]),
])
