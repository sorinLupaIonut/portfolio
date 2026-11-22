"""Generate the resolved-ticket seed library with the Worker's own SDK.

The library `find-similar-cases` searches has to exist before that skill means
anything. It is generated rather than hand-written: a Worker's own seed data is
something the Worker can produce.

This runs on a plain `Agent`, not the `SandboxAgent` the Worker itself uses.
Turning a prompt into structured JSON needs no filesystem, no shell and no
skills, so booting a Docker sandbox for it would buy nothing -- same SDK,
smaller primitive. It is also why this script talks to nothing else: it writes a
JSON file and stops. Seeding the database is the next script's job.
"""

import asyncio
import json
import os
import re
import sys
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

from agents import Agent, Runner
from agents.run import RunConfig
from dotenv import load_dotenv
from pydantic import BaseModel, Field

OUT_PATH = Path(__file__).parent / "resolved_tickets.json"

DEFAULT_MODEL = "gpt-5-mini"

# Four tickets per category, sixteen in total. Enough for a similarity search to
# have something to rank, small enough to embed for a fraction of a cent.
PER_CATEGORY = 4

# Two emails handed to every category run, so a few customers show up on more
# than one ticket. That is what makes the seeder's find-or-create step a real
# test: with every email unique, "find" never runs and a broken lookup passes.
RECURRING_EMAILS = ("dana.whitfield@example.com", "tomas.okafor@example.net")

# The category alone does not stop the model from writing the same ticket four
# times in four wordings. The "vary" line does: it names the axes to spread
# along, which is the difference between four cases and one case rephrased.
CATEGORIES = {
    "refund": (
        "Refund requests on a completed order. Vary the reason (defective on "
        "arrival, wrong size, order cancelled too late, price dropped after "
        "purchase), the amount, and how it ended: full refund, partial refund, "
        "store credit, and one where the refund was refused as out of policy."
    ),
    "login": (
        "Account access problems. Vary the cause (forgotten password, lost "
        "2FA device, account locked after failed attempts, email address on "
        "the account no longer reachable, SSO mapping broken after a work "
        "email change) and how identity was finally confirmed."
    ),
    "duplicate_charge": (
        "Being billed more than once. Vary the shape: the same order charged "
        "twice, a pre-authorisation hold sitting next to the real charge, a "
        "cancelled subscription that billed again, and a retry after a failed "
        "payment that went through twice. Say who reversed it and how long the "
        "money took to land."
    ),
    "shipping": (
        "Delivery going wrong. Vary the failure (tracking stuck for days, "
        "delivered to the wrong address, package arrived empty or damaged, "
        "held at customs, split shipment where only half arrived) and the fix: "
        "reship, refund, courier investigation, or it turned up on its own."
    ),
}

INSTRUCTIONS = """\
You write realistic past support tickets for a consumer e-commerce company, \
for use as seed data. Every ticket you write is already resolved.

Rules:
- Invent everything. Use only example.com, example.net and example.org email \
addresses, and names that are plainly fictional.
- `summary` is one line, under 90 characters, in the words a support agent \
would file it under. No ticket numbers.
- `body` is two or three sentences in the customer's own voice: what happened, \
what they expected, and any detail an agent would need (order number, amount, \
dates). Fictional order numbers and amounts are fine.
- `resolution` is one or two sentences saying what was actually done and how it \
ended. Include the outcome, not just the action.
- Write plain text. No markdown, no headings, no bullet points.
"""


class ResolvedTicket(BaseModel):
    """One past ticket, already closed."""

    customer_email: str = Field(description="Fictional customer email address.")
    summary: str = Field(description="One-line description of the issue.")
    body: str = Field(description="The customer's own account, 2-3 sentences.")
    resolution: str = Field(description="What was done and how it ended.")


class TicketBatch(BaseModel):
    """The model's reply.

    A list is wrapped in an object on purpose: structured outputs need an
    object at the root of the schema, so a bare `list[ResolvedTicket]` is not
    something the model can be asked for directly.
    """

    tickets: list[ResolvedTicket]


