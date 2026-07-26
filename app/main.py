"""The function host: a FastAPI app that serves this project's Inngest functions.

Run it with:
    uv run uvicorn app.main:app --port 8000 --reload

The Inngest dev server (npx inngest-cli@latest dev, on :8288) auto-discovers
this host at /api/inngest. Run exactly one function host per dev server.
"""

from fastapi import FastAPI

import inngest.fast_api

from .client import inngest_client
from .functions import greet_customer

app = FastAPI(title="AI Agent Nervous System - function host")


@app.get("/")
async def health() -> dict[str, object]:
    return {"status": "ok", "functions": ["greet-customer"]}


inngest.fast_api.serve(app, inngest_client, [greet_customer])
