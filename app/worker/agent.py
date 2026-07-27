"""The support agent: its context, its two tools, and the two factories that build it.

The agent and its run-config are built by separate functions on purpose. The
durable approval gate has to rebuild both from scratch when a run resumes hours
later in a different process, and it can only do that if neither is a module-level
singleton holding state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agents import Agent, RunConfig, RunContextWrapper, function_tool

from . import db

MODEL = "gpt-4.1-mini"

INSTRUCTIONS = """\
You are a customer-support agent for a small SaaS company. You answer one
incoming customer email at a time.

How to work:
1. Always call `lookup_customer` first, to see who is writing and on which plan.
   Enterprise and Pro customers get a slightly more attentive tone; never mention
   the plan name unless the customer brought it up.
2. If the email reports a duplicate charge, an overcharge, or an order that
   failed after being paid for, you MUST call the `issue_refund` tool. Do not
   promise a refund in prose without calling the tool: a promise nobody can act
   on is worse than no answer. Take the order id and the amount from the email;
   convert the amount to whole cents.
3. For anything else, just answer. Do not call `issue_refund` for a question, a
   complaint about speed, or a cancellation request.

Your final output is the body of the reply email and nothing else: no subject
line, no "Here is a draft", no placeholders in square brackets. Warm, plain,
under 150 words, and sign off as "The Support Team".
"""


@dataclass(frozen=True, slots=True)
class SupportContext:
    """Everything the tools need that the model must not be able to invent.

    All three fields are plain strings so the whole context can be rebuilt from a
    saved run and handed back on resume.
    """

    customer_id: str
    run_id: str
    request_id: str


@function_tool
async def lookup_customer(wrapper: RunContextWrapper[SupportContext]) -> str:
    """Look up the customer who sent this email: their id, email and plan tier."""
    customer = await db.find_customer(wrapper.context.customer_id)
    if customer is None:
        return "No such customer."
    return json.dumps({"id": customer.id, "email": customer.email, "tier": customer.tier})


@function_tool(needs_approval=True)
async def issue_refund(
    wrapper: RunContextWrapper[SupportContext],
    order_id: str,
    amount_cents: int,
    reason: str,
) -> str:
    """Refund a customer's order. Requires human approval before it runs.

    Args:
        order_id: The order being refunded, as the customer wrote it.
        amount_cents: The amount to refund, in whole cents.
        reason: One short sentence on why the refund is warranted.
    """
    ctx = wrapper.context
    written = await db.write_audit(
        "refund_issued",
        customer_id=ctx.customer_id,
        details={"order_id": order_id, "amount_cents": amount_cents, "reason": reason},
        request_id=ctx.request_id,
        run_id=ctx.run_id,
    )
    if not written:
        # The idempotency key caught a replay: the refund already happened once.
        return f"Refund for {order_id} was already issued; not issuing it twice."
    return f"Refund of {amount_cents / 100:.2f} issued for order {order_id}."


def build_support_agent() -> Agent[SupportContext]:
    """Build the agent fresh. Must produce the same tool shape every time.

    A resumed run is matched back against the tools that produced its saved
    state, so changing this list changes what an in-flight approval can resume.
    """
    return Agent[SupportContext](
        name="Support",
        instructions=INSTRUCTIONS,
        model=MODEL,
        tools=[lookup_customer, issue_refund],
    )


def build_run_config() -> RunConfig:
    """The run-config, kept apart from the agent so resume can re-supply it."""
    return RunConfig(workflow_name="customer-support")


def pending_approvals(result: Any) -> list[dict[str, Any]]:
    """Describe the tool calls a run stopped on, as plain JSON-able dicts."""
    pending: list[dict[str, Any]] = []
    for item in getattr(result, "interruptions", []) or []:
        raw = item.raw_item
        arguments = getattr(raw, "arguments", None) or "{}"
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            parsed = {"raw": arguments}
        pending.append({"tool": item.tool_name or getattr(raw, "name", "?"), "arguments": parsed})
    return pending
