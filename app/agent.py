"""The agents this Worker runs. For now: one hello-world greeter.

Built once at import time, not per run: an Agent is a plain config object (name,
instructions, model), so constructing it is cheap and stateless. The per-run work
is `Runner.run`, and that is what belongs inside a durable step.
"""

from agents import Agent

# gpt-4.1-mini is the cheap, fast default for this course. Swap the model here;
# nothing else in the codebase names it.
greeter = Agent(
    name="Greeter",
    instructions=(
        "You write a single warm, professional one-sentence greeting for a "
        "customer who has just contacted support. No preamble, no sign-off, "
        "no quotes: return the greeting itself and nothing else."
    ),
    model="gpt-4.1-mini",
)
