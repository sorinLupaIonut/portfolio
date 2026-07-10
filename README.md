# Reading Room — a connector-native MCP gateway

A single remote **MCP server** that a Claude user adds with one connector URL and one **Authorize**
click. It turns a plain chat into a small reading app with memory: a seed library of articles the
user can fetch, save to a reading list, mark as read, and annotate — and the state persists across
separate chats, tied to *who is signed in*, never to who the model claims to be.

The host chat app (claude.ai, the Claude desktop app) brings the model and the agent loop. **This
server brings the three things a model can't safely bring itself: tools, state, and identity.**
There is deliberately no agent loop in this repository.

> Built as the capstone of the *Connector-Native Apps* course. The security-critical token check
> (`auth.py`) and session primitive (`session.py`) were provided as a fixed standard to build
> against; **everything else here — the gateway, the OAuth resource-server wiring, the persistence
> layer, the session contract, the fail-closed design, and the live deployment — I built and
> verified end to end.** The interesting work is precisely the integration: wiring given security
> primitives into a correct, hard-to-misuse whole.

---

## What it demonstrates

- **OAuth 2.1 resource server done to spec** — RFC 9728 protected-resource discovery, RFC 8707
  audience-bound tokens, a real `401 → sign-in` flow, PKCE/S256 assumed at the client.
- **Identity you can't spoof from a prompt** — every per-user read and write is keyed to the `sub`
  claim of a *verified* token, sealed in server-side. No tool accepts a `user_id`; the model can
  never name whose data it touches.
- **A structural session gate** — a bearer OAuth token is exchanged for a short-lived signed
  *session* token; every real tool requires it as its first argument, so the model is *forced* to
  check in before it can do anything.
