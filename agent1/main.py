"""Agent 1: the orchestrator. Logs alice in, exchanges her token, calls agent2."""
from __future__ import annotations

import os
import secrets
import time

import httpx
import jwt as pyjwt
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
async def login(request: Request):
    resp = RedirectResponse("/placeholder")
    sid = _sid(request)
    if not sid or sid not in SESSIONS:
        sid = _new_session(resp)
    verifier, challenge = oidc.new_pkce()
    state = secrets.token_urlsafe(16)
    SESSIONS[sid].update(pkce=verifier, state=state)
    resp = RedirectResponse(oidc.authorize_url(state, challenge))
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
    sess["user_sub"] = claims.get("sub")
    sess["user_name"] = claims.get("preferred_username")
    trace.clear(sid)
    trace.add(sid, "1. H2A user token (browser -> agent1)", user_token,
              note="Keycloak issued this to alice after login.")
    return RedirectResponse("/")


@app.get("/whoami")
async def whoami(request: Request):
    sess = SESSIONS.get(_sid(request)) or {}
    return {"logged_in": "user_token" in sess, "user": sess.get("user_name")}


@app.post("/ask")
async def ask(request: Request, prompt: str = Form(...)):
    sid = _sid(request)
    sess = SESSIONS.get(sid)
    if not sess or "user_token" not in sess:
        raise HTTPException(401, "log in first")

    # Rebuild the trace fresh for this ask so the panel is not a pile of repeats.
    trace.clear(sid)
    await _reset_agent2_trace(sess.get("user_sub", ""))
    trace.add(sid, "1. H2A user token (browser -> agent1)", sess["user_token"],
              note="Keycloak issued this to alice after login.")

    try:
        obo = await exchange.obo_token(sess["user_token"])
    except exchange.ExchangeError as e:
        trace.add(sid, "2. A2A token exchange (RFC 8693)", None, note=str(e), ok=False)
        raise HTTPException(502, f"token exchange failed: {e}")

    obo_token = obo["access_token"]
    sess["obo"] = obo_token
    trace.add(sid, "2. A2A OBO token (agent1 -> agent2)", obo_token,
              note="RFC 8693 exchange. aud is now agent2-github-agent.")

    result = await a2a_client.ask_agent2(obo_token, prompt)

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
            trace.add(sid, row["hop"], row.get("token"), note=row.get("note", ""), ok=row.get("ok", True))
    except Exception as e:  # noqa: BLE001
        trace.add(sid, "merge agent2 trace", None, note=f"could not read agent2 trace: {e}", ok=False)


@app.get("/trace")
async def get_trace(request: Request):
    return {"rows": trace.get(_sid(request))}


# ---------------- break-it buttons ----------------

async def _local_junk_token(**over) -> str:
    body = {
        "sub": "alice", "iss": oidc.ISSUER, "aud": "agent2-github-agent",
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

    if kind == "wrong-aud":
        token = await _local_junk_token(aud="some-other-service")
        label = "Break: token signed by the wrong key, aud=some-other-service"
    elif kind == "raw-user":
        token = sess.get("user_token")
        label = "Break: raw user token (no exchange), aud has no agent2"
    elif kind == "expired":
        token = await _local_junk_token(exp=int(time.time()) - 60)
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
        "scope": "github.act",
    }
    async with httpx.AsyncClient(timeout=15) as hc:
        r = await hc.post(oidc.TOKEN_URL, data=data)
    r.raise_for_status()
    return r.json()["access_token"]
