---
name: escalate-with-context
description: Packages a customer conversation for handoff to a tier-2 support agent. Produces a structured escalation note with customer profile, issue summary, what was already tried, why escalation is recommended, and the suggested specialist team. Use when (a) the customer is on the Pro or Enterprise tier AND the issue is unresolved after one round of investigation, (b) the customer's sentiment is clearly negative, (c) the issue involves billing >$500 or a refund decision, or (d) the user explicitly asks for a human.
---

# Escalate with context

Hand a case to tier 2 with everything they need and nothing they have to ask for. A tier-2 specialist's first move is otherwise to re-read the whole thread; this note replaces that.

## First, check that escalation is actually warranted

Only escalate when one of the four conditions holds:

- **(a)** Pro or Enterprise tier **and** unresolved after a round of investigation — both halves, not either.
- **(b)** Sentiment is clearly negative: explicit anger, a threat to leave, or repeated contacts about the same unresolved thing.
- **(c)** Billing above $500, or any refund decision.
- **(d)** The customer asked for a human.

If none hold, say which one nearly held and why it did not, then keep working the case. This matters more than it looks: a Worker that escalates whenever things feel hard escalates everything, and a tier-2 queue that receives everything is indistinguishable from having no escalation path at all. The conditions are what give the escalation meaning.

`references/escalation-triggers.md` works each condition through one case that qualifies and one that nearly does. Read it when the call is close — the near-misses are where judgment is actually needed.

**On tier:** condition (a) needs the customer's account tier, which comes from `lookup_customer`. If you cannot retrieve it, treat (a) as unavailable and decide on (b), (c), (d) alone — and say in the note that tier was unknown. Never assume a tier to satisfy a condition; that turns an unmet bar into a met one by guessing.

## Then build the note

Get the case facts first by running **`summarize-ticket`**. It already produces the customer context, the issue, and what was tried, in a shape the receiver knows. Reuse it rather than restating those sections here — one summariser, maintained in one place, and this skill stays about the escalation decision.

Then produce:

```markdown
## Customer Profile
## Issue Summary
## Already Tried
## Why This Needs Tier 2
## Routing
```

**Customer Profile** — name, tier (or "unknown"), tenure, and history that changes handling. Say if this is a repeat contact on the same issue, and how many.

**Issue Summary** — the problem in two or three lines, from `summarize-ticket`'s Issue section. Keep the customer's reported symptom separate from any diagnosis nobody has confirmed.

**Already Tried** — every step taken and its outcome, from `summarize-ticket`'s Resolution Steps Taken. Failures included, and stated as failures. The tier-2 agent's most expensive mistake is repeating an attempt the customer already sat through.

**Why This Needs Tier 2** — name the condition that fired and the evidence for it: "condition (c) — refund decision of $840.00 exceeds tier-1 authority", "condition (b) — third contact in six days, customer used the words 'cancel my account'." Naming the condition lets the receiver disagree with the routing, and lets an auditor see later why this case left tier 1.

**Routing** — the suggested specialist team and target response time. `references/routing.md` maps issue classes to teams. Suggest, do not promise: if the note tells the customer anything about timing, it must be something tier 2 can actually meet.

## What not to do

Do not escalate and answer at the same time. If the case is going to tier 2, the customer should hear that it is being escalated — not receive a tier-1 attempt at a resolution alongside it, which reads as being handled twice and trusted neither time.

Do not soften the record to make the handoff look tidier. What was promised to the customer, and when, goes in the note exactly as promised, especially when we have already missed it.
