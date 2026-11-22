# Customer-support Worker: the plan

Status: **proposed, not approved.** No code is written from this yet. This is the Decision 2 artifact — the cheapest place in the whole build to be wrong.

Sources: this repo's `AGENTS.md`, the crash-course Part 1 (Skills), Part 2 Concepts 7–10 (schema, pgvector, audit), Part 3 Concepts 11–15 (MCP), and Part 4 Decisions 1–9. SDK class names below were confirmed against the live `openai-agents` docs through Context7 on 2026-07-22; anything still moving is flagged **[confirm at build time]**.

---

## 0. Where we actually are

The prompt says the foundation "stays." Three quarters of it does not exist yet, so naming the gap now avoids a nasty surprise at Decision 3.

| Foundation piece | State today | Arrives in |
| --- | --- | --- |
| OpenAI Agents SDK, `uv` project | present (`main.py`, `pyproject.toml`, `openai-agents[docker]` 0.18.3) | — |
| Sandbox runtime | present, but **`DockerSandboxClient`**, not `UnixLocalSandboxClient` — this is a Windows box and the Unix client raises `ImportError` on import | — |
| `SandboxAgent`, gpt-5-class model | present (`gpt-5-mini`, overridable via `AGENT_MODEL`) | — |
| **Session (memory across turns)** | **absent** — every turn starts cold | Decision 3 |
| **Streaming** | **absent** — one blocking `Runner.run` per turn | Decision 7 (audit needs the loop anyway) |
| **Guardrails** | **absent** | Decision 7 (the `guardrail_tripped` row needs something to trip) |
| Skills capability | absent | Decision 4 |
| Neon database | not provisioned | Decision 3 |

Nothing here blocks the plan. It does mean Decision 3 is bigger than "add a Session": it also introduces the first `conversations` row-writing code path.

---

## 1. The three Skills

All three live in `.claude/skills/<name>/` — one location, read by Claude Code, by OpenCode (as fallback), and by the Worker's own `Skills` capability pointed at the **relative** path `.claude/skills`. Never duplicated into `.opencode/skills/`.

Every one is scaffolded with `skill-creator`, never from a blank file, and every `description` gets the delete-the-keyword test: strip the obvious verb and see whether it still says *when* to fire.

### 1.1 `summarize-ticket`

**Shape: instruction-driven.** No `scripts/`. The output is prose in a fixed shape; there is no computation to run and no data to fetch. A script here would buy nothing and add a failure mode.

**Proposed description** (the frontmatter line, which is the entire trigger surface):

> Turns one support ticket or thread into a five-section handoff note — Customer Context, Issue, Resolution Steps Taken, Current Status, Recommended Next Action — so another person can pick the case up cold. Use when someone needs to hand a case off, brief a lead, take over someone else's thread, or establish where a case stands before escalating: "write a handoff note for #4471", "TL;DR this thread", "what's the story on 1042", "where does this stand", "bring me up to speed on Sam's case". Do NOT use for drafting a reply to the customer, triaging a batch of tickets, or reporting on ticket volume or trends.

It names the output, the situations, the phrasings that never say "summarize", and a do-NOT line for the three look-alikes. Delete "handoff" and "summar\*" from it and it still says when to fire — that is the test it has to pass.

**Reference files:**

- `references/examples.md` — two worked examples, because an example steers roughly 5× harder than a description: (1) a clean single-thread refund ticket, (2) a messy one with three agents, a duplicate ticket, and a customer who changed their story. The second is the one that teaches the model what to do when the facts conflict.
- `references/edge-cases.md` — the three that actually break it: a ticket with no resolution attempts yet (Resolution Steps Taken must read "none yet", not be invented), a ticket spanning two order numbers, and a thread where the last message is from the customer and unanswered.

**Fire/don't-fire eval set** (fed to `skill-creator` so it can optimise the description against held-out cases, per Decision 4 Step 3):

- Fire: "write a handoff note for #4471" · "TL;DR this thread" · "where does this stand before I escalate" · "bring my lead up to speed on Sam's case"
- Don't fire: "draft a reply to Sam" · "which of these 30 tickets are urgent" · "how many tickets did we close last week" · "what's our refund policy"

### 1.2 `find-similar-cases`

