"""Durable functions served by this host."""

import datetime

import inngest
from agents import Runner

from .agent import greeter
from .client import inngest_client


@inngest_client.create_function(
    fn_id="greet-customer",
    trigger=inngest.TriggerEvent(event="demo/greet"),
)
async def greet_customer(ctx: inngest.Context) -> dict[str, str]:
    """Greet, wait fifteen seconds durably, then say goodbye.

    The sleep costs zero compute: the dev server holds the resume time and this
    process sits idle. It can crash or reload during the wait and the run still
    resumes at the next line, with the greeting returned from memo.
    """
    # Read outside the steps: this is deterministic, so it costs nothing to
    # recompute when the function re-enters after the sleep.
    name = ctx.event.data.get("name", "there")

    async def compose_greeting() -> str:
        # The whole model call lives inside the step. Its result is memoized, so
        # a retry of a later step never pays for a second call to OpenAI.
        result = await Runner.run(greeter, f"The customer's name is {name}.")
        return result.final_output

    greeting = await ctx.step.run("compose-greeting", compose_greeting)

    await ctx.step.sleep("wait-fifteen-seconds", datetime.timedelta(seconds=15))

    def compose_farewell() -> str:
        return f"Goodbye, {name}. We will be in touch soon."

    farewell = await ctx.step.run("compose-farewell", compose_farewell)

    return {"greeting": greeting, "farewell": farewell}
