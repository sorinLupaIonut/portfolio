# `customer-data` MCP server

The Worker's only route to business data. Three tools, no general SQL tool.

```bash
uv run python customer-data-mcp/server.py
```

Listens on `http://127.0.0.1:8000/mcp` (`CUSTOMER_DATA_HOST` / `CUSTOMER_DATA_PORT`
override). It reads `OPENAI_API_KEY` and `NEON_DATABASE_URL` from the project's
`.env` and refuses to start without them.

## Two terminals

This is a streamable-HTTP service, not a subprocess the Worker spawns. It has to
be running **before** the Worker starts:

| Terminal | Command |
| --- | --- |
| 1 | `uv run python customer-data-mcp/server.py` |
| 2 | `uv run python main.py` |

Stop the server and the Worker's tool calls fail with a connection error — which
looks nothing like a wrong answer, and that is the point.

## The tools

| Tool | Writes? | What it is for |
| --- | --- | --- |
| `lookup_customer(customer_id)` | no | tier and open-ticket count, by UUID |
| `find_similar_resolved_tickets(description, limit)` | no | semantic search over past resolved cases |
| `issue_refund(order_id, amount_cents, reason)` | **yes** | refund + order status + audit row, one transaction |

There is deliberately no `run_sql`, and no argument through which SQL could
reach the database: every query is fixed in `server.py` and every value is a
bound parameter.

## Things that are load-bearing

**`statement_cache_size=0` on the pool.** Neon's pooled endpoint is PgBouncer in
transaction mode; it hands one server session to different clients, so a cached
prepared statement can arrive on a connection that never prepared it. Without
this the first query errors.

**`init=register_vector` on the pool.** pgvector registration is per-connection
and a pool hands out many. Miss it and the embedding column reads back as
garbage rather than raising.

**`EMBEDDING_MODEL` must match the seed.** `text-embedding-3-small`, the same
model `seed/seed_and_embed.py` wrote with. A mismatch does not fail; it returns
confident, plausible, wrong rankings.

**`issue_refund` refuses loudly and records the refusal.** A blocked refund
commits a `refund_blocked` audit row before the error is raised, so an attempt
to over-refund is as traceable as a payment.

## Not here yet

- **The approval gate on `issue_refund`** — Decision 9, via `require_approval`
  on the Worker's `MCPServerStreamableHttp`.
- **No caller identity.** `refunds.issued_by` and `audit_log.actor` are both the
  constant `customer-data-mcp`, and `refunds.conversation_id` is NULL, because
  the server does not know which conversation it is serving.
- **`orders` is empty.** Every seeded ticket has `order_id = NULL` and the order
  numbers live as free text inside ticket bodies. `issue_refund` works — proved
  against throwaway orders — but has no real data to act on.