**Shape: instruction-driven, MCP-backed.** Explicitly **no `scripts/`** — and this is a rule, not a preference. A `scripts/search.py` that opened its own Postgres connection would be the running Worker touching business data outside the `customer-data` MCP server, which is exactly what the first rule in `AGENTS.md` forbids. The skill's data path is the `find_similar_resolved_tickets` MCP tool and nothing else.

**Proposed description:**

> Searches the resolved-tickets library for tickets semantically similar to a customer's described issue, returning the top 3–5 with their resolutions, ranked by how closely each matches. Use when the user describes a problem, complaint, or symptom and you need to check whether the team has handled something similar before. Calls the `find_similar_resolved_tickets` MCP tool. Always run this BEFORE drafting a response, so the response can reference proven prior resolutions rather than inventing a new approach.

The "always run this BEFORE drafting" clause is load-bearing: without it the model drafts from priors and never opens the library.

**Body (imperative):** extract the issue description from context → call `find_similar_resolved_tickets` with `limit=5` → present the top three in a markdown table with their distances → flag anything above ~0.3 as "no strong prior precedent found" rather than presenting a weak match as precedent.

**Reference files:**

- `references/distance-bands.md` — what a cosine distance means in this corpus (lower = more similar; the ~0.3 cut-off, and why it is a heuristic to re-measure once the corpus grows past the seed set). Without this the model treats 0.42 as a match.

### 1.3 `escalate-with-context`

**Shape: instruction-driven, composed.** It invokes `summarize-ticket` first for the structured context, then writes the escalation note. Composition rather than one big skill, per Concept 5: each half is separately testable, and the summariser is reused on its own.

**Proposed description:**

> Packages a customer conversation for handoff to a tier-2 support agent. Produces a structured escalation note with customer profile, issue summary, what was already tried, why escalation is recommended, and the suggested specialist team. Use when (a) the customer is on the Pro or Enterprise tier AND the issue is unresolved after one round of investigation, (b) the customer's sentiment is clearly negative, (c) the issue involves billing over $500 or a refund decision, or (d) the user explicitly asks for a human.

The four conditions are the whole design. A Worker with vague escalation logic escalates everything, which is the same as escalating nothing.

**Reference files:**

- `references/escalation-triggers.md` — each of the four conditions with one worked yes and one worked no. The near-misses matter more than the hits: a Standard-tier customer who is annoyed but resolvable is a *no*.
- `references/routing.md` — issue class → specialist team → suggested SLA. This is the part that will change most often as the business changes, which is precisely why it is a reference file and not body text.

**Note on tier:** condition (a) needs the customer's tier, which comes from `lookup_customer`. Until something upstream resolves a customer id (see §3.4), this skill fires on (b), (c), (d) and degrades gracefully on (a). The body must say so, or the model will invent a tier.

---

## 2. The schema

One Neon Postgres database, `pgvector` enabled, migrated **on a branch** via `prepare_database_migration` → human approval → `complete_database_migration`. Never untested DDL against `main`.

### 2.1 Core spine (five tables, verbatim from Concept 7)

Built exactly as the course prints them, because Decision 8's replay query and Decision 6's join both depend on the exact column names:

- **`conversations`** — `session_id TEXT PK` (the *same* id passed to `SQLAlchemySession`), `user_id`, `started_at`, `ended_at`, `metadata JSONB`, `summary`. Business metadata and the closing summary. **Not** a message store.
- **`documents`** — `id UUID PK`, `source` ('policy_library' | 'kb_article' | 'past_case'), `title`, `body`, `metadata JSONB`, timestamps.
- **`embeddings`** — `id UUID PK`, `document_id` XOR `conversation_id` (enforced by `CHECK`), `chunk_text`, `chunk_index`, `embedding VECTOR(1536)`, `model`, `created_at`. Plus `idx_embeddings_hnsw ... USING hnsw (embedding vector_cosine_ops)`.
- **`audit_log`** — `id BIGSERIAL PK`, `conversation_id` FK → `conversations(session_id)`, `actor`, `action` (closed `CHECK` set, see §4.2), `target`, `payload JSONB`, `result JSONB`, `created_at`.
- **`capability_invocations`** — `id UUID PK`, `conversation_id` FK, `capability` ('skill:…' / 'tool:…' / 'mcp:…'), `arguments`, `result`, `status` `CHECK (ok|error|blocked|timeout)`, `latency_ms`, `cost_cents`, `created_at`.

