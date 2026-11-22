"""Durable functions served by this host.

The worker's logic is untouched; what changes is how the world reaches it. An
event wakes this function, and each unit of work sits in its own step.

Why the audit writes are steps of their own rather than lines inside the agent
step: a step's result is memoized the moment it succeeds. If the reply audit
fails and the run retries, the ingress row is not written a second time and the
agent is not called a second time, because both steps come back from memo. Fold
them into one step and a failure at the end redoes all three.

Flow control lives here too, as configuration on the function rather than code in
the handler. Three limits, three different resources: a global concurrency cap
for the database's connections, a per-customer cap so one loud sender cannot take
the whole pool, and a throttle for OpenAI's per-minute quota. The worker below is
untouched by all of it.
"""

from datetime import timedelta

import inngest

from .client import inngest_client
from .worker.run import (
    APPROVAL_WINDOW,
    IncomingEmail,
    draft_reply,
    list_health_check_customers,
    notify_reviewer,
    record_decision,
    record_health_check,
    record_ingress,
    record_refund_blocked,
    record_refund_requested,
    record_reply,
    resume_after_decision,
)

DECISION_EVENT = "refund/approval.decided"
"""The reviewer's answer: ``{"customer_id", "run_id", "approved", "reviewer"}``.

An event rather than an HTTP call to the waiting process, because there may not
be a waiting process. The run that asked for this decision can be suspended
across a crash, a deploy, or four hours of nothing; the event is delivered to
whatever picks it back up."""

HEALTH_CHECK_EVENT = "customer/health_check.requested"

RUN_CRON_NOW_EVENT = "ops/health_check.run_now"
"""An operational way in, alongside the schedule. A cron-only function cannot be
invoked by hand (the dev server has no event to route), and waiting for 09:00 is
no way to test a fan-out. In production this is also the "run it now" button
after a failed nightly."""


GLOBAL_CONCURRENCY = 10
"""Ceiling on steps of this function executing at once, whoever sent the email.

Sized against the narrowest resource downstream, which here is the Postgres
connection pool in ``worker/db.py``: that pool's ``max_size`` is the same number
on purpose, so a full house of runs still finds a connection instead of queueing
inside psycopg where Inngest cannot see it.

Note "steps", not "runs". Inngest counts executing steps, so a run parked in
``step.wait_for_event`` (D5's refund approval) holds no slot at all. That is what
makes a four-hour human pause affordable."""

PER_CUSTOMER_CONCURRENCY = 2
"""...and no single sender may take more than this share of the ceiling.

Without it, one customer who forwards forty emails fills all ten slots and
everyone else waits behind them, even though there is nothing wrong with the
system. The key is a CEL expression over the event, so Inngest can evaluate it
before the run starts; the sender's address is the only customer identity that
exists at that point, since the id is not resolved until the ingress step."""

OPENAI_REQUESTS_PER_MINUTE = 30
"""The downstream per-minute quota, which is a different limit from the two above.

Concurrency answers "how many at once"; throttle answers "how many may start per
minute". A cap of ten in flight still exceeds thirty a minute if each run is
quick, so the connection pool and the OpenAI quota each need their own control.

``burst`` is the pair of this number, not a decoration. It defaults to 1, which
smooths thirty a minute into one every two seconds, stricter than the real quota
and enough to serialize a perfectly legitimate spike. A per-minute bucket allows
all thirty at once, so that is what we configure.

Unlike the concurrency caps, this one is configured here and proven elsewhere:
the local dev server records the setting but does not act on it. Measured, not
assumed. Temporarily set to twelve a minute with ``burst=1``, which should have
started five runs five seconds apart, all five started in the same instant."""


