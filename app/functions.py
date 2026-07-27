"""Durable functions served by this host.

The worker's logic is untouched; what changes is how the world reaches it. An
event wakes this function, and each unit of work sits in its own step.

Why the audit writes are steps of their own rather than lines inside the agent
step: a step's result is memoized the moment it succeeds. If the reply audit
fails and the run retries, the ingress row is not written a second time and the
agent is not called a second time, because both steps come back from memo. Fold
them into one step and a failure at the end redoes all three.
"""

import inngest

from .client import inngest_client
from .worker.run import (
    IncomingEmail,
    draft_reply,
    record_ingress,
    record_refund_requested,
    record_reply,
)


@inngest_client.create_function(
    fn_id="handle-customer-email",
    trigger=inngest.TriggerEvent(event="customer/email.received"),
)
async def handle_customer_email(ctx: inngest.Context) -> dict[str, object]:
    """Wake on a customer email, draft a reply, and record both ends of it."""
    email = IncomingEmail.from_event(ctx.event.data)

    # Both ids are read out here, not minted inside a step. A fresh uuid in a
    # step body would differ on every retry, and a replay would stop matching the
    # original run's audit rows.
    run_id = ctx.run_id
    request_id = f"{run_id}-refund"

    async def audit_ingress() -> str | None:
        return await record_ingress(email, run_id=run_id)

    customer_id = await ctx.step.run("audit-ingress", audit_ingress)
    if customer_id is None:
        return {"status": "unknown_sender", "from": email.from_email}

    async def draft() -> dict[str, object]:
        outcome = await draft_reply(
            email, customer_id=customer_id, run_id=run_id, request_id=request_id
        )
        # Only JSON-serializable values leave a step: Inngest stores this result
        # and hands it back verbatim on a retry.
        return outcome.as_dict()

    drafted = await ctx.step.run("draft-reply", draft)

    if drafted["status"] == "awaiting_approval":
        # The agent stopped at the refund tool. For now the run just records that
        # and ends; D5 turns this into a durable wait for a human's decision.
        async def audit_refund_requested() -> None:
            await record_refund_requested(
                customer_id=customer_id,
                run_id=run_id,
                request_id=request_id,
                pending=drafted["pending"],
            )

        await ctx.step.run("audit-refund-requested", audit_refund_requested)
        return drafted

    async def audit_reply() -> None:
        await record_reply(customer_id=customer_id, run_id=run_id, reply=drafted["reply"])

    await ctx.step.run("audit-reply", audit_reply)
    return drafted
