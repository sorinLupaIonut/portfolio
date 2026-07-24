# Digital FTE — a customer-support AI Worker you can audit

A terminal chat agent turned into an **AI Worker**: it loads capabilities as portable Skills,
keeps its state and its system of record in Postgres, reaches its business data only through a
narrowly scoped MCP server, and writes an audit row for every meaningful thing it does — in the
same transaction as the action itself.

The point is not that it answers support questions. The point is that **every answer is
traceable, every write is reversible in the ledger sense, and nothing the model says can widen
its own access.**

> Built as the capstone of the *Building a Digital FTE* crash course on
> [The AI Agent Factory](https://agentfactory.panaversity.org/docs/digital-fte-crash-course)
> (Mode 2 — Manufacturing). The repository started as a bare base: a rules file, two MCP server
> declarations, and an empty `.env.example`. **The schema, the three Skills, the embedding
> pipeline, the `customer-data` MCP server, the audit layer, the guardrail, the approval gate and
> the end-to-end verification are mine**, built one decision at a time and proved with output
> before the next one started.

---

## What it demonstrates

- **Capabilities as Skills, not hard-coded tools.** Three `SKILL.md` folders
  (`summarize-ticket`, `find-similar-cases`, `escalate-with-context`) the Worker discovers by
  description and loads on demand. An allowlist means the *build-time* skills sitting in the same
  directory (`skill-creator`, `mcp-builder`, `neon-postgres`) can never leak into the runtime —
  fail-safe by construction, not by convention.
- **A scoped MCP server instead of database access.** The Worker never issues SQL against
  business tables. It calls exactly three tools — `lookup_customer`,
  `find_similar_resolved_tickets`, `issue_refund` — with no `run_sql` and no argument through
  which SQL could reach the database. The admin Neon MCP is development-plane only and is never
  wired into a runtime path.
- **A two-plane split that holds under pressure.** Build plane (provisioning, migrations on a
  branch, inspection) and runtime plane (three scoped tools) are separate on purpose; the
  interesting failure modes all come from collapsing them.
- **An audit log that is actually evidence.** `message_received`, `skill_activated`,
  `capability_invoked`, `refund_issued`, `refund_blocked`, `guardrail_tripped`, `message_sent` —
  each written inside the acting transaction, so the action and its trace land together or
  neither does. Audit runs on its own `asyncpg` pool, never through the MCP boundary it audits.
  A full conversation is replayable in SQL without re-running the model.
- **Human approval on the one tool that moves money.** `issue_refund` is gated with
  `require_approval`; a rejection is recorded as `refund_blocked`, so a refused refund is exactly
  as traceable as a paid one.
- **A deterministic guardrail, not a model call.** A Luhn check blocks a pasted card number
  *before* the model is invoked, so the number never reaches OpenAI and never reaches the session
  store — and the audit helper redacts it to the last four in the row that was already written.
  `run_in_parallel=False` is load-bearing here, not a tuning knob.
- **Semantic search that can't silently rot.** `text-embedding-3-small` into `VECTOR(1536)`,
  cosine distance, HNSW index, the same model on insert and on query. A mismatch does not raise;
  it returns confident nonsense, which is the whole reason it is pinned end to end.

---

## Architecture

```
   You (terminal)
        │
        ▼
   ┌────────────────────────────────────────────────────────────┐
   │  Worker — SandboxAgent on a Docker sandbox                 │
   │                                                            │
   │   input guardrail ── Luhn card check (before the model)     │
   │   Skills ─────────── summarize-ticket · find-similar-cases  │
   │                      escalate-with-context   (allowlisted)  │
   │   Session ────────── SQLAlchemySession → Neon (turn history)│
   │   RunHooks ───────── AuditHooks → audit_log                 │
   └───────────────┬────────────────────────────────────────────┘
                   │  MCP over streamable HTTP
                   │  (conversation_id carried in tool meta)
                   ▼
   ┌────────────────────────────────────────────────────────────┐
   │  customer-data MCP server — the ONLY route to business data │
   │    lookup_customer · find_similar_resolved_tickets          │
   │    issue_refund  ⟵ approval-gated, one transaction          │
   └───────────────┬────────────────────────────────────────────┘
                   │ asyncpg (+ pgvector)
                   ▼
   ┌────────────────────────────────────────────────────────────┐
   │  Neon Postgres + pgvector — the system of record            │
   │    conversations · documents · embeddings                   │
   │    audit_log · capability_invocations                       │
   │    customers · orders · tickets · refunds                   │
   └────────────────────────────────────────────────────────────┘

   Neon MCP (admin) ──▶ build plane only: provisioning, migrations. Never at runtime.
```

---

## Stack

Python 3.14 · OpenAI Agents SDK (`SandboxAgent`, Skills, Sessions, guardrails, RunHooks) ·
Model Context Protocol (FastMCP, streamable HTTP) · Neon Postgres · pgvector · asyncpg ·
SQLAlchemy async · Docker sandbox · uv.

---

## Running it

Two processes. The MCP server has to be up first — it is a service the Worker reaches by URL,
not a subprocess it spawns.

```bash
cp .env.example .env      # add OPENAI_API_KEY and NEON_DATABASE_URL
uv sync
uv run python seed/seed_and_embed.py          # 17 resolved tickets, embedded
uv run python customer-data-mcp/server.py     # terminal 1
uv run python main.py                         # terminal 2
```

Stop the MCP server and the Worker's tool calls fail with a connection error — which looks
nothing like a wrong answer, and that is the point.

---

## Decisions worth reading the code for

Each of these was found by running the thing, not by reasoning about it. They are commented at
the call site, with the measurement that produced them:

| Where | The decision |
| --- | --- |
| `guardrails.py` | Order UUIDs pass Luhn (a zero digit-sum is divisible by 10), so asking for a refund tripped the card guardrail. UUIDs are blanked before scanning, with same-length filler so offsets still line up. |
| `guardrails.py` | The distinct-digit floor is 2, not higher: the standard test card `4242 4242 4242 4242` has only two distinct digits, and a stricter rule silently stops detecting it. |
| `main.py` | `load_skill` returns `{'status': 'loaded'}` and nothing else, which reads like the skill is in effect. It only copied files — so the Worker is told to read the `SKILL.md` after loading it. |
| `main.py` | Told to look things up, the model reached for `lookup_customer`, found it needed a UUID nobody had, and asked for one instead of searching — in 1 run of 4. The instruction now orders search before identifiers. |
| `main.py` | `pool_pre_ping` + `pool_recycle`: a chat loop idles as long as a human takes to reply, and Neon's pooler closes the connection well inside that — killing the turn *after* `message_received` was written. |
| `customer-data-mcp/server.py` | `statement_cache_size=0`, because Neon's pooled endpoint is PgBouncer in transaction mode and a cached prepared statement can arrive on a connection that never prepared it. |
| `customer-data-mcp/server.py` | `init=register_vector` on the pool: pgvector registration is per-connection, and a pool hands out many. Miss it and the embedding column reads back as garbage rather than raising. |

---

## Honest limits

- `orders` is effectively empty — seeded tickets carry order numbers as free text, so
  `issue_refund` is proved against throwaway orders rather than the seeded corpus.
- The seed library is 17 resolved tickets: enough to show that semantic retrieval works and to
  measure it, not enough to say anything about recall at scale.
- Single-Worker, single-tenant, terminal front end. There is no queue, no web surface, and no
  multi-Worker routing.

---

## Related

[**Reading Room**](https://github.com/sorinLupaIonut/connector-native-app) — the same
architectural concerns from the other side of the boundary: a remote MCP server as a product
surface, with OAuth 2.1 identity and a fail-closed session gate.
