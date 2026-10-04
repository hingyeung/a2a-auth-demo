"""agent1, the orchestrator agent. Logs alice in, exchanges her token, and calls
agent2 (the repository agent) over A2A."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
import jwt as pyjwt
from a2a.client.errors import A2AClientHTTPError
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer

from common import events, jwt_verify, trace

import a2a_client
import exchange
import oidc

app = FastAPI()

SESSION_SECRET = os.environ["SESSION_SECRET"]
signer = URLSafeSerializer(SESSION_SECRET, salt="sid")
AGENT2_INTERNAL = os.environ["AGENT2_INTERNAL_URL"]

# session id -> {user_token, id_claims, pkce, state, obo}
SESSIONS: dict[str, dict] = {}

# Live events for the Auth Arcade page (/arcade), keyed by session id.
BUS = events.Bus()
EVENTS_KEY = os.environ.get("EVENTS_KEY", "")
# Pages a login or logout may send the browser back to.
RETURN_PAGES = {"arcade": "/arcade"}


def _ev(sid: str, step: str, **kw) -> None:
    BUS.emit(sid, events.make(step, **kw))


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
async def index(request: Request):
    # Keycloak's logout can only send the browser back to "/". If the logout
    # started on the arcade page, a short-lived cookie says to go back there.
    back = RETURN_PAGES.get(request.cookies.get("return_to", ""))
    if back:
        resp = RedirectResponse(back)
        resp.delete_cookie("return_to")
        return resp
    with open("static/index.html") as f:
        return f.read()


@app.get("/arcade", response_class=HTMLResponse)
async def arcade(request: Request):
    with open("static/arcade/index.html") as f:
        resp = HTMLResponse(f.read())
    # The event stream is keyed by session, so make sure there is one.
    sid = _sid(request)
    if not sid or sid not in SESSIONS:
        _new_session(resp)
    return resp


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/login")
async def login(request: Request, user: str = "", next: str = ""):
    resp = RedirectResponse("/placeholder")
    sid = _sid(request)
    if not sid or sid not in SESSIONS:
        sid = _new_session(resp)
    verifier, challenge = oidc.new_pkce()
    state = secrets.token_urlsafe(16)
    SESSIONS[sid].update(pkce=verifier, state=state, next=RETURN_PAGES.get(next, "/"))
    # A login starts a new story on the arcade page.
    BUS.clear(sid)
    _ev(sid, "h2a.login.start", leg="H2A", kind="request", src="user", dst="keycloak",
        note=f"{user or 'The user'} is sent to Keycloak to log in. Authorization code "
             "flow with PKCE: the orchestrator agent keeps a secret verifier, Keycloak only "
             "sees its hash.",
        data={"user": user, "code_challenge_method": "S256", "scope": "openid profile email"})
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
    # The orchestrator agent trusts its own login result. The trace panel shows the decoded body.
    claims = jwt_verify.unverified(user_token)
    sess["user_token"] = user_token
    sess["id_token"] = tok.get("id_token")
    sess["user_sub"] = claims.get("sub")
    sess["user_name"] = claims.get("preferred_username")
    sess["has_github_role"] = exchange.has_github_caller_role(claims)
    sess["roles"] = (claims.get("realm_access") or {}).get("roles", [])
    _ev(sid, "h2a.login.token", leg="H2A", kind="token", src="keycloak", dst="agent1",
        note=f"Password checked. Keycloak gives the orchestrator agent (Keycloak client "
             f"agent1-orchestrator, the azp on this token) a user token for "
             f"{sess['user_name']}. The sub is a UUID, not the name.",
        token=events.token_view(user_token, "User token"), data={"user": sess["user_name"]})
    # The trace is kept across steps (login, then every ask) until the user
    # clicks "clear token trace". Login is itself one step, so it gets a row.
    trace.add(sid, "H2A user token (browser -> orchestrator agent)", user_token,
              note=f"Keycloak issued this to {sess['user_name']} after login.")
    return RedirectResponse(sess.pop("next", "/"))


@app.get("/logout")
async def logout(request: Request, next: str = ""):
    """A real navigation, not a background fetch: Keycloak keeps its own SSO
    session cookie in the browser, separate from the orchestrator agent's. Just
    clearing the orchestrator agent's session left that cookie alive, so the next login silently
    picked up the previous user again (see README's "Is this user allowed
    to?" - same underlying lesson: the orchestrator agent's session and Keycloak's session
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
        sess.pop("roles", None)
    BUS.clear(sid)
    back = RETURN_PAGES.get(next)
    if id_token:
        q = urlencode({
            "id_token_hint": id_token,
            "post_logout_redirect_uri": f"{oidc.BASE_URL}/",
        })
        resp = RedirectResponse(f"{oidc.LOGOUT_URL}?{q}")
        # Only the Keycloak round trip lands on "/", which reads this cookie.
        if back:
            resp.set_cookie("return_to", next, max_age=120, httponly=True, samesite="lax")
        return resp
    return RedirectResponse(back or "/")


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

    # Give the repository agent a clean slate for this one call so its rows are
    # only the ones this ask produces. The orchestrator agent's own trace log is never cleared here -
    # it only grows, step by step, until the user clicks "clear token trace".
    await _reset_agent2_trace(sess.get("user_sub", ""))

    has_role = sess.get("has_github_role", False)
    who = sess.get("user_name") or "the user"
    _ev(sid, "ask.start", leg="H2A", kind="request", src="user", dst="agent1",
        note=f'{who} asks the orchestrator agent: "{prompt}". The browser sends it with the '
             f'orchestrator agent\'s session cookie.',
        data={"prompt": prompt})
    _ev(sid, "h2a.role.check", leg="H2A", kind="check", src="agent1",
        note=(f"The orchestrator agent reads {who}'s roles. github-caller is there, so it will ask "
              "Keycloak for the github.act scope." if has_role else
              f"The orchestrator agent reads {who}'s roles. No github-caller role, so it will NOT "
              "ask for the github.act scope."),
        check={"name": "github-caller role", "claim": "realm_access.roles",
               "expected": exchange.GITHUB_CALLER_ROLE, "actual": sess.get("roles", []),
               "ok": has_role})
    _ev(sid, "a2a.exchange.request", leg="A2A", kind="request", src="agent1", dst="keycloak",
        note="Token exchange (RFC 8693). The orchestrator agent hands over the user token and "
             "its own client secret, and asks for a new token for the repository agent "
             "(Keycloak audience agent2-github-agent). The repository agent uses an MCP "
             "tool to read the user's GitHub repos.",
        data={"audience": exchange.AUDIENCE,
              "scope": exchange.requested_scope(has_role).split()})
    try:
        obo = await exchange.obo_token(sess["user_token"], include_github_scope=has_role)
    except exchange.ExchangeError as e:
        # Keycloak refused to exchange this subject_token. The most common
        # cause by far is that the cached login token went stale (usually
        # it just expired - see README). The exact Keycloak error still
        # goes into the trace note for anyone who wants the raw detail.
        trace.add(sid, "A2A token exchange (RFC 8693)", None, note=str(e), ok=False)
        _ev(sid, "a2a.exchange.error", leg="A2A", kind="result", src="keycloak", dst="agent1",
            note="Keycloak refused the exchange. The login token most likely expired. "
                 "Log in again.", http={"status": 401, "body": str(e)[:300]})
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
    trace.add(sid, "A2A OBO token (orchestrator agent -> repository agent)", obo_token,
              note=f"RFC 8693 exchange. aud is now agent2-github-agent (the repository agent). {role_note}")
    _ev(sid, "a2a.exchange.token", leg="A2A", kind="token", src="keycloak", dst="agent1",
        note="Keycloak gives back an on-behalf-of (OBO) token. Same sub, but aud is now "
             "agent2-github-agent (the repository agent), and act/azp name agent1-orchestrator "
             "(the orchestrator agent) as the caller.",
        token=events.token_view(obo_token, "OBO token"),
        data={"before": events.token_view(sess["user_token"], "User token")})
    _ev(sid, "a2a.call", leg="A2A", kind="request", src="agent1", dst="agent2",
        note="The orchestrator agent calls the repository agent over A2A (JSON-RPC "
             "message/send) with the OBO token as a Bearer token.", data={"method": "message/send"})

    try:
        result = await a2a_client.ask_agent2(obo_token, prompt)
    except A2AClientHTTPError as e:
        # The repository agent's middleware refused the OBO token before the executor ever
        # ran. The streaming a2a-sdk client checks the response's
        # Content-Type before it checks the HTTP status code, so a plain
        # JSON error body reads to it as "not an SSE stream" and it raises
        # a made-up 400 rather than the repository agent's real status. Don't show that
        # to the user - reissue the same call as one plain HTTP request
        # (the same path the break-it buttons use) to get the repository agent's actual
        # status and body, and report those instead.
        raw = await a2a_client.raw_a2a_call(obo_token, repeat=True)
        await _merge_agent2_trace(sid)
        trace.add(sid, "A2A call to the repository agent refused", None,
                  note=f"The repository agent answered {raw['status']}: {raw['body']}", ok=False)
        _ev(sid, "a2a.refused", leg="A2A", kind="result", src="agent2", dst="user",
            note=f"The repository agent refused the call with {raw['status']}. Identity was fine. "
                 "Permission was not.",
            http={"status": raw["status"], "body": raw["body"]})
        raise HTTPException(
            raw["status"] if raw["status"] >= 400 else 502,
            f"The repository agent refused this call ({raw['status']}): {raw['body']}",
        )

    # Pull the repository agent's own trace rows and merge them in.
    await _merge_agent2_trace(sid)

    if result.get("input_required"):
        _ev(sid, "a2a.input_required", leg="CONSENT", kind="result", src="agent1", dst="user",
            note="The repository agent needs the user's OK for GitHub first. Open the consent link, "
                 "then ask again.", data={"ticket_url": result.get("ticket_url")})
    else:
        _ev(sid, "a2a.done", leg="A2A", kind="result", src="agent1", dst="user",
            note="The repository agent's answer comes back through the orchestrator agent to "
                 "the user.",
            data={"states": [s["state"] for s in result.get("states", [])],
                  "final_text": (result.get("final_text") or "")[:1500]})

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
            # The repository agent's /debug/trace never sends the raw token back over the
            # wire, only the already-decoded summary/body and a truncated
            # token_head. Keep that as-is instead of re-running trace.add()
            # with no token, which would silently blank the row out.
            trace.add_precomputed(sid, row)
    except Exception as e:  # noqa: BLE001
        trace.add(sid, "merge repository agent trace", None, note=f"could not read repository agent trace: {e}", ok=False)


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


# ---------------- Auth Arcade event stream ----------------

@app.get("/events")
async def event_stream(request: Request, since: int = 0):
    """Server-sent events. First the stored history for this session (so a
    page that loads after the login redirect still sees the login), then
    live events. Each event has an id, so EventSource resumes after a
    dropped connection without replaying what it already has."""
    sid = _sid(request)
    if not sid or sid not in SESSIONS:
        raise HTTPException(401, "no session, open /arcade first")
    last = request.headers.get("last-event-id")
    if last and last.isdigit():
        since = int(last)
    q = BUS.subscribe(sid)

    def frame(ev: dict) -> str:
        return f"id: {ev['seq']}\nevent: auth\ndata: {json.dumps(ev)}\n\n"

    async def gen():
        try:
            yield "retry: 2000\n\n"
            sent = since
            for ev in BUS.history(sid, since):
                sent = ev["seq"]
                yield frame(ev)
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if ev["seq"] > sent:
                    sent = ev["seq"]
                    yield frame(ev)
        finally:
            BUS.unsubscribe(sid, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"cache-control": "no-cache", "x-accel-buffering": "no"})


