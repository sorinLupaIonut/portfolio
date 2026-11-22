# The four conditions, worked

Each condition gets one case that qualifies and one that nearly does. The near-misses are the point — the qualifying cases are rarely the hard call.

---

## (a) Pro or Enterprise tier AND unresolved after a round of investigation

Both halves must hold. Tier alone is not a trigger, and neither is difficulty.

**Qualifies.** Enterprise customer reports API webhooks silently dropping. An agent checked delivery logs, confirmed the drops, retried the endpoint, and could not determine a cause. Investigated once, still unresolved, Enterprise tier — escalate.

**Does not.** Enterprise customer asks why their invoice shows proration. Nothing has been investigated yet; it is a first-contact question with a known answer. Enterprise tier does not make an unanswered question an escalation. Answer it.

The phrase "a round of investigation" means someone actually looked — checked a log, ran a lookup, tested a reproduction. A reply asking the customer for more information is not a round of investigation.

---

## (b) Sentiment is clearly negative

The word is **clearly**. Frustration is normal in support and is not by itself a trigger; this condition is for anger, threats to leave, and the slow burn of repeated contact about the same unresolved thing.

**Qualifies.** Third contact in six days on the same stuck refund. Latest message: "this is ridiculous, either fix it today or cancel my account." Explicit threat plus repetition — escalate.

**Does not.** Standard-tier customer opens with "I'm really annoyed, this is the second time this month a delivery has been late" — and the fix is a redelivery you can arrange right now. Annoyed but resolvable. Solve it; escalating adds a day and helps nobody.

The distinction is not how strong the language is, it is whether tier 1 can still fix the thing they are angry about.

---

## (c) Billing above $500, or any refund decision

This one is deliberately mechanical, because money decisions should not depend on how the day is going.

**Qualifies.** Customer requests a refund of $840.00 for a cancelled annual plan. Both halves fire — refund decision, and over $500.

Note that "any refund decision" has no threshold. A $12 refund is still a refund decision and still escalates under this condition, because the authority to move money is what is being checked, not the amount.

**Does not.** Customer asks what a $612.00 line item on their invoice was for. It is above $500 and it is billing, but nothing is being decided — they want an explanation. Answer it. If the answer turns out to be that we charged them wrongly, *that* becomes a refund decision and the condition fires then.

---

## (d) The customer explicitly asks for a human

The cheapest condition to check and the one most often argued with. Do not talk them out of it, and do not require them to justify it.

**Qualifies.** "Can I please talk to a real person about this."

**Does not.** "Is there a phone number for support?" — that is a question about channels, and it has an answer. Answer it. If they then ask to be put through, condition (d) fires.

---

## When two conditions fire

Name all of them in the note, and route on the most specific. A furious Enterprise customer demanding an $840 refund is (a), (b) and (c) at once — but (c) determines who can actually approve it, so it routes to billing. Sentiment shapes the urgency, not the destination.
