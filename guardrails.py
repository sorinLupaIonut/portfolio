"""Input guardrails for the Worker, plus the redaction they imply.

One guardrail today: a customer-support Worker must never ingest a full card
number. Without this, a pasted PAN lands in `agent_messages` in plaintext and
stays there for the life of the database.

It is deliberately deterministic -- a Luhn check, no model call -- so it costs
nothing, cannot drift, and can be proved by typing a test number.
"""

import re

from agents import (
    GuardrailFunctionOutput,
    RunContextWrapper,
    input_guardrail,
)

# 13-19 digits is the ISO/IEC 7812 range for a primary account number. Separators
# are allowed between groups because that is how people actually paste them.
_CANDIDATE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

# A UUID is digits and hyphens too, and this system's order ids ARE UUIDs. Found
# by measurement: "00000000-0000-0000-0000-000000000000" matched the pattern
# above and passed Luhn (a zero sum is divisible by 10), so asking for a refund
# on an order tripped the card guardrail. Blanked before scanning, with a
# same-length filler so match offsets still line up with the original text.
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# A single repeated digit is a placeholder, not a card: 0000000000000000 passes
# Luhn. Set to 2, not higher -- the standard test card 4242 4242 4242 4242 has
# only two distinct digits, and a stricter rule silently stops detecting it.
_MIN_DISTINCT_DIGITS = 2


def _luhn_ok(digits: str) -> bool:
    """The checksum every real card number satisfies.

    Digit-length alone would flag order numbers, phone numbers and timestamps.
    Luhn cuts the false-positive rate to ~1 in 10 for random digit runs, which
    is the difference between a guardrail people keep and one they switch off.
    """
    total, double = 0, False
    for char in reversed(digits):
        value = ord(char) - 48
        if double:
            value *= 2
            if value > 9:
                value -= 9
        total += value
        double = not double
    return total % 10 == 0


def find_card_numbers(text: str) -> list[str]:
    """Every Luhn-valid 13-19 digit run in the text, UUIDs excluded."""
    scanned = _UUID.sub(lambda m: "\x00" * len(m.group()), text)
    found = []
    for match in _CANDIDATE.finditer(scanned):
        digits = re.sub(r"[ -]", "", match.group())
        if (
            13 <= len(digits) <= 19
            and len(set(digits)) >= _MIN_DISTINCT_DIGITS
            and _luhn_ok(digits)
        ):
            found.append(match.group())
    return found


def redact_card_numbers(text: str) -> str:
    """Mask any card number, keeping the last four digits.

    The audit helper runs this over message payloads before storing them. The
    guardrail blocks the turn, but `message_received` is written *before* the
    guardrail runs -- so without redaction here, the audit log would preserve
    the exact number the guardrail exists to keep out.
    """
    for raw in find_card_numbers(text):
        digits = re.sub(r"[ -]", "", raw)
        text = text.replace(raw, f"[card ending {digits[-4:]}]")
    return text


# run_in_parallel=False is load-bearing, not a tuning knob. The default (True)
# races the guardrail against the model call, so with it the card number is sent
# to OpenAI anyway and the tripwire only discards the reply -- which defeats the
# entire purpose of this guardrail. Blocking first costs one cheap regex.
@input_guardrail(name="no_card_numbers", run_in_parallel=False)
async def no_card_numbers(
    context: RunContextWrapper[None],
    agent: object,
    user_input: str | list,
) -> GuardrailFunctionOutput:
    """Trip when the customer pastes a full card number.

    Tripping raises InputGuardrailTripwireTriggered out of Runner.run before the
    model is ever called, so the number never reaches OpenAI and never reaches
    the session store.
    """
    text = user_input if isinstance(user_input, str) else str(user_input)
    found = find_card_numbers(text)
    return GuardrailFunctionOutput(
        # Only the last four, so the guardrail's own record is not a copy of what
        # it blocked.
        output_info={"matches": [re.sub(r"[ -]", "", f)[-4:] for f in found]},
        tripwire_triggered=bool(found),
    )
