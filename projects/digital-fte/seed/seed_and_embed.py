"""Load the generated resolved tickets into Postgres and embed them.

Infrastructure, not a runtime path: this connects to Neon directly with asyncpg
rather than going through the `customer-data` MCP server. The MCP boundary is
for what the Worker does on its own; this script is something a human runs once,
by hand. There is no reason to put a boundary between yourself and your own
database when you are the one at the keyboard.

The row shape matters more than it looks. Decision 6's
`find_similar_resolved_tickets` finds a case by joining embeddings -> documents
(source='past_case') -> tickets, through `documents.metadata->>'ticket_id'`,
because `documents` has no ticket_id column. Lay the rows down any other way and
that search returns nothing later, with no error to explain it. The read-back at
the end runs that exact join, so the shape is proved the moment it is written.

    uv run python seed/seed_and_embed.py [--reset]
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import asyncpg
from dotenv import load_dotenv
from openai import AsyncOpenAI
from pgvector import Vector
from pgvector.asyncpg import register_vector

TICKETS_PATH = Path(__file__).parent / "resolved_tickets.json"

# Store and search with the same model, or results are plausible nonsense rather
# than an error. The column is VECTOR(1536) and this model produces 1536 dims;
# changing one without the other is the silent failure this whole file guards.
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536

# Stamped into the metadata of every row written here, so --reset can delete
# exactly what this script created and nothing else.
SEED_TAG = "resolved_tickets.json"

# The query the course uses to prove the search works. Run at the end.
PROBE_QUERY = "refund delayed two weeks"


def case_text(ticket: dict[str, Any]) -> str:
    """The text that gets embedded and stored as the document body.

    All three parts, not just the summary: the search has to match on the
    symptom (body) and on how it was filed (summary), but the resolution is what
    makes a hit useful rather than merely similar.
    """
    return (
        f"{ticket['summary']}\n\n"
        f"Customer: {ticket['body']}\n\n"
        f"Resolution: {ticket['resolution']}"
    )


def display_name(email: str) -> str:
    """Derive a human name from the local part: dana.whitfield -> Dana Whitfield.

    The generated tickets carry no name field. A derived one is honest enough for
    seed data and gives `summarize-ticket` something to put in Customer Context.
    """
    return " ".join(part.capitalize() for part in email.split("@")[0].split("."))


async def embed(client: AsyncOpenAI, texts: list[str]) -> list[Vector]:
    """Embed every text in one call, before any transaction is open.

    One HTTP round trip for the whole corpus, and deliberately not inside the
    write transaction: holding a Postgres transaction (and a Neon pooler slot)
    open across calls to another vendor is how a seed script ends up hanging.
    """
    response = await client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
    vectors = [item.embedding for item in response.data]
    for i, vector in enumerate(vectors):
        # Postgres would reject a wrong length here, but only because the column
        # is declared VECTOR(1536). Check anyway: this is the one place where the
        # model and the schema have to agree, and the failure is worth naming.
        if len(vector) != EMBEDDING_DIM:
            sys.exit(
                f"Embedding {i} has {len(vector)} dimensions, expected {EMBEDDING_DIM}. "
                f"{EMBEDDING_MODEL} no longer matches the VECTOR({EMBEDDING_DIM}) column."
            )
    return [Vector(v) for v in vectors]


async def reset(conn: asyncpg.Connection) -> None:
    """Delete only the rows this script wrote, identified by the seed tag.

    Never a blanket DELETE FROM: anything else in these tables belongs to someone
    else. Documents go first because embeddings cascade off them.
    """
    for table in ("documents", "tickets", "customers"):
        deleted = await conn.execute(
            f"DELETE FROM {table} WHERE metadata->>'seed_source' = $1", SEED_TAG
        )
        print(f"  reset: {table} {deleted}")
    await conn.execute("DELETE FROM audit_log WHERE action = 'corpus_seeded'")


async def seed(conn: asyncpg.Connection, tickets: list[dict], vectors: list[Vector]) -> dict:
    """Write every ticket and the run's audit row in one transaction.

    All of it commits or none of it does, audit row included -- so a half-seeded
    library can never exist, and the audit row can never claim a run that did not
    finish.
    """
    run_id = f"seed-{uuid.uuid4().hex[:12]}"
    tally = {"customers_created": 0, "customers_found": 0, "tickets": 0}

    async with conn.transaction():
        for ticket, vector in zip(tickets, vectors, strict=True):
            email = ticket["customer_email"]

            # DO UPDATE rather than DO NOTHING, and this is the line most easily
            # got wrong: `ON CONFLICT DO NOTHING ... RETURNING` returns no row on
            # conflict. Two emails in the seed data appear on two tickets each,
            # so the second insert conflicts, a DO NOTHING version gets None
            # back, and the ticket insert then dies on customer_id NOT NULL.
            # The no-op SET exists purely so a row always comes back.
            customer = await conn.fetchrow(
                """
                INSERT INTO customers (email, name, metadata)
                VALUES ($1, $2, $3::jsonb)
                ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
                RETURNING id, (xmax = 0) AS created
                """,
                email,
                display_name(email),
                json.dumps({"seed_source": SEED_TAG}),
            )
            tally["customers_created" if customer["created"] else "customers_found"] += 1

            ticket_id = await conn.fetchval(
                """
                INSERT INTO tickets (customer_id, subject, body, status, resolution,
                                     resolved_at, metadata)
                VALUES ($1, $2, $3, 'resolved', $4, now(), $5::jsonb)
                RETURNING id
                """,
                customer["id"],
                ticket["summary"],
                ticket["body"],
                ticket["resolution"],
                json.dumps({"seed_source": SEED_TAG, "category": ticket["category"]}),
            )

            text = case_text(ticket)
            document_id = await conn.fetchval(
                """
                INSERT INTO documents (source, title, body, metadata)
                VALUES ('past_case', $1, $2, $3::jsonb)
                RETURNING id
                """,
                ticket["summary"],
                text,
                # The ticket id lives here because documents has no ticket_id
                # column. This is the hop Decision 6's search depends on.
                json.dumps(
                    {
                        "ticket_id": str(ticket_id),
                        "category": ticket["category"],
                        "seed_source": SEED_TAG,
                    }
                ),
            )

            # chunk_index 0 and no chunking: these cases run to about 80 words,
            # far under any sensible chunk size. One chunk is the honest shape
            # rather than a pipeline pretending to split.
            await conn.execute(
                """
                INSERT INTO embeddings (document_id, chunk_text, chunk_index, embedding, model)
                VALUES ($1, $2, 0, $3, $4)
                """,
                document_id,
                text,
                vector,
                EMBEDDING_MODEL,
            )
            tally["tickets"] += 1

        # One row for the whole run, inside the same transaction as the writes it
        # describes. conversation_id stays NULL: a seed run is not a conversation,
        # and the column is a foreign key to one.
        await conn.execute(
            """
            INSERT INTO audit_log (conversation_id, actor, action, target, payload, result)
            VALUES (NULL, 'seed-script', 'corpus_seeded', $1, $2::jsonb, $3::jsonb)
            """,
            run_id,
            json.dumps(
                {
                    "source_file": TICKETS_PATH.name,
                    "embedding_model": EMBEDDING_MODEL,
                    "embedding_dim": EMBEDDING_DIM,
                    "tickets_in_file": len(tickets),
                }
            ),
            json.dumps(tally),
        )

    return {"run_id": run_id, **tally}


async def read_back(conn: asyncpg.Connection, client: AsyncOpenAI) -> None:
    """Prove the seed from the database side, not from what the writer believed."""
    documents = await conn.fetchval("SELECT count(*) FROM documents WHERE source = 'past_case'")
    embeddings = await conn.fetchval("SELECT count(*) FROM embeddings")
    models = await conn.fetch("SELECT model, count(*) AS n FROM embeddings GROUP BY model")

    print("\n--- read-back " + "-" * 52)
    print(f"  documents source='past_case' : {documents}")
    print(f"  embeddings                   : {embeddings}")
    for row in models:
        print(f"  embedding model              : {row['model']} ({row['n']})")
    if len(models) > 1:
        print("  !! more than one model present -- the corpus is not searchable as one set")

    audit = await conn.fetchrow(
        "SELECT target, action, payload, result, created_at FROM audit_log"
        " WHERE action = 'corpus_seeded' ORDER BY created_at DESC LIMIT 1"
    )
    print(f"\n  audit_log: {audit['action']} {audit['target']} at {audit['created_at']:%H:%M:%S}")
    print(f"    payload: {audit['payload']}")
    print(f"    result : {audit['result']}")

    # The same model that wrote the corpus, because a query embedded with a
    # different one lands somewhere unrelated in the same space without failing.
    probe = (await client.embeddings.create(model=EMBEDDING_MODEL, input=[PROBE_QUERY])).data[0]

    # Deliberately the full Decision 6 join rather than a bare scan of embeddings:
    # this is the query shape find_similar_resolved_tickets will use, so proving
    # it now means Decision 8 cannot fail on a join that was never tested.
    rows = await conn.fetch(
        """
        SELECT t.subject, t.metadata->>'category' AS category,
               e.embedding <=> $1 AS distance
        FROM embeddings e
        JOIN documents d ON d.id = e.document_id
        JOIN tickets   t ON t.id = (d.metadata->>'ticket_id')::uuid
        WHERE d.source = 'past_case'
        ORDER BY distance
        LIMIT 5
        """,
        Vector(probe.embedding),
    )
    print(f'\n  similarity search: "{PROBE_QUERY}"')
    for i, row in enumerate(rows, 1):
        print(f"    {i}. {row['distance']:.3f}  [{row['category']}] {row['subject']}")


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete rows a previous run of this script wrote, then seed again",
    )
    args = parser.parse_args()

    load_dotenv()
    for key in ("OPENAI_API_KEY", "NEON_DATABASE_URL"):
        if not os.getenv(key):
            sys.exit(f"{key} is not set. Put it in .env and try again.")

    payload = json.loads(TICKETS_PATH.read_text(encoding="utf-8"))
    tickets = payload["tickets"]
    print(f"{len(tickets)} tickets from {TICKETS_PATH.name}")

    client = AsyncOpenAI()
    print(f"embedding with {EMBEDDING_MODEL} ...")
    vectors = await embed(client, [case_text(t) for t in tickets])

    # asyncpg takes Neon's connection string as-is, sslmode and channel_binding
    # included -- unlike SQLAlchemy's asyncpg dialect, which needs both rewritten
    # (see to_asyncpg_url in main.py). statement_cache_size=0 is for the -pooler
    # host: PgBouncer in transaction mode hands one session to different clients,
    # so a cached prepared statement can arrive where it was never prepared.
    conn = await asyncpg.connect(os.environ["NEON_DATABASE_URL"], statement_cache_size=0)
    # Required on any connection that touches the embedding column. Without it
    # the vector goes in as something else and comes back as garbage, silently.
    await register_vector(conn)
    try:
        existing = await conn.fetchval(
            "SELECT count(*) FROM documents WHERE source = 'past_case'"
        )
        if existing and not args.reset:
            sys.exit(
                f"\n{existing} past_case documents already exist. Seeding again would "
                f"double them, and the Decision 5 check ('counts should match the number "
                f"of tickets') would then fail for a reason that looks like a counting "
                f"bug.\n\nRe-run with --reset to replace what a previous run wrote."
            )
        if args.reset:
            print("\nresetting previously seeded rows")
            await reset(conn)

        result = await seed(conn, tickets, vectors)
        print(f"\ncommitted as {result['run_id']}")
        print(
            f"  customers: {result['customers_created']} created, "
            f"{result['customers_found']} already present"
        )
        print(f"  tickets, documents, embeddings: {result['tickets']} each")

        await read_back(conn, client)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
