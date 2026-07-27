"""Open one run and show what each step actually did.

    uv run python -m scripts.run_trace <run_id> [<run_id> ...]

The diagnostic for durability questions, and it is deliberately per-run. Counting
audit rows per customer tells you nothing: two runs for the same customer are
supposed to write two ingress rows. What must never happen is one step running
twice inside one run, and the only place that shows up is a single run's trace.

So the number to read is the attempt count on each step. When a later step fails
and Inngest retries it, an earlier step that already succeeded must stay at one
attempt: its result comes back from memo instead of being computed again. An
ingress step at two attempts in one run means the memo missed, and the usual
cause is a step name that is not unique.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from app.worker import db
from scripts.burst_emails import DEV_SERVER, _gql, _parse_time

QUERY = """
query($runID: String!) {
  run(runID: $runID) { status queuedAt startedAt endedAt }
  runTrace(runID: $runID) {
    childrenSpans { name stepOp status attempts startedAt endedAt }
  }
}
"""


async def describe(client: httpx.AsyncClient, run_id: str) -> None:
    data = await _gql(client, QUERY, runID=run_id)
    run = data["run"]

    started = _parse_time(run["startedAt"]) if run["startedAt"] else None
    ended = _parse_time(run["endedAt"]) if run["endedAt"] else None
    took = f"{(ended - started).total_seconds():.1f}s" if started and ended else "-"
    print(f"\nrun {run_id}   {run['status']}   {took}")

    # The tracer emits every span twice, milliseconds apart. Merge by step name
    # and keep the highest attempt count, so a retried step reads as one row.
    merged: dict[str, dict] = {}
    for span in data["runTrace"]["childrenSpans"] or []:
        if not span["stepOp"]:
            continue  # executor bookkeeping, not a step of ours
        prior = merged.get(span["name"])
        if prior is None or span["attempts"] > prior["attempts"]:
            merged[span["name"]] = span

    print(f"  {'step':<28}{'op':<17}{'status':<12}{'attempts':<10}took")
    for span in merged.values():
        start = _parse_time(span["startedAt"]) if span["startedAt"] else None
        stop = _parse_time(span["endedAt"]) if span["endedAt"] else None
        elapsed = f"{(stop - start).total_seconds():.1f}s" if start and stop else "-"
        # attempts is zero-based in the dev server: a step that succeeded first
        # try reports 0. Shown as a human count so "1" means "ran once".
        print(
            f"  {span['name']:<28}{span['stepOp']:<17}{span['status']:<12}"
            f"{span['attempts'] + 1:<10}{elapsed}"
        )

    rows = await db.recent_audit(run_id=run_id)
    print(f"  audit_log rows written by this run: {len(rows)}")
    for row in rows:
        print(f"    {row['created_at']:%H:%M:%S}  {row['action']:<18} {row['details']}")


async def main(run_ids: list[str]) -> None:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for run_id in run_ids:
                await describe(client, run_id)
    finally:
        db.close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_ids", nargs="+", help="Inngest run ids (ULIDs)")
    args = parser.parse_args()
    if not DEV_SERVER:
        sys.exit("no dev server url")
    asyncio.run(main(args.run_ids))
