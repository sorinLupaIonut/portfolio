# Edge cases

Three situations that reliably break the five-section format. Each one tempts you to fill a section with something that isn't true.

---

## 1. Nothing has been tried yet

A ticket that arrived twenty minutes ago and has had no agent response has an empty Resolution Steps Taken. The temptation is to pad it — "ticket created", "auto-acknowledgement sent", "assigned to queue" — because an empty section feels like a failure of the summary.

It isn't. Write:

## Resolution Steps Taken
None yet — ticket opened Mar 12, 14:05, no agent response so far.

Include the age, because on an untouched ticket that is the single most important fact in the note. Twenty minutes old and untouched is normal; three days old and untouched is the reason someone asked for this handoff.

The same discipline applies to Current Status: "Unassigned, awaiting first response" is a real status. Do not upgrade it to "under investigation."

---

## 2. The ticket spans two orders

A customer writes about a late delivery and, four messages in, mentions that last month's order was also wrong. Now there are two order numbers in one thread and the format has one Issue section.

Decide which is the ticket's actual subject — usually the one the customer opened it about — and lead with it. Then name the second explicitly rather than blending them:

## Issue
Order SO-10412 (ordered Mar 1) has not arrived; tracking has shown "in transit" since Mar 4. **Separately**, the customer mentions that order SO-10188 arrived with the wrong colour last month and was never resolved — no ticket appears to exist for it.

Two reasons this matters. Blending them produces a note where the receiver cannot tell which order the refund applies to, and that mistake is expensive. And a second unresolved issue surfacing mid-thread is often the real reason the customer's tone has changed — which belongs in Customer Context, not buried in Issue.

If the second issue needs its own ticket, say so in Recommended Next Action. That is a concrete action, and it is frequently the right one.

---

## 3. The last message is from the customer, unanswered

The thread ends with the customer asking something and silence after it. This is the case where Current Status is most often written wrong, because the ticket's own status field usually still says "pending" or "in progress" — describing our workflow, not reality.

Lead with the silence and quantify it:

## Current Status
Customer replied Mar 9 asking whether the replacement had shipped. No response from us in 3 days. Ticket is still marked "in progress"; the ball is with us.

Quote or paraphrase the unanswered question, because it is what the receiver has to answer first, and it is the thing the customer will lead with when they next make contact.

Where the delay is long enough to have become its own problem — several days on a case the customer already found frustrating — say that in Recommended Next Action. Acknowledging the delay before answering the question is usually the first move, and a receiver working from the note alone will not know the delay happened unless it is written down.
