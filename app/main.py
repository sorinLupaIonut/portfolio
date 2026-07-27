"""The function host: a FastAPI app that serves this project's Inngest functions.

Run it with:
    uv run uvicorn app.main:app --port 8000 --reload

The Inngest dev server (npx inngest-cli@latest dev, on :8288) auto-discovers
this host at /api/inngest. Run exactly one function host per dev server.
"""

from fastapi import FastAPI

import inngest.fast_api

from .client import inngest_client
from .functions import (
    daily_customer_health_check,
    handle_customer_email,
    run_customer_health_check,
)

FUNCTIONS = [
    handle_customer_email,
    daily_customer_health_check,
    run_customer_health_check,
]

app = FastAPI(title="AI Agent Nervous System - function host")


@app.get("/")
async def health() -> dict[str, object]:
    return {"status": "ok", "functions": [fn.id for fn in FUNCTIONS]}


inngest.fast_api.serve(app, inngest_client, FUNCTIONS)
