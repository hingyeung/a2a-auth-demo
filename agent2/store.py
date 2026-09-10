"""SQLite token store. Keyed by user_sub taken from the verified OBO token."""
from __future__ import annotations

import os
import sqlite3
import time

DB_PATH = os.environ.get("AGENT2_DB", "/data/tokens.db")

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.execute(
    """CREATE TABLE IF NOT EXISTS github_tokens (
        user_sub TEXT PRIMARY KEY,
        access_token TEXT NOT NULL,
        refresh_token TEXT,
        expires_at INTEGER,
        scope TEXT,
        obtained_at INTEGER NOT NULL
    )"""
)
_conn.commit()


def put_token(user_sub: str, access_token: str, refresh_token: str | None,
              expires_in: int | None, scope: str | None) -> None:
    exp = int(time.time()) + expires_in if expires_in else None
    _conn.execute(
        "REPLACE INTO github_tokens VALUES (?,?,?,?,?,?)",
        (user_sub, access_token, refresh_token, exp, scope, int(time.time())),
    )
    _conn.commit()


def get_token(user_sub: str) -> dict | None:
    row = _conn.execute(
        "SELECT access_token, refresh_token, expires_at, scope FROM github_tokens WHERE user_sub=?",
        (user_sub,),
    ).fetchone()
    if not row:
        return None
    return {"access_token": row[0], "refresh_token": row[1], "expires_at": row[2], "scope": row[3]}
