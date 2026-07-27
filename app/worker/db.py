"""Neon Postgres access for the worker: a narrow set of typed functions.

The worker reaches Postgres through ``DATABASE_URL`` only. The Neon MCP is a
build-time tool for provisioning and inspection and is never on a runtime path.

Why the synchronous pool behind ``asyncio.to_thread``: psycopg's async mode
refuses to run on Windows' default ProactorEventLoop, and this worker has to run
under uvicorn (and later under Inngest) unchanged. Forcing a SelectorEventLoop
would trade a portable worker for a platform quirk. These queries are single-row
and sub-millisecond, so the thread hop costs less than the fight.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# The closed action vocabulary. It mirrors the CHECK constraint on audit_log, so
# a typo raises here in Python instead of arriving as a bad row (it cannot: the
# database would reject it too, but the Python error names the caller).
ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "email_received",
        "reply_drafted",
        "refund_requested",
        "refund_approved",
        "refund_rejected",
        "refund_issued",
        "health_check_run",
    }
)

SEED_CUSTOMERS: Final[tuple[tuple[str, str, str], ...]] = (
    ("cus_acme", "billing@acme.example", "enterprise"),
    ("cus_globex", "ops@globex.example", "enterprise"),
    ("cus_initech", "support@initech.example", "pro"),
    ("cus_umbrella", "accounts@umbrella.example", "pro"),
    ("cus_hooli", "hello@hooli.example", "free"),
)


@dataclass(frozen=True, slots=True)
class Customer:
    id: str
    email: str
    tier: str


_pool: ConnectionPool | None = None


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        url = os.getenv("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL is not set; the worker cannot reach Neon.")
        _pool = ConnectionPool(url, min_size=1, max_size=5, kwargs={"row_factory": dict_row})
        _pool.wait(timeout=30)
    return _pool


def close_pool() -> None:
    """Close the pool. Safe to call when it was never opened."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with _get_pool().connection() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


async def seed_customers_if_empty() -> int:
    """Insert the five sample customers when the table is empty. Returns rows added."""

    def work() -> int:
        with _get_pool().connection() as conn:
            cur = conn.execute("SELECT count(*) AS n FROM customers")
            row = cur.fetchone()
            if row and row["n"]:
                return 0
            conn.cursor().executemany(
                "INSERT INTO customers (id, email, tier) VALUES (%s, %s, %s)"
                " ON CONFLICT (id) DO NOTHING",
                SEED_CUSTOMERS,
            )
            return len(SEED_CUSTOMERS)

    return await asyncio.to_thread(work)


async def list_customers(tiers: tuple[str, ...] | None = None) -> list[Customer]:
    """All customers, or only those in the given tiers."""

    def work() -> list[Customer]:
        if tiers:
            rows = _query(
                "SELECT id, email, tier FROM customers WHERE tier = ANY(%s) ORDER BY id",
                (list(tiers),),
            )
        else:
            rows = _query("SELECT id, email, tier FROM customers ORDER BY id")
        return [Customer(**row) for row in rows]

    return await asyncio.to_thread(work)


async def find_customer_by_email(email: str) -> Customer | None:
    """The customer who sent an email, matched case-insensitively, or None."""

    def work() -> Customer | None:
        rows = _query(
            "SELECT id, email, tier FROM customers WHERE lower(email) = lower(%s)",
            (email.strip(),),
        )
        return Customer(**rows[0]) if rows else None

    return await asyncio.to_thread(work)


async def find_customer(customer_id: str) -> Customer | None:
    """One customer by id, or None."""

    def work() -> Customer | None:
        rows = _query("SELECT id, email, tier FROM customers WHERE id = %s", (customer_id,))
        return Customer(**rows[0]) if rows else None

    return await asyncio.to_thread(work)


async def write_audit(
    action: str,
    *,
    customer_id: str | None = None,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
    run_id: str | None = None,
) -> bool:
    """Append one audit row. Returns False when idempotency dropped a duplicate.

    ``created_at`` comes from Postgres' ``now()``, never from Python. That is not
    a style choice: once this runs inside ``step.run``, a ``datetime.now()`` in
    the step body would make a replay diverge from the original run.

    When ``request_id`` is given, the partial unique index on
    ``(action, request_id)`` makes the insert idempotent, which is what stops a
    replay from writing a second refund row.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown audit action {action!r}; allowed: {sorted(ACTIONS)}")

    def work() -> bool:
        with _get_pool().connection() as conn:
            cur = conn.execute(
                "INSERT INTO audit_log (customer_id, action, details, request_id, run_id)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT DO NOTHING"
                " RETURNING id",
                (customer_id, action, Jsonb(details or {}), request_id, run_id),
            )
            return cur.fetchone() is not None

    return await asyncio.to_thread(work)


async def recent_audit(limit: int = 20, run_id: str | None = None) -> list[dict[str, Any]]:
    """Read the audit trail back, newest first. For verification, not for the agent."""

    def work() -> list[dict[str, Any]]:
        if run_id:
            rows = _query(
                "SELECT id, customer_id, action, details, request_id, run_id, created_at"
                " FROM audit_log WHERE run_id = %s ORDER BY id",
                (run_id,),
            )
        else:
            rows = _query(
                "SELECT id, customer_id, action, details, request_id, run_id, created_at"
                " FROM audit_log ORDER BY id DESC LIMIT %s",
                (limit,),
            )
        return [dict(row) | {"details": json.loads(json.dumps(row["details"]))} for row in rows]

    return await asyncio.to_thread(work)
