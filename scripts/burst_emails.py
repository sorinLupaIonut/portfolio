"""Fire a burst of emails at the durable function and check the caps held.

    uv run python -m scripts.burst_emails

Sends twenty ``customer/email.received`` events spread over five customers, waits
for every run to finish, then reconstructs from the dev server's own traces how
many steps were actually executing at each instant. Concurrency is the one
flow-control layer a single-tenant dev server really does enforce, so it is the
one we can prove here rather than merely configure.

Read-only apart from the events it sends: it asks the dev server what happened
and asks Neon what was written. It changes neither.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx
import inngest

from app.client import inngest_client
from app.functions import GLOBAL_CONCURRENCY, PER_CUSTOMER_CONCURRENCY
from app.worker import db

DEV_SERVER = os.getenv("INNGEST_BASE_URL", "http://127.0.0.1:8288")
EVENT_NAME = "customer/email.received"
AGENT_STEP = "draft-reply"

EMAILS_PER_CUSTOMER = 4
"""Twice the per-customer cap, so half of each customer's mail must wait."""

WAIT_TIMEOUT = timedelta(minutes=10)

# Ordinary questions on purpose. A refund email would park the run at the
# approval gate and leave the audit trail one row short, which is D5's business,
# not this one.
QUESTIONS: tuple[tuple[str, str], ...] = (
    ("How do I add a second seat?", "We hired someone and I cannot find where to add them."),
    ("Where do I download last month's invoice?", "I need it for our accountant today."),
    ("Can I change our billing email?", "Invoices should go to finance from now on."),
    ("Does the export include archived items?", "We are moving tools and want everything."),
)


@dataclass(frozen=True, slots=True)
class Span:
    """One executing step, as the dev server recorded it."""

    run_id: str
    from_email: str
    step: str
    start: datetime
    end: datetime


def _parse_time(value: str) -> datetime:
    """Read the dev server's RFC 3339 stamps, which carry sub-microsecond digits."""
    text = value.replace("Z", "+00:00")
    if "." in text:
        head, _, rest = text.partition(".")
        digits = ""
        while rest and rest[0].isdigit():
            digits, rest = digits + rest[0], rest[1:]
        text = f"{head}.{digits[:6].ljust(6, '0')}{rest or '+00:00'}"
    return datetime.fromisoformat(text).astimezone(timezone.utc)


async def _gql(client: httpx.AsyncClient, query: str, **variables: Any) -> dict[str, Any]:
    response = await client.post(
        f"{DEV_SERVER}/v0/gql", json={"query": query, "variables": variables}
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"dev server rejected the query: {body['errors']}")
    return body["data"]


def build_burst(customers: list[db.Customer], per_customer: int) -> list[inngest.Event]:
    """One event per (customer, question), all carrying the sender's address.

    ``from_email`` is not just payload: it is what the per-customer concurrency
    key reads. Inngest evaluates that expression against the event before the run
    starts, so the field has to be on the event, not looked up inside it.
    """
    return [
        inngest.Event(
            name=EVENT_NAME,
            data={
                "from_email": customer.email,
                "subject": subject,
                "body": body,
                "burst": True,
            },
        )
        for customer in customers
        for subject, body in QUESTIONS[:per_customer]
    ]


async def runs_for_events(client: httpx.AsyncClient, event_ids: list[str]) -> dict[str, str]:
    """Map each event id to the run it started, waiting for the runs to appear."""
    found: dict[str, str] = {}
    deadline = datetime.now(timezone.utc) + timedelta(seconds=60)
    while len(found) < len(event_ids) and datetime.now(timezone.utc) < deadline:
        for event_id in event_ids:
            if event_id in found:
                continue
            response = await client.get(f"{DEV_SERVER}/v1/events/{event_id}/runs")
            data = response.json().get("data") or []
            if data:
                found[event_id] = data[0]["run_id"]
        if len(found) < len(event_ids):
            await asyncio.sleep(0.5)
    missing = [e for e in event_ids if e not in found]
    if missing:
        raise RuntimeError(f"{len(missing)} events never started a run: {missing}")
    return found


async def wait_for_runs(client: httpx.AsyncClient, run_ids: list[str]) -> dict[str, str]:
    """Poll until every run reaches a terminal status. Returns run id -> status."""
    query = "query($runID: String!) { run(runID: $runID) { status } }"
    terminal = {"COMPLETED", "FAILED", "CANCELLED"}
    statuses: dict[str, str] = {}
    deadline = datetime.now(timezone.utc) + WAIT_TIMEOUT
    while len(statuses) < len(run_ids):
        if datetime.now(timezone.utc) > deadline:
            raise RuntimeError(f"gave up after {WAIT_TIMEOUT}; {len(statuses)} runs finished")
        for run_id in run_ids:
            if run_id in statuses:
                continue
            status = (await _gql(client, query, runID=run_id))["run"]["status"]
            if status in terminal:
                statuses[run_id] = status
        if len(statuses) < len(run_ids):
            print(f"  {len(statuses):>2}/{len(run_ids)} runs finished...")
            await asyncio.sleep(2)
    return statuses


