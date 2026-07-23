"""The `customer-data` MCP server: the Worker's only route to business data.

Three tools and nothing else. No `run_sql`, and no argument anywhere through
which SQL could be smuggled -- every query is fixed in this file and every value
arrives as a bound parameter. That narrow surface is the whole point: the Worker
can do exactly three things to the database, not anything SQL allows.

The admin Neon MCP server stays on the build plane and is never wired in here.

Runs as its own process, on streamable HTTP, so it is a real addressable service:

    uv run python customer-data-mcp/server.py
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg
from dotenv import load_dotenv
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from openai import AsyncOpenAI
from pgvector import Vector
from pgvector.asyncpg import register_vector
from pydantic import BaseModel, ConfigDict, Field

# The same model the seed corpus was written with. A mismatch here does not
# raise: the query lands somewhere unrelated in the same space and the distances
# still look like ordinary numbers, so the rankings come back confident and
# wrong. Changing this means re-embedding everything in `embeddings`.
EMBEDDING_MODEL = "text-embedding-3-small"

# tickets.status allows open / pending / resolved / escalated. "Open" here means
# everything that is not resolved -- counting `escalated` as closed would hide
# exactly the tickets a support agent most needs to know about.
OPEN_TICKET_STATUSES = ("open", "pending", "escalated")

# Written to refunds.issued_by and audit_log.actor. It is this server acting;
# there is no caller identity to record until the Worker passes one.
ACTOR = "customer-data-mcp"

HOST = os.getenv("CUSTOMER_DATA_HOST", "127.0.0.1")
PORT = int(os.getenv("CUSTOMER_DATA_PORT", "8000"))


@dataclass
class AppContext:
    """Process-wide resources, created once at startup."""

    pool: asyncpg.Pool
    openai: AsyncOpenAI


@asynccontextmanager
async def app_lifespan(_: FastMCP) -> AsyncIterator[AppContext]:
    """Open the connection pool once, for the life of the server.

    On streamable HTTP the lifespan is entered once at manager startup and
    shared across every request, which is what a pool wants. Per-request setup
    would belong in the tool body instead.
    """
    load_dotenv()
    for key in ("OPENAI_API_KEY", "NEON_DATABASE_URL"):
        if not os.getenv(key):
            sys.exit(f"{key} is not set. Put it in .env and try again.")

    pool = await asyncpg.create_pool(
        os.environ["NEON_DATABASE_URL"],
        # Neon's pooled endpoint is PgBouncer in transaction mode: it hands one
        # server session to different clients between transactions, so a cached
        # prepared statement can arrive on a connection that never prepared it.
        # Without this the very first query errors.
        statement_cache_size=0,
        # register_vector is per-connection, not per-pool, and a pool hands out
        # many. asyncpg runs `init` on every new connection, and its signature is
        # exactly what register_vector takes. Miss this and the embedding column
        # reads back as garbage rather than raising.
        init=register_vector,
        min_size=1,
        max_size=5,
    )
    try:
        yield AppContext(pool=pool, openai=AsyncOpenAI())
    finally:
        await pool.close()


mcp = FastMCP(
    "customer_data_mcp",
    instructions=(
        "Scoped access to the customer-support system of record. Look a customer "
        "up by id, search past resolved tickets for precedent, and issue a refund. "
        "There is no general query tool; these three are the whole surface."
    ),
    lifespan=app_lifespan,
    # Each call is an independent request with no session to keep, so any worker
    # can serve any request and a second copy can be started under load.
    stateless_http=True,
    json_response=True,
    host=HOST,
    port=PORT,
)


def _ctx(ctx: Context) -> AppContext:
    return ctx.request_context.lifespan_context


async def _insert_audit(
    conn: asyncpg.Connection,
    *,
    action: str,
    target: str,
    payload: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Write one audit row on the caller's connection.

    Takes a connection rather than the pool on purpose: the caller has an open
    transaction, and the audit row has to land inside it.
    """
    await conn.execute(
        """
        INSERT INTO audit_log (conversation_id, actor, action, target, payload, result)
        VALUES (NULL, $1, $2, $3, $4::jsonb, $5::jsonb)
        """,
        ACTOR,
        action,
        target,
        json.dumps(payload),
        json.dumps(result),
    )


# --------------------------------------------------------------------------
# lookup_customer
# --------------------------------------------------------------------------


class LookupCustomerInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    customer_id: UUID = Field(
        ...,
        description="The customer's UUID primary key, e.g. '3f2a...-...'. Not an email.",
    )


class CustomerProfile(BaseModel):
    customer_id: str = Field(description="The customer's UUID.")
    email: str = Field(description="Email address on the account.")
    tier: str = Field(description="Account tier: 'standard' unless set explicitly.")
    open_tickets: int = Field(description="Tickets not yet resolved (open, pending or escalated).")


