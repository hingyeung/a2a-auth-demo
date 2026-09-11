"""Agent 1: the orchestrator. Logs alice in, exchanges her token, calls agent2."""
from __future__ import annotations

import os
import secrets
import time
from urllib.parse import urlencode

import httpx
import jwt as pyjwt
from a2a.client.errors import A2AClientHTTPError
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer

from common import jwt_verify, trace

import a2a_client
import exchange
import oidc

app = FastAPI()

SESSION_SECRET = os.environ["SESSION_SECRET"]
signer = URLSafeSerializer(SESSION_SECRET, salt="sid")
AGENT2_INTERNAL = os.environ["AGENT2_INTERNAL_URL"]

# session id -> {user_token, id_claims, pkce, state, obo}
SESSIONS: dict[str, dict] = {}


def _sid(request: Request) -> str:
    raw = request.cookies.get("sid")
    if raw:
        try:
            return signer.loads(raw)
        except BadSignature:
            pass
    return ""


def _new_session(resp) -> str:
    sid = secrets.token_urlsafe(18)
    SESSIONS[sid] = {}
    resp.set_cookie("sid", signer.dumps(sid), httponly=True, samesite="lax")
    return sid


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html") as f:
        return f.read()


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/login")
async def login(request: Request, user: str = ""):
    resp = RedirectResponse("/placeholder")
    sid = _sid(request)
    if not sid or sid not in SESSIONS:
        sid = _new_session(resp)
    verifier, challenge = oidc.new_pkce()
    state = secrets.token_urlsafe(16)
    SESSIONS[sid].update(pkce=verifier, state=state)
    # login_hint only prefills the username field on Keycloak's own login
    # form - alice or bob still type their own password there.
    resp = RedirectResponse(oidc.authorize_url(state, challenge, login_hint=user or None))
    resp.set_cookie("sid", signer.dumps(sid), httponly=True, samesite="lax")
    return resp


@app.get("/callback")
async def callback(request: Request, code: str = "", state: str = ""):
    sid = _sid(request)
    sess = SESSIONS.get(sid)
    if not sess or state != sess.get("state"):
        raise HTTPException(400, "bad session or state")
    tok = await oidc.exchange_code(code, sess["pkce"])
    user_token = tok["access_token"]
    # agent1 trusts its own login result. The trace panel shows the decoded body.
    claims = jwt_verify.unverified(user_token)
    sess["user_token"] = user_token
    sess["id_token"] = tok.get("id_token")
    sess["user_sub"] = claims.get("sub")
    sess["user_name"] = claims.get("preferred_username")
    sess["has_github_role"] = exchange.has_github_caller_role(claims)
    # The trace is kept across steps (login, then every ask) until the user
    # clicks "clear token trace". Login is itself one step, so it gets a row.
    trace.add(sid, "H2A user token (browser -> agent1)", user_token,
              note=f"Keycloak issued this to {sess['user_name']} after login.")
    return RedirectResponse("/")


@app.get("/logout")
async def logout(request: Request):
    """A real navigation, not a background fetch: Keycloak keeps its own SSO
    session cookie in the browser, separate from agent1's. Just clearing
    agent1's session left that cookie alive, so the next login silently
    picked up the previous user again (see README's "Is this user allowed
    to?" - same underlying lesson: agent1's session and Keycloak's session
    are not the same thing). Ending it needs a real redirect to Keycloak's
    own logout endpoint so its Set-Cookie actually reaches the browser.
    """
    sid = _sid(request)
    sess = SESSIONS.get(sid)
    id_token = sess.pop("id_token", None) if sess else None
    if sess:
        sess.pop("user_token", None)
        sess.pop("user_sub", None)
        sess.pop("user_name", None)
        sess.pop("has_github_role", None)
        sess.pop("obo", None)
    if id_token:
        q = urlencode({
            "id_token_hint": id_token,
            "post_logout_redirect_uri": f"{oidc.BASE_URL}/",
        })
        return RedirectResponse(f"{oidc.LOGOUT_URL}?{q}")
    return RedirectResponse("/")


@app.get("/whoami")
async def whoami(request: Request):
    sess = SESSIONS.get(_sid(request)) or {}
    return {
        "logged_in": "user_token" in sess,
        "user": sess.get("user_name"),
        "has_github_role": sess.get("has_github_role", False),
    }


@app.post("/ask")
async def ask(request: Request, prompt: str = Form(...)):
    sid = _sid(request)
    sess = SESSIONS.get(sid)
    if not sess or "user_token" not in sess:
        raise HTTPException(401, "log in first")

    # Give agent2 a clean slate for this one call so its rows are only the
    # ones this ask produces. Agent1's own trace log is never cleared here -
    # it only grows, step by step, until the user clicks "clear token trace".
    await _reset_agent2_trace(sess.get("user_sub", ""))

    has_role = sess.get("has_github_role", False)
    try:
        obo = await exchange.obo_token(sess["user_token"], include_github_scope=has_role)
    except exchange.ExchangeError as e:
        # Keycloak refused to exchange this subject_token. The most common
        # cause by far is that the cached login token went stale (usually
        # it just expired - see README). The exact Keycloak error still
        # goes into the trace note for anyone who wants the raw detail.
        trace.add(sid, "A2A token exchange (RFC 8693)", None, note=str(e), ok=False)
        sess.pop("user_token", None)
        sess.pop("user_sub", None)
        sess.pop("user_name", None)
        raise HTTPException(
            401,
            "Your login token was refused by Keycloak (it likely expired). "
            "Please log in again and ask right away.",
        )

    obo_token = obo["access_token"]
    sess["obo"] = obo_token
    role_note = (
        "user has the github-caller role, so github.act was requested."
        if has_role else
        "user does NOT have the github-caller role, so github.act was "
        "deliberately left out of the exchange request."
    )
    trace.add(sid, "A2A OBO token (agent1 -> agent2)", obo_token,
              note=f"RFC 8693 exchange. aud is now agent2-github-agent. {role_note}")

    try:
        result = await a2a_client.ask_agent2(obo_token, prompt)
    except A2AClientHTTPError as e:
        # Agent2's middleware refused the OBO token before the executor ever
        # ran. The streaming a2a-sdk client checks the response's
        # Content-Type before it checks the HTTP status code, so a plain
        # JSON error body reads to it as "not an SSE stream" and it raises
        # a made-up 400 rather than agent2's real status. Don't show that
        # to the user - reissue the same call as one plain HTTP request
        # (the same path the break-it buttons use) to get agent2's actual
        # status and body, and report those instead.
        raw = await a2a_client.raw_a2a_call(obo_token)
        await _merge_agent2_trace(sid)
        trace.add(sid, "A2A call to agent2 refused", None,
                  note=f"agent2 answered {raw['status']}: {raw['body']}", ok=False)
        raise HTTPException(
            raw["status"] if raw["status"] >= 400 else 502,
            f"agent2 refused this call ({raw['status']}): {raw['body']}",
        )

    # Pull agent2's own trace rows and merge them in.
    await _merge_agent2_trace(sid)

    return JSONResponse(result)


