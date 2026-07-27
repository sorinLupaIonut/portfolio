"""Drive one refund end to end and watch the run survive the pause.

    uv run python -m scripts.decide_refund --approve
    uv run python -m scripts.decide_refund --reject

Sends a duplicate-charge email, waits until the run is genuinely suspended at
``await-refund-approval``, shows it sitting there, then sends the reviewer's
decision and shows the same run finishing on the turn where it stopped.

The pause is the thing being proved. Between the two halves of this script the
function host could be restarted and the run would still complete, because the
serialized agent state is held by Inngest rather than by any live process.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import inngest

from app.client import inngest_client
from app.functions import DECISION_EVENT
from app.worker import db
from scripts.burst_emails import _gql, runs_for_events

EMAIL_EVENT = "customer/email.received"
WAIT_STEP = "await-refund-approval"
SENDER = "billing@acme.example"

REFUND_EMAIL = {
    "from_email": SENDER,
    "subject": "Charged twice for order ORD-4471",
    "body": (
        "We were billed 49.00 USD twice for order ORD-4471 on the same day. "
        "The second charge is a duplicate. Please refund it."
    ),
}

RUN_QUERY = """
query($runID: String!) {
  run(runID: $runID) { status }
  runTrace(runID: $runID) { childrenSpans { name stepOp status startedAt endedAt } }
}
"""


def steps_of(trace: dict) -> list[tuple[str, str, str]]:
    """The run's steps in order, one entry each, with the tracer's copies merged."""
    seen: dict[str, tuple[str, str, str]] = {}
    for span in trace["childrenSpans"] or []:
        if span["stepOp"] and span["name"] not in seen:
            seen[span["name"]] = (span["name"], span["stepOp"], span["status"])
    return list(seen.values())


async def wait_until_suspended(client: httpx.AsyncClient, run_id: str) -> list[tuple[str, str, str]]:
    """Poll until the run is parked at the approval step, and report what it did."""
    deadline = datetime.now(timezone.utc) + timedelta(minutes=3)
    while datetime.now(timezone.utc) < deadline:
        data = await _gql(client, RUN_QUERY, runID=run_id)
        steps = steps_of(data["runTrace"])
        waiting = [s for s in steps if s[0] == WAIT_STEP]
        if waiting:
            return steps
        if data["run"]["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            raise RuntimeError(
                f"run finished as {data['run']['status']} without ever waiting; "
                f"steps: {[s[0] for s in steps]}"
            )
        await asyncio.sleep(1)
    raise RuntimeError(f"run {run_id} never reached {WAIT_STEP}")


async def wait_until_done(client: httpx.AsyncClient, run_id: str) -> tuple[str, list]:
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    while datetime.now(timezone.utc) < deadline:
        data = await _gql(client, RUN_QUERY, runID=run_id)
        if data["run"]["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            return data["run"]["status"], steps_of(data["runTrace"])
        await asyncio.sleep(2)
    raise RuntimeError(f"run {run_id} never finished")


def show_steps(steps: list[tuple[str, str, str]]) -> None:
    for name, op, status in steps:
        print(f"  {name:<26} {op:<16} {status}")


async def show_audit(run_id: str) -> list[str]:
    rows = await db.recent_audit(run_id=run_id)
    for row in rows:
        print(f"  {row['created_at']:%H:%M:%S}  {row['action']:<18} {row['details']}")
    return [row["action"] for row in rows]


async def main(approved: bool) -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set in .env; refusing to call the model.")

    customer = await db.find_customer_by_email(SENDER)
    if customer is None:
        raise SystemExit(f"{SENDER} is not in the customers table; seed it first.")

    verdict = "APPROVE" if approved else "REJECT"
    print(f"Sending the duplicate-charge email from {SENDER}; the reviewer will {verdict}.")

    async with httpx.AsyncClient(timeout=30) as client:
        [event_id] = await inngest_client.send(
            inngest.Event(name=EMAIL_EVENT, data=REFUND_EMAIL)
        )
        run_id = (await runs_for_events(client, [event_id]))[event_id]
        print(f"run {run_id}\n")

        print("--- Waiting for the run to suspend ---")
        started = datetime.now(timezone.utc)
        steps = await wait_until_suspended(client, run_id)
        show_steps(steps)
        print("\n  The run is parked. Nothing of ours is running; Inngest holds the")
        print("  serialized agent state and will hand it back when the decision arrives.")

        print("\n--- Audit trail so far ---")
        await show_audit(run_id)

        print(f"\n--- Sending the decision: {verdict} ---")
        await inngest_client.send(
            inngest.Event(
                name=DECISION_EVENT,
                data={
                    "customer_id": customer.id,
                    "run_id": run_id,
                    "approved": approved,
                    "reviewer": "sorin",
                },
            )
        )

        status, steps = await wait_until_done(client, run_id)
        waited = (datetime.now(timezone.utc) - started).total_seconds()
        print(f"\n--- Run {status} after {waited:.0f}s ---")
        show_steps(steps)

    print("\n--- Audit trail, final ---")
    actions = await show_audit(run_id)

    print("\n--- The reply that went to the customer ---")
    reply = next(
        (r for r in await db.recent_audit(run_id=run_id) if r["action"] == "reply_drafted"),
        None,
    )
    output = await _run_output(run_id)
    print(f"  ({reply['details']['characters'] if reply else 0} characters)\n")
    print(output or "  <no final output captured>")

    print("\n--- Verdict ---")
    issued = actions.count("refund_issued")
    blocked = actions.count("refund_blocked")
    want_issued, want_blocked = (1, 0) if approved else (0, 1)
    ok = issued == want_issued and blocked == want_blocked
    print(f"  refund_issued:  {issued} (expected {want_issued})")
    print(f"  refund_blocked: {blocked} (expected {want_blocked})")
    print(f"  {'PASS' if ok else 'FAIL'}")

    db.close_pool()


async def _run_output(run_id: str) -> str | None:
    """The run's return value, which carries the reply the agent finished with."""
    query = "query($runID: String!) { run(runID: $runID) { output } }"
    async with httpx.AsyncClient(timeout=30) as client:
        data = await _gql(client, query, runID=run_id)
    raw = data["run"]["output"]
    if not raw:
        return None
    parsed = json.loads(raw)
    return parsed.get("reply") if isinstance(parsed, dict) else str(parsed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--approve", action="store_true", help="the reviewer approves the refund")
    group.add_argument("--reject", action="store_true", help="the reviewer declines it")
    args = parser.parse_args()
    try:
        asyncio.run(main(approved=args.approve))
    finally:
        db.close_pool()