- **Fail-closed under failure** — if the store is unreachable, the app refuses honestly ("can't
  reach your shelf, this session can't continue") instead of inventing state. A store that is *down*
  never looks like a user who simply has *nothing saved*.
- **Cross-chat memory** — a two-table Postgres store on Neon; state saved in one chat is there in
  the next.
- **Modern Python** — 3.14+, built-in generics, `X | None`, PEP 695 `type` aliases, mypy-clean.

---

## Architecture

```
                         ┌───────────────────────────────────────────────┐
   Claude host           │              Reading Room gateway              │
   (model + loop)        │            one FastMCP app, one URL            │
        │                │                                               │
        │  MCP over HTTP  │   ┌─────────────────────────────────────┐     │
        └────────────────┼──▶│ RemoteAuthProvider (OAuth 2.1 RS)    │     │
     Bearer OAuth token   │   │  • /.well-known/oauth-protected-... │     │
                         │   │  • delegates to auth.py's 4 checks: │     │
                         │   │    signature · iss · aud · expiry   │     │
                         │   └───────────────┬─────────────────────┘     │
                         │        verified `sub`                          │
                         │                   ▼                            │
                         │   begin_session ──▶ mints signed session token │
                         │                   │  + rules + persona + state │
                         │                   ▼                            │
                         │   session gate on every domain_*/user_* tool   │
                         │                   │                            │
                         │        ┌──────────┴──────────┐                 │
                         │   seed library         Neon Postgres           │
                         │  (articles.json)   users · user_state(jsonb)   │
                         └───────────────────────────────────────────────┘
```

### Two tokens, two jobs

1. **OAuth token** (external authorization server, RS256) — verified by `auth.py`'s four checks
   (JWKS signature, issuer, audience = this server, expiry), then `sub` is extracted. The gateway
   is a resource server *only*; it never issues tokens.
2. **Session token** (internal, HS256, 30-min TTL) — minted by `begin_session` from the verified
   `sub`, and required by every `domain_*`/`user_*` tool. This is the structural gate: nothing
   auto-invokes `begin_session`, so a tool called without a session fails closed.

---

## The tools (one gateway, three namespaces)

MCP tool names allow only letters, digits, `_`, `-` — never a dot — so the underscore prefix *is*
the namespace. One server, three groups, seven tools:

| Tool | Group | Session-gated | Purpose |
|---|---|:---:|---|
| `config_health` | `config_*` | — | Liveness probe (ungated on purpose, for fail-closed diagnostics) |
| `begin_session` | entry | issues the token | Check the reader in; return rules + persona + shelf + session token |
| `domain_get_item` | `domain_*` | ✅ | Fetch one library article by id |
| `user_get_state` | `user_*` | ✅ | Read the reader's shelf (reading list, finished, notes) |
| `user_save_article` | `user_*` | ✅ | Add an article to the reading list |
| `user_mark_read` | `user_*` | ✅ | Mark an article finished |
| `user_set_note` | `user_*` | ✅ | Attach/replace a note on an article |

Only MCP **tools** are used — no resources, no prompts. The app's rules and persona travel as
plain data inside `begin_session`'s return value, phrased as cooperation ("here is how to behave
for this reader"), never as an override, so the host's injection defenses don't discount them.

---

## Data model

```sql
users(
  sub         text primary key,          -- the verified OAuth subject; the ONLY identity
  created_at  timestamptz not null default now()
)
user_state(
  sub         text primary key references users(sub),
  state       jsonb  not null default '{}',   -- {"reading_list":[…], "finished":[…], "notes":{…}}
  updated_at  timestamptz not null default now()
)
```

One JSON document per user; writers read-modify-write it. Identity is never a query parameter the
model controls — it is sealed in from the verified token upstream, so the database physically
cannot be asked for someone else's row.

---

## Tech stack

- **[FastMCP](https://gofastmcp.com)** — the MCP server, streamable HTTP transport
- **OAuth 2.1** resource-server pattern (RFC 9728 discovery, RFC 8707 audience binding)
- **Neon** serverless Postgres, via **psycopg 3**
- **python-jose** (JWT verification), **httpx** (JWKS fetch)
- **Python 3.14+**, **uv** for env & deps
- **cloudflared** quick-tunnel for the live demo

---

## Run it

```bash
uv sync --extra dev
uv run pytest -q          # 5 green: imports, valid token, wrong-audience rejected, isolation, no-session refused
```

Bring the gateway up (binds `127.0.0.1:8000`, serves `/mcp`):

```bash
uv run python -m connector_app.server
```

**Live demo (auth off, public tunnel).** Set `AUTH_DISABLED=1`, start the gateway, and expose it
over a free Cloudflare quick-tunnel; paste the resulting HTTPS URL into claude.ai → Settings →
Connectors → *Add custom connector*. This demo path is single-user and open by design — it exists
to see the whole app working inside a real chat host in one afternoon, with no host account and no
sign-in service. Real multi-user OAuth against a live authorization server is the natural next step.

Configuration lives in `.env` (never committed); copy `.env.example` and fill it in.

---

## Repository layout

```
src/connector_app/
  server.py         the one FastMCP gateway: OAuth wiring, the session gate, the 7 tools
  db.py             two-table Neon/psycopg persistence, keyed only by verified `sub`
  config_store.py   the librarian's cooperative rules, persona, and fail-closed instruction
  auth.py           the OAuth token check — the four checks (provided; wired, not rewritten)
  session.py        the session-token primitive (provided; wired, not rewritten)
seed/articles.json  the small read-only library
tests/              five offline smoke tests over the security core
mock_auth/          a local dev sign-in service for exercising the real auth path (never deployed)
```

## The four invariants it holds to

1. **One gateway.** One MCP server, one public URL — a user adds a single connector.
2. **Tools only.** No MCP resources or prompts for app logic.
3. **Prove, don't trust.** Identity comes only from the verified token's `sub`, never from a tool
   argument the model controls.
4. **Fail closed.** If the session can't start or a tool errors, the app refuses — it never
   improvises user state.