async def _reset_agent2_trace(sub: str):
    try:
        async with httpx.AsyncClient(timeout=10) as hc:
            await hc.post(f"{AGENT2_INTERNAL}/debug/trace/clear", params={"sub": sub})
    except Exception:  # noqa: BLE001
        pass


async def _merge_agent2_trace(sid: str):
    try:
        async with httpx.AsyncClient(timeout=10) as hc:
            r = await hc.get(f"{AGENT2_INTERNAL}/debug/trace", params={"sub": SESSIONS[sid].get("user_sub", "")})
        for row in r.json().get("rows", []):
            # Agent2's /debug/trace never sends the raw token back over the
            # wire, only the already-decoded summary/body and a truncated
            # token_head. Keep that as-is instead of re-running trace.add()
            # with no token, which would silently blank the row out.
            trace.add_precomputed(sid, row)
    except Exception as e:  # noqa: BLE001
        trace.add(sid, "merge agent2 trace", None, note=f"could not read agent2 trace: {e}", ok=False)


@app.get("/trace")
async def get_trace(request: Request):
    return {"rows": trace.get(_sid(request))}


@app.post("/trace/clear")
async def clear_trace(request: Request):
    sid = _sid(request)
    trace.clear(sid)
    sess = SESSIONS.get(sid) or {}
    await _reset_agent2_trace(sess.get("user_sub", ""))
    return {"ok": True}


@app.get("/.well-known/agent-card.json")
async def agent1_card():
    """agent1 is not a full A2A server (its /ask endpoint is plain REST, not
    JSON-RPC). This card exists only so the web page can show it next to
    agent2's real A2A card, for comparison."""
    return {
        "name": "Agent 1 orchestrator",
        "description": "Logs the end user in, exchanges their token (RFC 8693), "
                        "and calls agent2 over A2A on their behalf. Not itself an "
                        "A2A server - /ask is a plain REST endpoint for this demo's web page.",
        "url": os.environ["AGENT1_BASE_URL"],
        "version": "0.1.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{
            "id": "ask",
            "name": "Ask (forwards to agent2)",
            "description": "Takes a prompt from the browser, exchanges the user's "
                            "token, and forwards it to agent2 over A2A.",
            "tags": ["orchestrator"],
        }],
    }


# ---------------- break-it buttons ----------------

async def _local_junk_token(sub: str = "alice", **over) -> str:
    body = {
        "sub": sub, "iss": oidc.ISSUER, "aud": "agent2-github-agent",
        "azp": "agent1-orchestrator", "scope": "github.act",
        "iat": int(time.time()), "exp": int(time.time()) + 300,
    }
    body.update(over)
    return pyjwt.encode(body, "not-the-real-key", algorithm="HS256")


@app.post("/break/{kind}")
async def break_it(kind: str, request: Request):
    sid = _sid(request)
    sess = SESSIONS.get(sid) or {}
    token: str | None

    sub = sess.get("user_sub", "alice")

    if kind == "wrong-aud":
        token = await _local_junk_token(sub=sub, aud="some-other-service")
        label = "Break: token signed by the wrong key, aud=some-other-service"
    elif kind == "raw-user":
        token = sess.get("user_token")
        label = "Break: raw user token (no exchange), aud has no agent2"
    elif kind == "expired":
        token = await _local_junk_token(sub=sub, exp=int(time.time()) - 60)
        label = "Break: expired token"
    elif kind == "no-token":
        token = None
        label = "Break: no Authorization header"
    elif kind == "rogue-client":
        token = await _rogue_token()
        label = "Break: real Keycloak token, but azp=rogue-agent (not allowlisted)"
    else:
        raise HTTPException(404, "unknown break kind")

    res = await a2a_client.raw_a2a_call(token)
    trace.add(sid, label, token, note=f"agent2 answered {res['status']}", ok=False)
    return {"kind": kind, "result": res}


async def _rogue_token() -> str:
    data = {
        "grant_type": "client_credentials",
        "client_id": os.environ.get("ROGUE_CLIENT_ID", "rogue-agent"),
        "client_secret": os.environ.get("ROGUE_SECRET", "rogue-dev-secret"),
        "scope": "github.act agent2-audience",
    }
    async with httpx.AsyncClient(timeout=15) as hc:
        r = await hc.post(oidc.TOKEN_URL, data=data)
    r.raise_for_status()
    return r.json()["access_token"]