@inngest_client.create_function(
    fn_id="handle-customer-email",
    trigger=inngest.TriggerEvent(event="customer/email.received"),
    concurrency=[
        inngest.Concurrency(limit=GLOBAL_CONCURRENCY),
        inngest.Concurrency(
            limit=PER_CUSTOMER_CONCURRENCY, key="event.data.from_email"
        ),
    ],
    throttle=inngest.Throttle(
        limit=OPENAI_REQUESTS_PER_MINUTE,
        period=timedelta(minutes=1),
        burst=OPENAI_REQUESTS_PER_MINUTE,
    ),
)
async def handle_customer_email(ctx: inngest.Context) -> dict[str, object]:
    """Wake on a customer email, draft a reply, and record both ends of it.

    When the agent reaches for a refund it stops and waits for a human, and that
    wait is durable: the run can be suspended for four hours, across a crash or a
    deploy, and still finish on the exact turn where it stopped.
    """
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

    if drafted["status"] != "awaiting_approval":

        async def audit_reply() -> None:
            await record_reply(customer_id=customer_id, run_id=run_id, reply=drafted["reply"])

        await ctx.step.run("audit-reply", audit_reply)
        return drafted

    # The agent stopped at the refund tool. Everything from here down is the
    # durable pause: the SDK's own interruption only lives as long as the
    # process, and the whole point is to outlive it.
    #
    # Where the paused run is kept is the quiet part. `draft-reply` already
    # returned the serialized RunState as its output, so Inngest is holding it,
    # not this process. That is what makes the wait below free: nothing of ours
    # has to stay alive, and the run holds no concurrency slot while it waits.

    async def audit_refund_requested() -> None:
        await record_refund_requested(
            customer_id=customer_id,
            run_id=run_id,
            request_id=request_id,
            pending=drafted["pending"],
        )

    await ctx.step.run("audit-refund-requested", audit_refund_requested)

    async def notify() -> dict[str, object]:
        return await notify_reviewer(
            customer_id=customer_id,
            run_id=run_id,
            request_id=request_id,
            pending=drafted["pending"],
        )

    await ctx.step.run("notify-reviewer", notify)

    decision = await ctx.step.wait_for_event(
        "await-refund-approval",
        event=DECISION_EVENT,
        timeout=APPROVAL_WINDOW,
        # Matched on the run as well as the customer. On the customer alone, an
        # approval meant for one refund would release any other refund pending
        # for the same customer, which is the wrong money on the wrong order.
        if_exp=(
            f"async.data.customer_id == '{customer_id}' "
            f"&& async.data.run_id == '{run_id}'"
        ),
    )

    if decision is None:
        # Four hours, no answer. The refund is dropped rather than left pending
        # forever, and that is a fact worth a row: an unanswered request looks
        # nothing like a refused one when you come back to read the trail.
        async def audit_expired() -> None:
            await record_refund_blocked(
                customer_id=customer_id,
                run_id=run_id,
                request_id=request_id,
                reason="expired",
            )

        await ctx.step.run("audit-refund-expired", audit_expired)
        return {"status": "expired", "customer_id": customer_id}

    approved = bool(decision.data.get("approved"))
    reviewer = str(decision.data.get("reviewer") or "unknown")

    async def audit_decision() -> None:
        await record_decision(
            approved=approved,
            customer_id=customer_id,
            run_id=run_id,
            request_id=request_id,
            reviewer=reviewer,
        )

    await ctx.step.run("audit-decision", audit_decision)

    if not approved:

        async def audit_blocked() -> None:
            await record_refund_blocked(
                customer_id=customer_id,
                run_id=run_id,
                request_id=request_id,
                reason="rejected",
            )

        await ctx.step.run("audit-refund-blocked", audit_blocked)

    async def resume() -> dict[str, object]:
        # The agent run lives inside the step. Outside it, Inngest would re-enter
        # this code on every retry and call the model again, paying twice and
        # firing the refund tool twice. Memoized, the refund fires once.
        outcome = await resume_after_decision(
            drafted["state"],
            approved=approved,
            customer_id=customer_id,
            run_id=run_id,
            request_id=request_id,
        )
        return outcome.as_dict()

    resumed = await ctx.step.run("resume-after-decision", resume)

    async def audit_resumed_reply() -> None:
        await record_reply(customer_id=customer_id, run_id=run_id, reply=resumed["reply"])

    await ctx.step.run("audit-reply-after-decision", audit_resumed_reply)
    return resumed


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