**Keep `audit_log.target` and both `CHECK` sets exactly as printed.** They look optional; they are what Decision 8's replay query selects on.

### 2.2 The SDK's own turn store — not ours to design

`SQLAlchemySession.from_url(session_id, url=<postgresql+asyncpg://…>, create_tables=True)` creates and owns `agent_sessions` and `agent_messages` on this same database. **We do not hand-roll a `messages` table.** `conversations` is the cover sheet, the Session tables are the transcript, joined on `session_id`.

Default flavour, chosen deliberately: the Session owns turns. We build a custom turns table only if we later need per-turn rows we can type, query, or embed directly — and if we ever do, that table *replaces* the Session store rather than duplicating it.

### 2.3 Domain tables (four)

```
customers    id UUID PK
             email          TEXT UNIQUE NOT NULL      -- the seeder's find-or-create key
             name           TEXT
             metadata       JSONB NOT NULL DEFAULT '{}'   -- tier lives at metadata->>'tier'
             created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()

orders       id UUID PK
             customer_id    UUID NOT NULL REFERENCES customers(id)
             order_number   TEXT UNIQUE NOT NULL      -- the human-facing "#4429"
             total_cents    INT  NOT NULL CHECK (total_cents >= 0)
             status         TEXT NOT NULL CHECK (status IN
                              ('placed','shipped','delivered','refunded','cancelled'))
             placed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
             metadata       JSONB NOT NULL DEFAULT '{}'

tickets      id UUID PK
             customer_id    UUID NOT NULL REFERENCES customers(id)
             order_id       UUID     NULL REFERENCES orders(id)
             subject        TEXT NOT NULL
             body           TEXT NOT NULL
             status         TEXT NOT NULL CHECK (status IN
                              ('open','pending','resolved','escalated'))
             resolution     TEXT                       -- NOT NULL in spirit when status='resolved'
             created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
             resolved_at    TIMESTAMPTZ
             metadata       JSONB NOT NULL DEFAULT '{}'

refunds      id UUID PK
             order_id       UUID NOT NULL REFERENCES orders(id)
             ticket_id      UUID     NULL REFERENCES tickets(id)
             conversation_id TEXT    NULL REFERENCES conversations(session_id)
             amount_cents   INT  NOT NULL CHECK (amount_cents > 0)   -- integer cents, never a float
             reason         TEXT NOT NULL
             issued_by      TEXT NOT NULL              -- 'worker:customer-support' + approver
             issued_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
```

Three deliberate choices, each of which the build depends on:

1. **No `tier` column on `customers`.** Tier lives at `customers.metadata->>'tier'`, `COALESCE`d to `'standard'`. `lookup_customer` reads it from there. (Tier is exactly the kind of field that *should* graduate to a real column once we query it often — noting it here so that promotion is a decision, not a drift.)
2. **No `ticket_id` column on `documents`.** A seeded past-case document links to its ticket via `documents.metadata->>'ticket_id'`. This is what `find_similar_resolved_tickets` joins on; get it wrong at seed time and the search silently returns zero rows in Decision 8 with no error anywhere.
3. **`tickets.customer_id` is `NOT NULL`,** so the seeder must find-or-create the customer by email *before* inserting the ticket.

Nine tables in `public` when the migration lands (5 core + 4 domain), plus the two the Session creates on first use.

### 2.4 Deferred: `run_states`

One row per paused approval — serialized `RunState`, plus `awaiting|approved|rejected`. Needed only when sign-off arrives later or in another process. Not in the first migration; Decision 9 approves synchronously in the terminal. It does not belong in `audit_log` (append-only) or as a column on `conversations` (one conversation can pause more than once).

### 2.5 Embedding contract — one line, held end to end

Model `text-embedding-3-small` → dimension `VECTOR(1536)` → cosine distance `<=>` → HNSW `vector_cosine_ops`. **Same model on insert and on query.** A mismatch does not raise; it returns ranked nonsense. Every connection that touches the `embedding` column calls `register_vector`, or writes corrupt silently.

---

## 3. The `customer-data` MCP server

