---
name: summarize-ticket
description: Turns one support ticket or thread into a five-section handoff note — Customer Context, Issue, Resolution Steps Taken, Current Status, Recommended Next Action — so a colleague can pick the case up cold. Use whenever someone needs to hand a case off, brief a lead, take over another agent's thread, catch up on where a case stands, or establish the current state before escalating, including phrasings that never say "summarize": "write a handoff note for #4471", "TL;DR this thread", "where does this stand before I escalate", "what's the story on 1042", "bring me up to speed on Sam's case". Do not use it for drafting a reply to the customer, for triaging or ranking a batch of tickets, or for reporting on ticket volume, trends, or metrics.
---

# Summarize ticket

Turn **one** ticket into a handoff note: everything the next person needs to take the case over cold, and nothing they would skim past.

Picture the reader. They have never seen this case, and they are about to act on it — reply, refund, escalate, close. They are not auditing history; they are deciding what to do in the next five minutes. Every choice below follows from that.

## Get the facts before writing

Work only from ticket content that is actually in front of you: the thread in this conversation, a pasted transcript, or the result of a lookup tool.

If you are handed a bare ticket number with no content, retrieve it through the `customer-data` MCP tools. Never query the database directly — the running Worker reaches business data only through that server, and a skill that opens its own connection breaks that boundary silently.

Refuse only when you have essentially nothing — a ticket number and no facts at all. Then say what you are missing and ask for it, rather than producing a plausible-looking note about a ticket you cannot see. The receiver will act on this note without re-reading the source; a confident invention is worse than an admitted gap.

Partial facts are the normal case, and they are enough. Write all five sections from what you were given and name each gap in place: "tier unknown", "no order number given", "None yet." A note that says what is missing is exactly what lets the receiver go find it — whereas refusing to write anything leaves them with nothing to act on and no idea what to collect.

## The output

Use these five headings, in this order, every time — including when a section is thin. The receiver scans by position, and a dropped heading reads as "nothing to report" when it usually means "unknown":

```markdown
## Customer Context
## Issue
## Resolution Steps Taken
## Current Status
## Recommended Next Action
```

**Customer Context** — who this is and how much latitude the receiver has: name, account tier, tenure or order volume if known, and any history that changes how this case should be handled (a third complaint this month is context; a clean two-year record is also context). One or two lines. If tier is unknown, write "tier unknown" rather than guessing — tier drives refund authority and escalation routing downstream, so a guess here becomes a wrong decision there.

**Issue** — what the customer says is wrong, in their words, plus the order, product, or account it concerns. Keep the reported symptom separate from any diagnosis: "package arrived empty" is what they said; "likely warehouse pick error" is what an agent concluded. Marking which is which stops the receiver from inheriting a theory as a fact.

**Resolution Steps Taken** — what has already been tried, in order, each with who did it and what came of it. Include the failures; they are the most valuable lines in the note. The single most expensive handoff failure is the new agent cheerfully repeating a step that already failed, in front of a customer who remembers it. If nothing has been tried, write "None yet." — do not fill the section with the fact that the ticket was created.

**Current Status** — the state of the case right now, who the ball is with, and anything time-bound: an SLA clock, a promised callback, a shipment in transit, a refund awaiting approval. If the customer is waiting on us and has been for a while, that belongs here, because it changes the urgency of everything below it.

**Recommended Next Action** — one concrete action the receiver can take next, specific enough to start on without another decision. "Refund the $89.99 to the original card and confirm in-thread" is an action; "follow up with the customer" is a shrug. If the right next step genuinely depends on something unknown, name the unknown and the action that resolves it. One recommendation, not a menu — a list of options hands the decision back.

## Length and fidelity

Aim for something readable in under a minute: roughly 150–250 words in total, prose or tight bullets, no preamble and no closing summary of the summary. A handoff nobody finishes is a handoff nobody read.

Every claim must trace to something in the source. Where you infer rather than read, mark it — "(inferred)" is enough. Never invent an account tier, an order number, a refund amount, a date, or a promise made to the customer; those five are exactly what the receiver will repeat back to the customer verbatim.

When sources conflict — the customer changed their story, two agents recorded different causes — report the conflict rather than silently picking a side. Resolving it may be the receiver's actual job.

## Reference files

- `references/examples.md` — two full worked examples: one clean single-thread refund, one messy case with three agents, a duplicate ticket, and a customer whose story changed. Read this before your first summary; the messy one shows the shape more than any instruction here does.
- `references/edge-cases.md` — the three situations that reliably break the format: nothing tried yet, a ticket spanning two orders, and a thread whose last message is an unanswered customer. Read it when the ticket in front of you resembles one of those.
