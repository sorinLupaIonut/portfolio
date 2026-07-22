# Routing: issue class → specialist team

These are the placeholder mappings for this build. **Replace them with your organisation's real teams and targets before this Worker touches a live queue** — routing is the part of an escalation that changes most often, and a wrong team costs the customer a full handoff cycle. That churn is exactly why this lives in a reference file rather than in the skill body.

| Issue class | Team | Target first response |
|---|---|---|
| Refunds, chargebacks, invoice disputes, proration | Billing Specialists | 4 business hours |
| Payment failures, card declines, processor holds | Billing Specialists | 4 business hours |
| Lost, damaged, or undelivered shipments | Logistics | 1 business day |
| Wrong item, missing contents, warehouse errors | Logistics | 1 business day |
| API errors, integration failures, webhooks | Technical Support (tier 2) | 4 business hours |
| Account access, login, SSO, permissions | Identity & Access | 2 business hours |
| Suspected fraud or compromised account | Trust & Safety | 1 hour |
| Data deletion, privacy, or legal requests | Compliance | 1 business day |

## Choosing when a case spans classes

Route on the **action the receiving team must take**, not on what the customer talked about most.

A customer whose package never arrived and who now wants their money back is a *refund decision* — Billing owns it, even though the story is entirely about shipping. Logistics cannot approve the refund, so routing there buys one more handoff and one more day.

If two teams genuinely both need to act, route to the one that must act **first** and say in the note that the other is also required. Do not split an escalation across two teams; each will assume the other has it.

## Target response times are targets

They set the expectation inside the note, for the receiving agent. They are not promises to make to the customer unless someone has confirmed the team can meet them today. A missed commitment made on tier 2's behalf lands on the same customer who was already escalated once.

## Trust & Safety is different

Suspected fraud or account compromise routes immediately and does not wait for the other conditions to be evaluated. Do not continue troubleshooting the surface problem while a possible compromise sits unrouted — and do not tell the customer what you suspect, since the account may not be under their control.
