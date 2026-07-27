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
    list_health_check_customers,
    record_health_check,
    record_ingress,
    record_refund_requested,
    record_reply,
)

HEALTH_CHECK_EVENT = "customer/health_check.requested"

RUN_CRON_NOW_EVENT = "ops/health_check.run_now"
"""An operational way in, alongside the schedule. A cron-only function cannot be
invoked by hand (the dev server has no event to route), and waiting for 09:00 is
no way to test a fan-out. In production this is also the "run it now" button
after a failed nightly."""


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


@inngest_client.create_function(
    fn_id="daily-customer-health-check",
    trigger=[
        inngest.TriggerCron(cron="0 9 * * *"),
        inngest.TriggerEvent(event=RUN_CRON_NOW_EVENT),
    ],
)
async def daily_customer_health_check(ctx: inngest.Context) -> dict[str, object]:
    """Once a day, ask for one health check per paying customer.

    The parent does no work of its own: it emits N events and returns in
    milliseconds. Each child is then its own run, isolated, retried on its own,
    capped by its own concurrency. A loop in here instead would couple them, so
    one slow customer would hold up the rest and a crash would lose the batch.
    """
    # The tick identifies this firing. Retries and re-deliveries of this run keep
    # the same run id, which is what makes the event ids below stable.
    tick = ctx.run_id

    async def load_eligible() -> list[str]:
        return await list_health_check_customers()

    customer_ids = await ctx.step.run("load-eligible-customers", load_eligible)

    events = [
        inngest.Event(
            name=HEALTH_CHECK_EVENT,
            data={"customer_id": customer_id, "check_id": f"health-{customer_id}-{tick}"},
            # The event id is the idempotency key. Inngest drops a duplicate id
            # within a 24-hour window, so a re-delivered tick cannot give a
            # customer a second check.
            id=f"health-{customer_id}-{tick}",
        )
        for customer_id in customer_ids
    ]

    if events:
        # Inside a step, not a bare client send: a retry of the fan-out then
        # returns from memo instead of emitting every event a second time.
        await ctx.step.send_event("fan-out-health-checks", events)

    return {"tick": tick, "customers": customer_ids, "sent": len(events)}


@inngest_client.create_function(
    fn_id="run-customer-health-check",
    trigger=inngest.TriggerEvent(event=HEALTH_CHECK_EVENT),
)
async def run_customer_health_check(ctx: inngest.Context) -> dict[str, object]:
    """One customer's daily check: its own durable run, its own audit row."""
    customer_id = ctx.event.data["customer_id"]
    check_id = ctx.event.data["check_id"]
    run_id = ctx.run_id

    async def audit_health_check() -> bool:
        return await record_health_check(
            customer_id=customer_id, run_id=run_id, request_id=check_id
        )

    written = await ctx.step.run("audit-health-check", audit_health_check)
    return {"customer_id": customer_id, "check_id": check_id, "written": written}