async def step_spans(client: httpx.AsyncClient, run_to_email: dict[str, str]) -> list[Span]:
    """Every executing step of every run, which is exactly what concurrency counts.

    The dev server emits each span twice, a few milliseconds apart; that is a
    tracing artifact, not a second execution, so we collapse a run's spans per
    step name to the widest interval rather than letting the copy invent overlap.

    Only ``stepOp == "RUN"`` spans count. The trace also carries the executor's
    own bookkeeping ("Finalization", "executor.nonstep"), which is not work the
    concurrency cap governs.
    """
    query = """
    query($runID: String!) {
      runTrace(runID: $runID) {
        childrenSpans { name stepOp startedAt endedAt }
      }
    }
    """
    spans: list[Span] = []
    for run_id, from_email in run_to_email.items():
        data = await _gql(client, query, runID=run_id)
        widest: dict[str, tuple[datetime, datetime]] = {}
        for child in data["runTrace"]["childrenSpans"] or []:
            if child.get("stepOp") != "RUN":
                continue
            if not (child.get("startedAt") and child.get("endedAt")):
                continue
            name = child["name"]
            start, end = _parse_time(child["startedAt"]), _parse_time(child["endedAt"])
            if name in widest:
                start, end = min(start, widest[name][0]), max(end, widest[name][1])
            widest[name] = (start, end)
        if AGENT_STEP not in widest:
            raise RuntimeError(f"run {run_id} has no {AGENT_STEP} span: {sorted(widest)}")
        spans += [
            Span(run_id=run_id, from_email=from_email, step=name, start=start, end=end)
            for name, (start, end) in widest.items()
        ]
    return spans


def peak_overlap(spans: Iterable[Span]) -> int:
    """The most steps executing at the same instant, by sweep line.

    An end is processed before a start at the same timestamp: a step that
    finishes exactly as another begins never held a slot at the same moment.
    """
    spans = list(spans)  # read twice below; a generator would empty on the first
    edges = sorted(
        [(s.start, 1) for s in spans] + [(s.end, -1) for s in spans],
        key=lambda edge: (edge[0], edge[1]),
    )
    running = peak = 0
    for _, delta in edges:
        running += delta
        peak = max(peak, running)
    return peak


def draw_timeline(spans: list[Span], width: int = 56) -> list[str]:
    """One row per run, so the queueing is visible instead of merely counted."""
    origin = min(s.start for s in spans)
    total = max((max(s.end for s in spans) - origin).total_seconds(), 0.001)
    lines = []
    for span in sorted(spans, key=lambda s: (s.from_email, s.start)):
        left = int((span.start - origin).total_seconds() / total * (width - 1))
        right = int((span.end - origin).total_seconds() / total * (width - 1))
        bar = " " * left + "#" * max(right - left + 1, 1)
        who = span.from_email.split("@")[1].split(".")[0]
        lines.append(f"  {who:<9} |{bar.ljust(width)}|")
    return lines


def tally(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


async def main(per_customer: int) -> None:
    try:
        await run_burst(per_customer)
    finally:
        # Always, even on a failure: psycopg's pool keeps non-daemon threads
        # alive and the interpreter hangs on exit complaining about them.
        db.close_pool()


async def run_burst(per_customer: int) -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set in .env; refusing to call the model.")

    customers = await db.list_customers()
    events = build_burst(customers, per_customer)
    print(
        f"Sending {len(events)} events across {len(customers)} customers "
        f"(caps: {GLOBAL_CONCURRENCY} global, {PER_CUSTOMER_CONCURRENCY} per customer)"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        event_ids = await inngest_client.send(events)
        mapping = await runs_for_events(client, event_ids)
        run_to_email = {
            mapping[event_id]: event.data["from_email"]
            for event, event_id in zip(events, event_ids)
        }

        print(f"\n{len(run_to_email)} runs started. Waiting for them to finish...")
        statuses = await wait_for_runs(client, list(run_to_email))
        spans = await step_spans(client, run_to_email)

    print("\n--- Runs ---")
    for status, count in sorted(tally(statuses.values()).items()):
        print(f"  {status}: {count}")

    agent = [s for s in spans if s.step == AGENT_STEP]
    print("\n--- The agent step over time (one row per run) ---")
    for line in draw_timeline(agent):
        print(line)

    print("\n--- Peak concurrency (all steps, which is what the cap counts) ---")
    overall = peak_overlap(spans)
    print(
        f"  global: {overall} of {GLOBAL_CONCURRENCY} allowed -> "
        f"{'within the cap' if overall <= GLOBAL_CONCURRENCY else 'OVER THE CAP'}"
    )
    for email in sorted({s.from_email for s in spans}):
        peak = peak_overlap(s for s in spans if s.from_email == email)
        verdict = "ok" if peak <= PER_CUSTOMER_CONCURRENCY else "OVER THE CAP"
        print(f"  {email:<26} {peak} of {PER_CUSTOMER_CONCURRENCY} allowed -> {verdict}")

    print("\n--- Audit trail: one row in, one row out, per run ---")
    wrong = []
    for run_id in run_to_email:
        actions = [row["action"] for row in await db.recent_audit(run_id=run_id)]
        if actions != ["email_received", "reply_drafted"]:
            wrong.append((run_id, actions))
    for run_id, actions in wrong:
        print(f"  {run_id}: {actions}")
    if wrong:
        print(f"  {len(wrong)} of {len(run_to_email)} runs have the wrong rows")
    else:
        print(f"  all {len(run_to_email)} runs: email_received, reply_drafted. No duplicates.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--per-customer",
        type=int,
        default=EMAILS_PER_CUSTOMER,
        help=f"emails per customer (default {EMAILS_PER_CUSTOMER}, max {len(QUESTIONS)})",
    )
    args = parser.parse_args()
    asyncio.run(main(max(1, min(args.per_customer, len(QUESTIONS)))))