def build_agent(model: str) -> Agent[None]:
    return Agent(
        name="Ticket generator",
        model=model,
        instructions=INSTRUCTIONS,
        # output_type switches the model from prose to structured outputs, and
        # the SDK validates the reply against this model before handing it back.
        output_type=TicketBatch,
    )


def prompt_for(category: str, brief: str) -> str:
    return (
        f"Write {PER_CATEGORY} resolved tickets, all in this category:\n\n"
        f"{brief}\n\n"
        f"The {PER_CATEGORY} must be genuinely different cases, not one case in "
        f"{PER_CATEGORY} wordings. Give exactly one of them the email "
        f"{RECURRING_EMAILS[0]} or {RECURRING_EMAILS[1]}; invent the rest."
    )


# Short words and support boilerplate carry no signal about whether two tickets
# are the same case, so they are dropped before the overlap is measured.
_NOISE = frozenset(
    """the and for was were with that this from has had not you your our their
    but they she him her its been being are get got customer support ticket
    agent order account issue problem after before when what which who how"""
    .split()
)


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", text.lower()) if len(w) > 3 and w not in _NOISE}


def summary_overlaps(tickets: list[dict]) -> list[tuple[float, int, int]]:
    """Score every pair of summaries by word overlap, worst first.

    A cheap stand-in for reading all sixteen: if the model quietly produced four
    rewordings of one case, the top pair here comes back close to 1.0. It says
    nothing about semantic similarity -- that is what the embeddings are for.
    """
    scored = []
    for i, j in combinations(range(len(tickets)), 2):
        a, b = _content_words(tickets[i]["summary"]), _content_words(tickets[j]["summary"])
        if a and b:
            scored.append((len(a & b) / len(a | b), i, j))
    return sorted(scored, reverse=True)


async def generate(model: str) -> list[dict]:
    agent = build_agent(model)
    run_config = RunConfig(workflow_name="Seed ticket generation")

    # One run per category rather than one run for all sixteen: asking a model
    # for sixteen mixed tickets in a single reply reliably thins out the tail.
    results = await asyncio.gather(
        *(
            Runner.run(agent, prompt_for(name, brief), run_config=run_config)
            for name, brief in CATEGORIES.items()
        )
    )

    tickets = []
    for category, result in zip(CATEGORIES, results, strict=True):
        batch: TicketBatch = result.final_output
        # The category is stamped here instead of being asked for: the model has
        # no reason to mislabel it, and a field it cannot get wrong is one less
        # thing to check.
        tickets.extend({"category": category, **t.model_dump()} for t in batch.tickets)
    return tickets


def report(tickets: list[dict]) -> None:
    print(f"\n{len(tickets)} tickets generated\n")
    for i, t in enumerate(tickets):
        print(f"  [{i:>2}] {t['category']:<17} {t['customer_email']:<32} {t['summary']}")

    counts = {c: sum(t["category"] == c for t in tickets) for c in CATEGORIES}
    emails = [t["customer_email"] for t in tickets]
    reused = sorted({e for e in emails if emails.count(e) > 1})

    print("\n  by category :", "  ".join(f"{c}={n}" for c, n in counts.items()))
    print(f"  emails      : {len(set(emails))} distinct of {len(emails)}", end="")
    print(f", reused: {', '.join(reused)}" if reused else ", none reused")

    overlaps = summary_overlaps(tickets)
    if overlaps:
        print("\n  closest summary pairs (word overlap; high means a reworded duplicate):")
        for score, i, j in overlaps[:3]:
            print(f"    {score:.2f}  [{i}] {tickets[i]['summary']}")
            print(f"          [{j}] {tickets[j]['summary']}")


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv()
    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY is not set. Put it in .env and try again.")

    model = os.getenv("SEED_MODEL", DEFAULT_MODEL)
    print(f"generating {PER_CATEGORY} x {len(CATEGORIES)} tickets with {model} ...")
    tickets = await generate(model)
    report(tickets)

    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": model,
        "count": len(tickets),
        "tickets": tickets,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
