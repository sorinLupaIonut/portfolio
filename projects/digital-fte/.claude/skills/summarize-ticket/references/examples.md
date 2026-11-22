# Worked examples

Two cases, one clean and one messy. The clean one shows the shape; the messy one shows the judgment, which is the part that actually matters.

---

## Example 1 — clean single-thread refund

**Source thread (ticket #4471):**

> **Customer (Mar 3, 09:12):** Ordered the standing desk converter on Feb 24 (order SO-10233), it arrived yesterday with a cracked riser arm. Photos attached. I'd like a refund rather than a replacement, I need something that works this week.
>
> **Agent Dana (Mar 3, 10:40):** Confirmed damage from the photos, apologised, offered expedited replacement arriving Mar 6.
>
> **Customer (Mar 3, 11:02):** That's still after my deadline. Refund please.
>
> **Agent Dana (Mar 3, 11:20):** Refund is $189.00, above my $150 limit. Flagged for approval, told the customer they'd hear back within one business day.
>
> *(Account panel: Priya Raman, Pro tier, customer since 2022, 14 orders, no prior complaints.)*

**Handoff note:**

## Customer Context
Priya Raman, Pro tier, customer since 2022, 14 orders and no prior complaints. First issue on the account.

## Issue
Standing desk converter from order SO-10233 arrived Mar 2 with a cracked riser arm (photos in thread). Customer declined a replacement — she needs a working desk this week — and is asking for a refund.

## Resolution Steps Taken
- Mar 3 — Dana confirmed the damage from the customer's photos and offered an expedited replacement for Mar 6. Customer rejected it: still past her deadline.
- Mar 3 — Dana raised the $189.00 refund for approval, as it exceeds her $150 authority, and told the customer to expect an answer within one business day.

## Current Status
Waiting on refund approval. The ball is with us, and the one-business-day promise made on Mar 3 at 11:20 is the clock to watch.

## Recommended Next Action
Approve the $189.00 refund to the original payment method and confirm in-thread today. The damage is documented, the customer is Pro tier with a clean history, and a replacement has already been refused on a deadline we cannot meet.

**Why this note works:** the refused replacement is in Resolution Steps, so nobody offers it again. The $150 authority limit explains *why* this is sitting unresolved rather than making Dana look slow. The promise made to the customer is in Current Status, where it functions as a deadline.

---

## Example 2 — messy: three agents, a duplicate ticket, a changed story

**Source (ticket #4429, merged from #4402):**

> **Customer (Feb 18):** Charged twice for order SO-9981, $64.50 each. Please refund the duplicate.
>
> **Agent Tom (Feb 18):** Checked billing, found only one charge of $64.50. Asked for a bank statement.
>
> **Customer (Feb 19, opens a second ticket #4402):** Nobody is helping me. I've been charged three times now.
>
> **Agent Lee (Feb 19, on #4402):** Apologised for the duplicate ticket, merged it into #4429. Asked again for the statement.
>
> **Customer (Feb 21):** Attached statement. Shows one charge from us ($64.50) and one from "SPCONV*STOREFRONT" ($64.50) on the same day.
>
> **Agent Tom (Feb 22):** Identified the second line as our payment processor's descriptor for a *pre-authorisation hold*, not a second charge. Explained it drops off in 5–7 days. Customer did not reply.
>
> **Agent Nadia (Feb 24, internal note):** Customer called, still upset, says the hold is still showing. Confirmed by phone it's day 6. Told her to wait 24h and call back.
>
> *(Account panel: Marcus Bell, Standard tier, 3 orders, first complaint.)*

**Handoff note:**

## Customer Context
Marcus Bell, Standard tier, 3 orders, first complaint on the account. He has been in contact five times over six days across two tickets (#4402 merged into #4429) and is audibly frustrated by phone.

## Issue
Reported as a duplicate charge on order SO-9981 — first as two charges of $64.50, then escalated by the customer to three. His bank statement shows one charge from us plus one `SPCONV*STOREFRONT` line for the same amount. Tom identified the second line as a pre-authorisation hold from our processor, not a charge (inferred from the descriptor, not confirmed with the processor).

## Resolution Steps Taken
- Feb 18 — Tom checked billing, found a single $64.50 charge, requested a bank statement.
- Feb 19 — Lee merged the customer's second ticket (#4402) into this one and repeated the statement request.
- Feb 22 — Tom explained the pre-auth hold and the 5–7 day drop-off. No customer reply in-thread.
- Feb 24 — Nadia took a phone call: the hold was still visible on day 6. She asked him to wait a further 24 hours and call back.

## Current Status
Ball is with the customer to call back, but the 24-hour window closed on Feb 25 and there has been no contact since. The hold is now past the 5–7 day window Tom quoted, so the explanation given to the customer no longer covers the facts.

## Recommended Next Action
Confirm the hold's status directly with the payment processor before contacting him again — the customer has now been told twice it would clear, and a third "please wait" without evidence is what turns this into an escalation. If the hold is genuinely stuck, refund proactively and tell him so.

**Why this note works:**

- **The customer's story changed** (two charges → three), and the note says so plainly instead of picking the version that fits the diagnosis. The receiver needs to know the reported facts moved.
- **Tom's conclusion is marked as inference.** It is the most likely explanation and also the thing nobody has verified — and the whole case now rests on it.
- **The merged ticket appears in Customer Context**, not as a mystery. The receiver would otherwise trip over #4402 in the history.
- **The deadline that quietly expired is the headline of Current Status.** "Waiting on the customer" would have been technically true and completely misleading.
- **The recommendation names the real risk**: this is one bad reply away from an escalation, and the note says why.