@mcp.tool(
    name="lookup_customer",
    annotations=ToolAnnotations(
        title="Look up a customer profile",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def lookup_customer(params: LookupCustomerInput, ctx: Context) -> CustomerProfile:
    """Fetch one customer's profile and their open-ticket count by UUID.

    Returns who the customer is and how much latitude you have with them: the
    account tier drives refund authority and escalation routing, and the
    open-ticket count tells you whether this is a first contact or the fourth
    complaint this month.

    Args:
        customer_id: the customer's UUID primary key.

    Returns:
        customer_id, email, tier ('standard' unless set), open_tickets (a count
        of tickets in status open, pending or escalated).

    Call this when: you have a customer UUID and need their tier or history
    before deciding how to handle a case, or before escalating.

    Do NOT call this when:
      - you only have an email address or a name. This takes a UUID and there is
        no search-by-email tool; ask the user for the id rather than guessing one.
      - you want the customer's tickets themselves. This returns a count, not a list.

    Errors: raises if no customer has that id. It does not return an empty
    profile, because an empty profile reads like a customer with no history.
    """
    app = _ctx(ctx)
    row = await app.pool.fetchrow(
        """
        SELECT c.id, c.email,
               COALESCE(c.metadata->>'tier', 'standard') AS tier,
               count(t.id) FILTER (WHERE t.status = ANY($2::text[])) AS open_tickets
        FROM customers c
        LEFT JOIN tickets t ON t.customer_id = c.id
        WHERE c.id = $1
        GROUP BY c.id, c.email
        """,
        params.customer_id,
        list(OPEN_TICKET_STATUSES),
    )
    if row is None:
        raise ToolError(
            f"No customer with id {params.customer_id}. Check the id, or ask the "
            f"user for it -- this tool cannot search by email or name."
        )
    return CustomerProfile(
        customer_id=str(row["id"]),
        email=row["email"],
        tier=row["tier"],
        open_tickets=row["open_tickets"],
    )


# --------------------------------------------------------------------------
# find_similar_resolved_tickets
# --------------------------------------------------------------------------


class SimilarTicketsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    description: str = Field(
        ...,
        description=(
            "The problem in the customer's own words, e.g. 'package arrived empty, "
            "just packing material'. Symptoms retrieve better than diagnoses."
        ),
        min_length=3,
        max_length=2000,
    )
    limit: int = Field(
        default=5,
        description="How many matches to return, closest first.",
        ge=1,
        le=20,
    )


class SimilarTicket(BaseModel):
    ticket_id: str = Field(description="UUID of the resolved ticket.")
    subject: str = Field(description="How the ticket was filed, one line.")
    resolution: str = Field(description="What was actually done, and how it ended.")
    category: str | None = Field(description="Issue category, e.g. 'refund', 'shipping'.")
    distance: float = Field(
        description="Cosine distance, lower is closer. Under ~0.5 is a real match in this corpus."
    )


@mcp.tool(
    name="find_similar_resolved_tickets",
    annotations=ToolAnnotations(
        title="Search past resolved tickets",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def find_similar_resolved_tickets(
    params: SimilarTicketsInput, ctx: Context
) -> list[SimilarTicket]:
    """Search the library of past resolved tickets for cases like this one.

    Semantic search, not keyword search: it matches on what the problem *is*, so
    a description that shares no words with the stored ticket still finds it.
    Use it so an answer repeats a resolution that already worked on a real
    customer instead of inventing a fresh one.

    Args:
        description: the problem in the customer's own words.
        limit: how many to return, 1-20, default 5.

    Returns:
        A list ordered closest-first, each with ticket_id, subject, resolution,
        category and distance. An empty list means the library has nothing at
        all, which is different from having nothing close.

    Call this when: the user describes a problem, complaint or symptom, and
    before drafting any reply to a customer.

    Do NOT call this when:
      - you want one specific ticket you already have the id of. This searches by
        meaning and will return neighbours, not that ticket.
      - you need counts, trends or reporting over tickets. This returns a handful
        of individual cases, not statistics.

    Reading the distances: lower is closer. In this corpus a genuine match lands
    roughly 0.35-0.50 and unrelated text sits above 0.6, but the absolute number
    moves with how long the description is. The more reliable signal is the gap
    between the first and second result: when they are within about 0.05 of each
    other there is no real precedent, however small the numbers look.
    """
    app = _ctx(ctx)
    embedded = await app.openai.embeddings.create(
        model=EMBEDDING_MODEL, input=[params.description]
    )
    query_vector = Vector(embedded.data[0].embedding)

    rows = await app.pool.fetch(
        """
        SELECT t.id, t.subject, t.resolution,
               t.metadata->>'category' AS category,
               e.embedding <=> $1 AS distance
        FROM embeddings e
        JOIN documents d ON d.id = e.document_id
        JOIN tickets   t ON t.id = (d.metadata->>'ticket_id')::uuid
        WHERE d.source = 'past_case' AND t.status = 'resolved'
        ORDER BY distance
        LIMIT $2
        """,
        query_vector,
        params.limit,
    )
    return [
        SimilarTicket(
            ticket_id=str(row["id"]),
            subject=row["subject"],
            resolution=row["resolution"] or "",
            category=row["category"],
            distance=float(row["distance"]),
        )
        for row in rows
    ]


# --------------------------------------------------------------------------
# issue_refund
# --------------------------------------------------------------------------


class IssueRefundInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    order_id: UUID = Field(..., description="UUID of the order being refunded.")
    amount_cents: int = Field(
        ...,
        description="Amount in integer cents, e.g. 16230 for $162.30. Never a float.",
        gt=0,
    )
    reason: str = Field(
        ...,
        description="Why the refund is being issued, in one sentence, for the record.",
        min_length=3,
        max_length=500,
    )


class RefundResult(BaseModel):
    refund_id: str = Field(description="UUID of the refund row just written.")
    order_id: str = Field(description="UUID of the order, now marked refunded.")
    amount_cents: int = Field(description="Amount refunded, in cents.")
    order_status: str = Field(description="The order's status after the refund.")


@mcp.tool(
    name="issue_refund",
    annotations=ToolAnnotations(
        title="Issue a refund on an order",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
async def issue_refund(params: IssueRefundInput, ctx: Context) -> RefundResult:
    """Refund money on an order, and mark the order refunded.

    This moves money and cannot be undone from here. It writes the refund, sets
    the order's status to 'refunded', and records an audit row -- all three in
    one transaction, so either every part lands or none of it does.

    Args:
        order_id: UUID of the order.
        amount_cents: amount in integer cents (16230 means $162.30).
        reason: one sentence explaining the refund, stored on the record.

    Returns:
        refund_id, order_id, amount_cents, order_status.

    Call this when: a refund has been decided and you are carrying it out.

    Do NOT call this when:
      - you are checking whether a refund is possible or how much is left. This
        performs the refund; there is no dry-run mode.
      - the refund was already issued elsewhere and you only want it recorded.
        This pays the money again.
      - you are unsure of the amount. A wrong amount is a real payment.

    Refused, with a 'refund_blocked' row written to the audit log, when the order
    does not exist, when the amount exceeds what was paid, or when the order has
    already been refunded. A refusal is recorded as carefully as a payment.
    """
    app = _ctx(ctx)
    payload = {
        "order_id": str(params.order_id),
        "amount_cents": params.amount_cents,
        "reason": params.reason,
    }

    async with app.pool.acquire() as conn:
        async with conn.transaction():
            # FOR UPDATE inside the transaction: two concurrent refunds on the
            # same order would otherwise both read 'placed' and both pay out.
            order = await conn.fetchrow(
                "SELECT id, status, total_cents FROM orders WHERE id = $1 FOR UPDATE",
                params.order_id,
            )

            if order is None:
                blocked = (
                    f"No order with id {params.order_id}. Nothing was refunded. "
                    f"Confirm the order id before retrying."
                )
            elif order["status"] == "refunded":
                blocked = (
                    f"Order {params.order_id} is already refunded. Nothing was "
                    f"refunded again. Check the existing refund before retrying."
                )
            elif params.amount_cents > order["total_cents"]:
                blocked = (
                    f"Refund of {params.amount_cents} cents exceeds the order total "
                    f"of {order['total_cents']} cents. Nothing was refunded. Re-check "
                    f"the amount; it must be in cents, not dollars."
                )
            else:
                blocked = None

            if blocked is not None:
                # The refusal is committed, not rolled back -- a blocked refund is
                # exactly the kind of event the audit trail exists to hold. The
                # error is raised after this block so the row survives.
                await _insert_audit(
                    conn,
                    action="refund_blocked",
                    target=str(params.order_id),
                    payload=payload,
                    result={"blocked": True, "reason": blocked},
                )
            else:
                refund_id = await conn.fetchval(
                    """
                    INSERT INTO refunds (order_id, amount_cents, reason, issued_by)
                    VALUES ($1, $2, $3, $4)
                    RETURNING id
                    """,
                    params.order_id,
                    params.amount_cents,
                    params.reason,
                    ACTOR,
                )
                await conn.execute(
                    "UPDATE orders SET status = 'refunded' WHERE id = $1", params.order_id
                )
                await _insert_audit(
                    conn,
                    action="refund_issued",
                    target=str(params.order_id),
                    payload=payload,
                    result={"refund_id": str(refund_id), "order_status": "refunded"},
                )

    if blocked is not None:
        raise ToolError(blocked)

    return RefundResult(
        refund_id=str(refund_id),
        order_id=str(params.order_id),
        amount_cents=params.amount_cents,
        order_status="refunded",
    )


if __name__ == "__main__":
    print(f"customer-data MCP server on http://{HOST}:{PORT}/mcp", file=sys.stderr)
    # Note the hyphen: the transport literal is 'streamable-http', not the
    # 'streamable_http' some older docs show.
    mcp.run(transport="streamable-http")
