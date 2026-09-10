"""Append-only in-memory trace log, keyed by browser session id.

Each hop adds one row. The web page reads the rows and draws a table.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from . import jwt_verify

_lock = threading.Lock()
_log: dict[str, list[dict[str, Any]]] = {}


def add(
    session_id: str,
    hop: str,
    token: str | None,
    *,
    note: str = "",
    ok: bool = True,
) -> None:
    body: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    if token:
        try:
            body = jwt_verify.unverified(token)
            summary = {
                "sub": body.get("sub"),
                "azp": body.get("azp"),
                "act": body.get("act"),
                "aud": body.get("aud"),
                "iss": body.get("iss"),
                "scope": body.get("scope"),
            }
        except Exception as exc:  # noqa: BLE001
            summary = {"error": f"not a JWT: {exc}"}
    row = {
        "hop": hop,
        "ts": time.time(),
        "ok": ok,
        "note": note,
        "summary": summary,
        "body": body,
        "token_head": (token[:24] + "...") if token else None,
    }
    with _lock:
        _log.setdefault(session_id, []).append(row)


def get(session_id: str) -> list[dict[str, Any]]:
    with _lock:
        return list(_log.get(session_id, []))


def clear(session_id: str) -> None:
    with _lock:
        _log.pop(session_id, None)
