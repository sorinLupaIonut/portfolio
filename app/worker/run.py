"""Running the agent on one email, and picking it back up after a decision.

This is the whole worker as plain async functions. Nothing here knows about
Inngest; the durable layer calls these pieces from inside steps.

The pieces are deliberately small and separately callable. A durable function
wants each unit of work in its own step, so that a failure in one does not redo
the ones already finished; `handle_email` at the bottom composes the same pieces
for running the worker standalone.
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

    @classmethod
    def from_event(cls, data: dict[str, Any]) -> IncomingEmail:
        return cls(
            from_email=data["from_email"],
            subject=data.get("subject", ""),
            body=data.get("body", ""),
        )


@dataclass(frozen=True, slots=True)
class EmailOutcome:
    status: Status
    customer_id: str | None = None
    reply: str | None = None
    pending: list[dict[str, Any]] = field(default_factory=list)
    state: str | None = None
    """The paused run, serialized. The durable gate stores this and resumes from it."""

    def as_dict(self) -> dict[str, Any]:
        """A step's result has to survive a round trip through JSON."""
        return {
            "status": self.status,
            "customer_id": self.customer_id,
            "reply": self.reply,
            "pending": self.pending,
            "state": self.state,
        }


def _prompt(email: IncomingEmail) -> str:
    return f"From: {email.from_email}\nSubject: {email.subject}\n\n{email.body}"


async def record_ingress(email: IncomingEmail, *, run_id: str) -> str | None:
    """Resolve the sender and write the ingress audit row. Returns the customer id.

    The lookup lives here rather than in the caller so that a durable function
    pays for it once: the customer id comes back as this step's memoized result
    and the later steps read it from there instead of hitting Neon again.

    No request_id on this row on purpose. Idempotency belongs on the refund, the
    one action with an effect outside the database. An ingress row per run is
    correct, and a recovery run after a bad deploy should write its own.
    """
    customer = await db.find_customer_by_email(email.from_email)
    if customer is None:
        return None

    await db.write_audit(
        "email_received",
        customer_id=customer.id,
        details={"from": email.from_email, "subject": email.subject},
        run_id=run_id,
    )
    return customer.id


async def draft_reply(
    email: IncomingEmail, *, customer_id: str, run_id: str, request_id: str
) -> EmailOutcome:
    """Run the agent on the email. Writes nothing: the caller records the outcome.

    ``run_id`` and ``request_id`` are passed in, never generated here. A fresh id
    minted inside this call would change on every retry once this runs inside a
    step, and a replay would diverge from the original run.
    """
    ctx = SupportContext(customer_id=customer_id, run_id=run_id, request_id=request_id)
    result = await Runner.run(
        build_support_agent(),
        _prompt(email),
        context=ctx,
        run_config=build_run_config(),
    )

    if result.interruptions:
        return EmailOutcome(
            status="awaiting_approval",
            customer_id=customer_id,
            pending=pending_approvals(result),
            state=result.to_state().to_string(),
        )

    return EmailOutcome(status="replied", customer_id=customer_id, reply=result.final_output)


async def record_refund_requested(
    *, customer_id: str, run_id: str, request_id: str, pending: list[dict[str, Any]]
) -> None:
    """Record that the agent stopped and is waiting on a human."""
    await db.write_audit(
        "refund_requested",
        customer_id=customer_id,
        details={"pending": pending},
        request_id=request_id,
        run_id=run_id,
    )


async def record_decision(
    *, approved: bool, customer_id: str, run_id: str, request_id: str, reviewer: str = "dev"
) -> None:
    """Record the human's answer, before the agent is resumed with it."""
    await db.write_audit(
        "refund_approved" if approved else "refund_rejected",
        customer_id=customer_id,
        details={"reviewer": reviewer},
        request_id=request_id,
        run_id=run_id,
    )


async def record_reply(*, customer_id: str, run_id: str, reply: str | None) -> None:
    """Record that a reply left the worker."""
    await db.write_audit(
        "reply_drafted",
        customer_id=customer_id,
        details={"characters": len(reply or "")},
        run_id=run_id,
    )


async def resume_after_decision(
    state_str: str,
    *,
    approved: bool,
    customer_id: str,
    run_id: str,
    request_id: str,
) -> EmailOutcome:
    """Pick the paused run back up with the human's answer and let it finish."""
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

    return EmailOutcome(status="replied", customer_id=customer_id, reply=result.final_output)


async def handle_email(email: IncomingEmail, *, run_id: str, request_id: str) -> EmailOutcome:
    """The whole thing in one call, for running the worker without a nervous system.

    The durable function does not use this: it calls the pieces above so each one
    lands in its own step. This is what the standalone script runs.
    """
    customer_id = await record_ingress(email, run_id=run_id)
    if customer_id is None:
        return EmailOutcome(status="unknown_sender")

    outcome = await draft_reply(
        email, customer_id=customer_id, run_id=run_id, request_id=request_id
    )

    if outcome.status == "awaiting_approval":
        await record_refund_requested(
            customer_id=customer_id,
            run_id=run_id,
            request_id=request_id,
            pending=outcome.pending,
        )
    else:
        await record_reply(customer_id=customer_id, run_id=run_id, reply=outcome.reply)

    return outcome