Built with `mcp-builder`, on **streamable HTTP, stateless flavour** — each call an independent request, so it is a real addressable service and horizontally scalable. (stdio would work for a single local Worker; stateless HTTP is what we'd actually ship.) It runs as its own process, with its own `asyncpg` pool.

**Exactly three tools. No `run_sql`. No DDL tool of any kind** — schema changes never reach production through the agent.

### 3.1 `lookup_customer`

```
lookup_customer(customer_id: str) -> {
    id: str, email: str, tier: str, open_ticket_count: int
}
```

Input is a **customer UUID**, not free text. Errors cleanly on an unknown id; never falls back to a fuzzy search.

### 3.2 `find_similar_resolved_tickets`

```
find_similar_resolved_tickets(description: str, limit: int = 5) -> [
    { ticket_id: str, summary: str, resolution: str, distance: float }
]   # ascending distance; limit clamped to 1..10
```

`description` is free text **by design** — it is embedded, not interpolated into SQL. That is the difference between a semantic-search parameter and an injection hole. The tool embeds with `text-embedding-3-small` (the same model the seed used), registers pgvector on the connection, and joins `embeddings → documents (source='past_case') → tickets` via `documents.metadata->>'ticket_id'`.

### 3.3 `issue_refund`

```
issue_refund(order_id: str, amount_cents: int, reason: str) -> {
    refund_id: str, order_status: str, audit_id: int
}
```

`amount_cents` is an **integer**, never a float. One `transaction()` block does all three writes — insert the `refunds` row, flip `orders.status` to `'refunded'`, write the `refund_issued` `audit_log` row — or none of them. Human-approval-gated in Decision 9 (§5).

### 3.4 The known gap, named rather than papered over

The Worker sees "order #4429" and "I'm Sam" in the message. `issue_refund` wants an order **UUID** and `lookup_customer` wants a customer **UUID**. Nothing in the current tool list turns a human-facing handle into an id.

That is deliberate for now — the verification scenario in Decision 8 is read-only and does not need it, and three tools is the scope the course pins. **Recommendation: ship three, and record the resolver as the first thing to add after Decision 9**, either as `lookup_order_by_number(order_number)` / `lookup_customer_by_email(email)` on this same server, or by having auth/the orchestrator inject the customer id into context. Until then, Decision 9's refund run gets its order UUID from the human, not from the model.

### 3.5 Explicitly rejected tool shapes

Each of these is `run_sql` in disguise, and each is the failure Decision 2's check exists to catch:

- `query(sql: str)` — obviously.
- `lookup_customer(query: str)` — free text we would build SQL from.
- `search(table: str, filters: dict)` — a generic query builder with a friendly name.
- `execute(operation: str, params: dict)` — the same thing wearing a hat.

### 3.6 Registration on the Worker

Confirmed live via Context7:

```python
MCPServerStreamableHttp(
    name="customer-data",
    params={"url": "http://localhost:8000/mcp", "timeout": 30},
    cache_tools_list=False,                      # True once the tool list is stable
    require_approval={"always": {"tool_names": ["issue_refund"]}},   # Decision 9
)
```

The gate lives on the **registration**, not inside the tool. The two read tools stay ungated: reads run free, writes are gated, structural changes are not exposed at all.

---

## 4. The audit plan

Two rules above everything: **every state-changing action commits with its audit row in one transaction**, and **audit runs on its own direct `asyncpg` pool**, never through the MCP boundary it audits. An audit subsystem that the audited system can starve is not an audit subsystem.

### 4.1 What writes a row, and from where

| Action | `audit_log.action` | Written by | Trigger point |
| --- | --- | --- | --- |
| Customer message arrives | `message_received` | agent-side pool | before anything else; **upserts the `conversations` row first**, or the FK violates and rolls back the very transaction it was recording |
| A skill loads | `skill_activated` | agent-side pool | `on_tool_start` where `tool.name == "load_skill"` — there is no skill hook; in lazy Skills mode a skill *is* a tool call |
| An MCP tool returns | `capability_invoked` | agent-side pool | `on_tool_end`, `target='mcp:<tool_name>'`; also writes a `capability_invocations` row with latency and status |
| Refund succeeds | `refund_issued` | **MCP server's** pool, inside `issue_refund`'s transaction | the one business-write row the server writes itself |
| Refund approval rejected | `refund_blocked` | agent-side pool | on rejection at the approval gate |
| A guardrail trips | `guardrail_tripped` | agent-side pool | `except InputGuardrailTripwireTriggered` around `Runner.run` — guardrails raise, they do not hook |
| Reply sent | `message_sent` | agent-side pool | after the final output |
| Seed script run | `corpus_seeded` | seed script's direct connection | once per seed run — this row is how we debug a seeder that swallowed an error |

### 4.2 The vocabulary is closed, so it is decided now

The `CHECK` constraint on `audit_log.action` pins exactly these eight:

```
message_received · message_sent · skill_activated · capability_invoked
refund_issued · refund_blocked · guardrail_tripped · corpus_seeded
```

Adding a ninth verb later is an `ALTER TABLE … DROP/ADD CONSTRAINT` migration, and the failure surfaces as a constraint violation that points nowhere near "you forgot to plan your vocabulary." Decide the full set once; do not let synonyms drift in.

### 4.3 What does **not** write an audit row

- **Conversation turns.** The SDK Session already stores every message. Duplicating them doubles storage and makes the log unreadable.
- **The model's reasoning.** Out, always.
- **Raw sensitive data.** Hash or summarise; the full thing goes somewhere access-controlled, not into a table humans read casually.
- **Reads that never leave the process** — loading a `references/` file inside a skill, for instance. Not an action against the record.
- **Retries inside a tool.** One row per logical action, not per network attempt. (A *failed* action still gets its row, with `status='error'`.)
- **Trace/telemetry data.** The OpenAI dashboard trace is the flight recorder for debugging; the audit log is the receipt. Run both, never conflate them.

### 4.4 The done test

Given a conversation and a time, reconstruct what the Worker did and why, **without re-running the model.** If that fails, we have logs, not an audit trail. Decision 8's replay query is the concrete form:

```sql
SELECT created_at, action, target, payload, result
FROM audit_log
WHERE conversation_id = $1
ORDER BY created_at;
```

A clean read-only run reads: `message_received` → (`skill_activated`, only if a skill loaded) → `capability_invoked` → `message_sent`.

---

## 5. Build order and the gotchas each step hits

| # | Step | The thing that bites |
| --- | --- | --- |
| 3 | Provision Neon, migrate on a branch, add `SQLAlchemySession` | `uv add "openai-agents[sqlalchemy]" asyncpg pgvector greenlet` — the extra does **not** pull `greenlet`; the async engine needs `postgresql+asyncpg://`, not bare `postgresql://` |
| 4 | Three Skills + `Skills` capability | path must be **relative** (`.claude/skills`) and the Worker run from the project root; **add** Skills to the default capabilities, never replace them |
| 5 | Seed + embed (direct asyncpg, infrastructure not runtime) | `register_vector` on the connection; one embedding model only; lay rows down in the shape §3.2's join reads |
| 6 | `customer-data` MCP server | Neon's pooled endpoint breaks asyncpg prepared statements → `statement_cache_size=0` on **every** pool, this one and the audit one |
| 7 | Audit wiring | `on_tool_end`'s `result` is typed `str` but hands you a raw object — coerce with `str()` **and** wrap the hook body in `try/except`, because an exception inside a hook kills the whole turn; detect `"Error executing tool"` by substring, not `startswith`, or a failed refund logs as a success |
| 7 | Input guardrail | with a Session, the guardrail sees the **whole transcript** — screen only the latest `role: user` item, or one flagged word poisons every later turn |
| 8 | End-to-end verification + replay | a missing `skill_activated` is **not** a bug: the model may call the MCP tool directly without loading a skill |
| 9 | Approval gate on `issue_refund` | resuming an approved run is a **loop**, not one call — a run can hold several pending approvals; blocking `input()` freezes the asyncio loop and Neon's pooler then closes the idle connection, so prompt via `asyncio.to_thread` |

---

## 6. Two things worth pushing back on before this is approved

1. **`escalate-with-context` condition (a) is partly dead on arrival.** It needs a tier, the tier needs a customer id, and nothing resolves one from a chat message (§3.4). Either we accept a skill that fires on three of its four conditions for now, or we add the resolver tool before Decision 4 rather than after Decision 9. My recommendation is to accept it and add the resolver later — but the skill body has to say so explicitly, or the model will fill the gap by guessing a tier.

2. **`capability_invocations` is optional and I have included it anyway.** It costs a table and a write per tool call, and it buys per-skill metrics in SQL (fire rate, error rate, what precedes an escalation). Decision 3's check counts nine tables, which assumes it is there. If we drop it, the `capability_invoked` audit rows still carry the trace and only the metrics go — say so now, because removing it later means re-running the migration.
