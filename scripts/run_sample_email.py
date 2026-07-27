"""Run the standalone worker on two sample emails and show what it did.

    uv run python scripts/run_sample_email.py            # the refund email pauses
    uv run python scripts/run_sample_email.py --approve  # ... then approve it
    uv run python scripts/run_sample_email.py --reject   # ... or reject it

The ids are minted here, outside the worker, which is where they will have to
come from once the worker runs inside a durable step.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid

from app.worker import db
from app.worker.run import (
    IncomingEmail,
    handle_email,
    record_decision,
    record_refund_blocked,
    record_reply,
    resume_after_decision,
)

QUESTION = IncomingEmail(
    from_email="support@initech.example",
    subject="How do I add a second seat?",
    body=(
        "Hi, we just hired someone and I cannot find where to add them to our "
        "workspace. Could you point me at it? Thanks."
    ),
)

REFUND = IncomingEmail(
    from_email="billing@acme.example",
    subject="Charged twice for order ORD-4471",
    body=(
        "We were billed 49.00 USD twice for order ORD-4471 on the same day. "
        "The second charge is a duplicate. Please refund it."
    ),
)


def rule(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


async def main(decision: str | None) -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set in .env; refusing to call the model.")

    seeded = await db.seed_customers_if_empty()
    print(f"customers seeded: {seeded} (0 means the table already had rows)")

    rule("Email 1 - an ordinary question (no refund expected)")
    run_id = f"local-{uuid.uuid4().hex[:8]}"
    outcome = await handle_email(QUESTION, run_id=run_id, request_id=f"{run_id}-req")
    print(f"status: {outcome.status}  customer: {outcome.customer_id}\n")
    print(outcome.reply)

    rule("Email 2 - a duplicate charge (the agent should reach for the refund tool)")
    run_id = f"local-{uuid.uuid4().hex[:8]}"
    request_id = f"{run_id}-req"
    outcome = await handle_email(REFUND, run_id=run_id, request_id=request_id)
    print(f"status: {outcome.status}  customer: {outcome.customer_id}")

    if outcome.status == "awaiting_approval":
        for call in outcome.pending:
            print(f"  paused on: {call['tool']}({call['arguments']})")
        print("\nThe refund did NOT run. The pause is the point; D5 makes it durable.")

        if decision is not None and outcome.state and outcome.customer_id:
            approved = decision == "approve"
            rule(f"A human answers: {decision.upper()}")
            await record_decision(
                approved=approved,
                customer_id=outcome.customer_id,
                run_id=run_id,
                request_id=request_id,
            )
            if not approved:
                # Same trail the durable function writes, so the standalone
                # worker and the wrapped one stay readable side by side.
                await record_refund_blocked(
                    customer_id=outcome.customer_id,
                    run_id=run_id,
                    request_id=request_id,
                    reason="rejected",
                )
            outcome = await resume_after_decision(
                outcome.state,
                approved=approved,
                customer_id=outcome.customer_id,
                run_id=run_id,
                request_id=request_id,
            )
            await record_reply(
                customer_id=outcome.customer_id, run_id=run_id, reply=outcome.reply
            )
            print(outcome.reply)
    else:
        print("\nThe agent answered without calling the refund tool:\n")
        print(outcome.reply)

    rule("Audit trail (newest first)")
    for row in await db.recent_audit(limit=8):
        print(f"  {row['created_at']:%H:%M:%S}  {row['action']:<16} {row['customer_id']:<14} {row['details']}")

    db.close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--approve", action="store_true", help="approve the paused refund in-process")
    group.add_argument("--reject", action="store_true", help="reject the paused refund in-process")
    args = parser.parse_args()
    choice = "approve" if args.approve else "reject" if args.reject else None
    asyncio.run(main(choice))
