# Reading the distance numbers

`find_similar_resolved_tickets` ranks by **cosine distance** (`<=>` in pgvector). Lower is closer. `0` is identical direction, `1` is unrelated, `2` is opposite. You will essentially never see the top of that range in this corpus — real support text clusters low, which is exactly why an absolute number needs interpreting rather than reading as a percentage.

## Working bands

| Distance | What it usually means | What to do |
|---|---|---|
| < 0.15 | Same issue, near-identical wording | Treat the prior resolution as the default answer |
| 0.15 – 0.30 | Same class of issue, different specifics | Useful precedent; check the differences before reusing the resolution |
| 0.30 – 0.45 | Same domain, different problem | Context at best. Say "no strong prior precedent found" |
| > 0.45 | Unrelated | Do not show it as precedent at all |

## Why 0.3 is a heuristic, not a constant

The cut-off is calibrated against the seeded corpus of resolved tickets, and it moves as the corpus grows. A small library returns higher distances simply because the right ticket may not exist yet; a large one returns lower distances for everything, including bad matches, because there is more text to be coincidentally near.

So re-measure rather than inherit the number. Take a dozen searches whose correct answer you already know, look at where the genuine matches land versus the noise, and move the band. If every search comes back under 0.2, the threshold has gone slack and is no longer filtering anything.

## The failure this prevents

A model shown `0.42` with no frame reads it as "fairly close" — it is a small number, and the ticket it points at will be about roughly the right subject. That match then gets quoted to a customer as "we've seen this before," which is how a support team ends up confidently applying a shipping fix to a billing problem.

The bands exist so the number becomes a decision instead of an impression.

## One thing that silently breaks all of this

Distances are only meaningful if the query was embedded with the **same model** used to store the library — `text-embedding-3-small`, 1536 dimensions. A mismatch does not raise an error. It returns confident, plausible, wrong rankings, and the distances still look like ordinary numbers. If results seem subtly nonsensical — right domain, wrong tickets, no clear ordering — suspect this before suspecting the threshold.
