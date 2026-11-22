"""The shared Inngest client. Import this everywhere; construct it once."""

import logging
import os
from pathlib import Path

import inngest
from dotenv import load_dotenv

# Load .env BEFORE constructing the client: the SDK reads INNGEST_DEV at
# construction time. Without it the SDK silently defaults to Cloud mode, asks
# for a signing key, and never registers with the local dev server.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

logger = logging.getLogger("uvicorn")

if not os.getenv("INNGEST_DEV"):
    logger.warning(
        "INNGEST_DEV is not set - the SDK will start in Cloud mode and will "
        "not register with the local dev server on :8288."
    )

inngest_client = inngest.Inngest(
    app_id="ai-agent-nervous-system",
    logger=logger,
)
