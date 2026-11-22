---
name: find-similar-cases
description: Searches the resolved-tickets library for tickets semantically similar to a customer's described issue, returning the top 3-5 with their resolutions, ranked by how closely each matches. Use when the user describes a problem, complaint, or symptom and you need to check whether the team has handled something similar before. Calls the find_similar_resolved_tickets MCP tool. Always run this BEFORE drafting a response, so the response can reference proven prior resolutions rather than inventing a new approach.
---

# Find similar cases

Check the resolved-tickets library before answering a customer, so the answer repeats something that already worked instead of inventing a fresh approach that has never been tried on a real customer.

## Steps

**1. Extract the issue description.** Pull the symptom out of the conversation in the customer's own terms — what they observe, not what you suspect is causing it. Search matches against how past tickets were *described*, so "package arrived empty, just packing material" retrieves far better than "suspected warehouse pick error." If several problems are in play, search the one you are about to answer.

**2. Call `find_similar_resolved_tickets(description, limit=5)`.** This MCP tool is the only way this skill reaches the library. Never open a database connection or write SQL — the running Worker reaches business data through the `customer-data` server and nothing else, and a skill that quietly bypasses that boundary makes the audit trail a lie.

Ask for 5 even though you will show 3. The two you drop are how you tell a strong match from the best of a weak field.

**3. Present the top three in a table**, closest first:

| # | Ticket | Issue | Resolution | Distance |
|---|--------|-------|------------|----------|
| 1 | #3812 | Package delivered empty, seal intact | Full refund + warehouse audit opened | 0.11 |

**4. Say plainly when nothing is close.** Distances above roughly 0.3 are not precedent. Write "No strong prior precedent found" and show the nearest matches as context rather than as an answer. See `references/distance-bands.md` for what the numbers mean and why 0.3 is a starting heuristic rather than a constant.

This step is the one that gets skipped under pressure. A weak match presented as precedent is worse than an empty result: it borrows the authority of "we've handled this before" for a resolution nobody has validated on this problem.

## If the tool is not available

Say so and stop. Do not answer from memory of what support teams usually do, and do not present a plausible-sounding resolution as though it came from the library. The whole value of this skill is that its answers are traceable to tickets that actually closed.
