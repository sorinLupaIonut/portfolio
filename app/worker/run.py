"""Running the agent on one email, and picking it back up after a decision.

This is the whole worker as a plain async function. Nothing here knows about
Inngest; the durable layer will call `handle_email` from inside a step and turn
the in-memory pause below into one that survives a crash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from agents import RunState, Runner

from . import db
from .agent import SupportContext, build_run_config, build_support_agent, pending_approvals

Status = Literal["replied", "awaiting_approval", "unknown_sender"]

MAX_RESUME_LAPS = 3
"""How many times a resumed run may come back still interrupted before we give up."""

REJECTION_MESSAGE = (
    "A human reviewer declined this refund. Do not call issue_refund again. "
    "Tell the customer plainly that the refund was not approved, do not imply it "
    "is still coming, and offer to look into it further if they disagree."
)
"""Fed back to the model on a rejection, so the reply says declined rather than
leaving the customer waiting for a refund that will never arrive."""


@dataclass(frozen=True, slots=True)
class IncomingEmail:
    from_email: str
    subject: str
    body: str


@dataclass(frozen=True, slots=True)
class EmailOutcome:
    status: Status
    customer_id: str | None = None
    reply: str | None = None
    pending: list[dict[str, Any]] = field(default_factory=list)
    state: str | None = None
    """The paused run, serialized. The durable gate stores this and resumes from it."""


def _prompt(email: IncomingEmail) -> str:
    return f"From: {email.from_email}\nSubject: {email.subject}\n\n{email.body}"


async def handle_email(email: IncomingEmail, *, run_id: str, request_id: str) -> EmailOutcome:
    """Draft a reply to one customer email, pausing if the agent wants a refund.

    ``run_id`` and ``request_id`` are passed in, never generated here: a fresh id
    invented inside this call would change on every retry once this runs inside a
    step, and a replay would diverge from the original run.
    """
    customer = await db.find_customer_by_email(email.from_email)
    if customer is None:
        return EmailOutcome(status="unknown_sender")

    # No request_id on the ingress row on purpose. Idempotency belongs on the
    # refund, the one action with an effect outside the database; an ingress row
    # per run is correct, and a recovery run should write its own.
    await db.write_audit(
        "email_received",
        customer_id=customer.id,
        details={"from": email.from_email, "subject": email.subject},
        run_id=run_id,
    )

    ctx = SupportContext(customer_id=customer.id, run_id=run_id, request_id=request_id)
    result = await Runner.run(
        build_support_agent(),
        _prompt(email),
        context=ctx,
        run_config=build_run_config(),
    )

    if result.interruptions:
        pending = pending_approvals(result)
        await db.write_audit(
            "refund_requested",
            customer_id=customer.id,
            details={"pending": pending},
            request_id=request_id,
            run_id=run_id,
        )
        return EmailOutcome(
            status="awaiting_approval",
            customer_id=customer.id,
            pending=pending,
            state=result.to_state().to_string(),
        )

    return await _finish(result, customer_id=customer.id, run_id=run_id)


async def resume_after_decision(
    state_str: str,
    *,
    approved: bool,
    customer_id: str,
    run_id: str,
    request_id: str,
    reviewer: str = "dev",
) -> EmailOutcome:
    """Pick the paused run back up with the human's answer and let it finish."""
    await db.write_audit(
        "refund_approved" if approved else "refund_rejected",
        customer_id=customer_id,
        details={"reviewer": reviewer},
        request_id=request_id,
        run_id=run_id,
    )

    agent = build_support_agent()
    ctx = SupportContext(customer_id=customer_id, run_id=run_id, request_id=request_id)
    # context_override is load-bearing: serializing a paused run drops the custom
    # context and only warns. Without it the refund tool resumes with no customer
    # id, writes nothing, and the run still reports success.
    state: RunState[SupportContext] = await RunState.from_string(
        agent, state_str, context_override=ctx
    )

    result: Any = None
    # One resume can leave further approvals pending, so loop until none remain.
    # The lap cap is not decoration: get the resume wrong and the run comes back
    # interrupted every time, which is a silent, paid, infinite loop.
    for _ in range(MAX_RESUME_LAPS):
        for item in state.get_interruptions():
            if approved:
                state.approve(item)
            else:
                state.reject(item, rejection_message=REJECTION_MESSAGE)
        # Do NOT pass context= here. Alongside a RunState it replaces the state's
        # context wrapper with a fresh one, and the approvals you just recorded
        # live on that wrapper: the run comes back with the same interruption,
        # forever, with no error to explain it. The context arrives through
        # RunState.from_string(context_override=...) above and nowhere else.
        result = await Runner.run(agent, state, run_config=build_run_config())
        if not result.interruptions:
            break
        state = result.to_state()
    else:
        raise RuntimeError(
            f"the run was still interrupted after {MAX_RESUME_LAPS} resumes; "
            "the decision is not reaching the tool"
        )

    return await _finish(result, customer_id=customer_id, run_id=run_id)


async def _finish(result: Any, *, customer_id: str, run_id: str) -> EmailOutcome:
    reply = result.final_output
    await db.write_audit(
        "reply_drafted",
        customer_id=customer_id,
        details={"characters": len(reply or "")},
        run_id=run_id,
    )
    return EmailOutcome(status="replied", customer_id=customer_id, reply=reply)