@app.post("/events/clear")
async def events_clear(request: Request):
    BUS.clear(_sid(request))
    return {"ok": True}


@app.post("/events/ingest")
async def events_ingest(request: Request):
    """The repository agent (agent2) posts its events here. It only knows the user's sub, so the
    event goes to every browser session logged in as that user."""
    if not EVENTS_KEY or request.headers.get("x-events-key") != EVENTS_KEY:
        raise HTTPException(403, "bad events key")
    body = await request.json()
    sub, ev = body.get("sub"), body.get("event") or {}
    for sid, sess in list(SESSIONS.items()):
        if sub and sess.get("user_sub") == sub:
            BUS.emit(sid, ev)
    return {"ok": True}


@app.get("/.well-known/agent-card.json")
async def agent1_card():
    """The orchestrator agent is not a full A2A server (its /ask endpoint is plain REST, not
    JSON-RPC). This card exists only so the web page can show it next to
    the repository agent's real A2A card, for comparison."""
    return {
        "name": "Orchestrator agent",
        "description": "Logs the end user in, exchanges their token (RFC 8693), "
                        "and calls the repository agent over A2A on their behalf. Not itself an "
                        "A2A server - /ask is a plain REST endpoint for this demo's web page.",
        "url": os.environ["AGENT1_BASE_URL"],
        "version": "0.1.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{
            "id": "ask",
            "name": "Ask (forwards to the repository agent)",
            "description": "Takes a prompt from the browser, exchanges the user's "
                            "token, and forwards it to the repository agent over A2A.",
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
        label = "Break: raw user token (no exchange), aud is not the repository agent (agent2-github-agent)"
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
    trace.add(sid, label, token, note=f"The repository agent answered {res['status']}", ok=False)
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
