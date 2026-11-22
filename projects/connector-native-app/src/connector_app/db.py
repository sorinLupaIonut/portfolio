"""db.py — the persistence layer for user memory.

One row per person, keyed **only** by the verified OAuth `sub`. Every function here takes a
`sub` that the caller obtained from `auth.verified_claims` (or, under `AUTH_DISABLED=1`, the
fixed `DEV_SUB`). It is never an id the model chose and passed as a tool argument.

That is invariant 3 (prove, don't trust) expressed in the data layer: the database physically
cannot be asked to read or write someone else's row, because the identity is not a parameter
the model controls — it is sealed in from the verified token upstream.

Two tables (created on the Neon dev branch at Concept 5):

    users(sub pk, created_at)
    user_state(sub pk -> users.sub, state jsonb, updated_at)

`state` is a single JSON document: {"reading_list": [ids], "finished": [ids], "notes": {id: text}}.
Writers read-modify-write the whole document; there is one document per user.

Connections are opened per call. For this app (short, infrequent queries against Neon's pooled
endpoint) that is the simplest correct thing; a connection pool is an optimisation we don't need.
"""

import os
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

type State = dict[str, Any]


def _empty_state() -> State:
    """A fresh, independent empty state (never a shared mutable default)."""
    return {"reading_list": [], "finished": [], "notes": {}}


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        # Fail closed (invariant 4): no store configured means no state, not invented state.
        raise RuntimeError("DATABASE_URL is not set — the store is unavailable")
    return url


def _connect() -> psycopg.Connection:
    return psycopg.connect(_dsn())


def ensure_user(sub: str) -> None:
    """Record that we've seen this person. Idempotent; safe to call on every begin_session."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into users (sub) values (%s) on conflict (sub) do nothing",
            (sub,),
        )


def read_state(sub: str) -> State:
    """Return this user's saved state, or a fresh empty state if they have no row yet."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("select state from user_state where sub = %s", (sub,))
        row = cur.fetchone()
    if row is None:
        return _empty_state()
    return row[0]


def save_state(sub: str, state: State) -> State:
    """Upsert this user's whole state document. Guarantees the users row exists first (FK)."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into users (sub) values (%s) on conflict (sub) do nothing",
            (sub,),
        )
        cur.execute(
            """
            insert into user_state (sub, state) values (%s, %s)
            on conflict (sub) do update set state = excluded.state, updated_at = now()
            """,
            (sub, Jsonb(state)),
        )
    return state
