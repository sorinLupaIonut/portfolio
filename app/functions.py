"""Durable functions served by this host.

This is the first layer of the nervous system. It adds nothing to the worker's
logic: it takes the same `handle_email` call the standalone script makes and puts
it inside a step, so its result is stored the moment it succeeds.

What that buys: the agent call is the expensive part of the run, several seconds
and real tokens. Inside `step.run` its result is memoized, so when a later step
fails and Inngest retries the run, the agent is not called again. Without the
step, every retry re-pays for the model and re-fires its tools.
"""

import inngest

from .client import inngest_client
from .worker.run import IncomingEmail, handle_email


@inngest_client.create_function(
    fn_id="handle-customer-email",
    trigger=inngest.TriggerEvent(event="customer/email.received"),
)
async def handle_customer_email(ctx: inngest.Context) -> dict[str, object]:
    """Draft a reply to one customer email, durably."""
    data = ctx.event.data
    email = IncomingEmail(
        from_email=data["from_email"],
        subject=data.get("subject", ""),
        body=data.get("body", ""),
    )

    # Both ids are read out here, not minted inside the step. A fresh uuid in a
    # step body would differ on every retry, and a replay would stop matching the
    # original run's audit rows.
    run_id = ctx.run_id
    request_id = f"{run_id}-refund"

    async def draft_reply() -> dict[str, object]:
        outcome = await handle_email(email, run_id=run_id, request_id=request_id)
        # Only JSON-serializable values leave a step: Inngest stores this result
        # and hands it back verbatim on a retry.
        return {
            "status": outcome.status,
            "customer_id": outcome.customer_id,
            "reply": outcome.reply,
            "pending": outcome.pending,
            "state": outcome.state,
        }

    # One step, the whole agent call inside it. D2 splits the audit writes out
    # into steps of their own so each is memoized separately.
    return await ctx.step.run("draft-reply", draft_reply)
