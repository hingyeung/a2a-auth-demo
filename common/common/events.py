"""Live auth events for the Auth Arcade page.

One event is one step of the auth story: a redirect, a token minted, a check
passed or failed. The arcade page draws each one as it arrives.

Two halves:
  - Bus: lives in agent1. Keeps a short history per browser session and
    pushes new events to any open SSE stream for that session.
  - forward(): used by agent2. agent2 only knows the user's `sub`, never the
    browser session, so it posts its events to agent1, and agent1 routes
    them to every session logged in as that `sub`.

Events never carry a raw token, only decoded claims (see token_view).
Sending events must never break the auth flow, so every failure is swallowed.
"""
from __future__ import annotations

import asyncio
import itertools
import os
import time
from typing import Any

import httpx

from . import jwt_verify

HISTORY_MAX = 200

# The claims a student needs to see on the token card. Everything else is
# still there under "full", shown when the card is clicked.
KEY_CLAIMS = ("sub", "preferred_username", "aud", "azp", "act", "scope", "iss", "exp")


def token_view(token: str | None, label: str) -> dict[str, Any] | None:
    """Decode a token for display. Opaque tokens (GitHub's) get a short head only."""
    if not token:
        return None
    try:
        full = jwt_verify.unverified(token)
    except Exception:  # noqa: BLE001
        return {"label": label, "opaque": True, "head": token[:10] + "..."}
    claims = {k: full[k] for k in KEY_CLAIMS if k in full}
    roles = (full.get("realm_access") or {}).get("roles")
    if roles is not None:
        claims["realm_access.roles"] = roles
    return {"label": label, "opaque": False, "claims": claims, "full": full}


def make(step: str, *, leg: str, kind: str, src: str, dst: str | None = None,
         note: str = "", **extra: Any) -> dict[str, Any]:
    """Build one event. extra may hold token, check, http, data."""
    ev = {"step": step, "leg": leg, "kind": kind, "from": src, "to": dst or src, "note": note}
    ev.update({k: v for k, v in extra.items() if v is not None})
    return ev


class Bus:
    """In-memory, single-process event bus (agent1 runs one uvicorn worker)."""

    def __init__(self) -> None:
        self._seq = itertools.count(1)
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._subs: dict[str, set[asyncio.Queue]] = {}

    def emit(self, key: str, event: dict[str, Any]) -> None:
        if not key:
            return
        ev = dict(event, seq=next(self._seq), ts=time.time())
        hist = self._history.setdefault(key, [])
        hist.append(ev)
        del hist[:-HISTORY_MAX]
        for q in list(self._subs.get(key, ())):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    def history(self, key: str, since: int = 0) -> list[dict[str, Any]]:
        return [e for e in self._history.get(key, []) if e["seq"] > since]

    def clear(self, key: str) -> None:
        self._history.pop(key, None)

    def subscribe(self, key: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.setdefault(key, set()).add(q)
        return q

    def unsubscribe(self, key: str, q: asyncio.Queue) -> None:
        self._subs.get(key, set()).discard(q)


# ---------------- agent2 side: forward events to agent1 ----------------

_INGEST_URL = os.environ.get("EVENTS_INGEST_URL", "")
_KEY = os.environ.get("EVENTS_KEY", "")


async def forward(sub: str | None, event: dict[str, Any]) -> None:
    """Post one event to agent1. Awaited (not fire and forget) so agent2's
    events reach agent1 in the order they happened. Short timeout, and any
    error is ignored: the arcade is a viewer, never a dependency."""
    if not sub or not _INGEST_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=1.5) as hc:
            await hc.post(_INGEST_URL, json={"sub": sub, "event": event},
                          headers={"x-events-key": _KEY})
    except Exception:  # noqa: BLE001
        pass
