"""server.py — the one gateway (Concept 4 scaffold).

A single FastMCP app, one public URL, streamable HTTP, bound to 127.0.0.1:8000. This is the
whole product's front door: every tool the model calls arrives here. Invariant 1 (one gateway)
lives in this file — we never split into more than one server.

Tools are grouped only by an underscore name-prefix, because MCP tool names allow letters,
digits, `_` and `-` but never a dot. The prefix *is* the namespace:

    config_*  — liveness / rules (config_health is deliberately NOT session-gated)
    domain_*  — the reading library (domain_get_item)
    user_*    — per-person memory (added at Concept 10, behind the session gate)

At this stage there is NO auth, NO database, and NO session gate yet — those arrive at
Concepts 8 and 10. Right now the point is only that a local client can connect and list the
tools. `domain_get_item` reads real articles from `seed/articles.json`; the `session` gate
(invariant 4) is still to come at Concept 10.

Run it:  uv run python -m connector_app.server   (serves http://127.0.0.1:8000/mcp)
"""

import json
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

mcp: FastMCP = FastMCP("reading-room")

# The seed library: static, read-only reference content shipped with the app (not user data).
# Load it once at import and index by id so lookups are a dict hit. server.py lives at
# src/connector_app/server.py, so parents[2] is the project root that holds seed/.
_SEED_PATH = Path(__file__).resolve().parents[2] / "seed" / "articles.json"
_ARTICLES: dict[str, dict[str, Any]] = {
    article["id"]: article for article in json.loads(_SEED_PATH.read_text(encoding="utf-8"))
}


@mcp.tool(
    annotations=ToolAnnotations(
        title="Health check",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def config_health() -> dict[str, Any]:
    """Liveness probe. Returns the server status without needing a session.

    Deliberately ungated (invariant 4): you can prove the gateway is up before any session
    exists, which is what fail-closed diagnostics need.
    """
    return {"status": "ok", "service": "reading-room"}


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get article by id",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def domain_get_item(id: str) -> dict[str, Any]:
    """Fetch one library article by its id from the seed library.

    Returns the article's id, title, topic and body. An unknown id raises rather than
    inventing an article: fail closed (invariant 4) — the model never gets fabricated content.

    The `session` gate (invariant 4) is added at Concept 10; today this is ungated.
    """
    article = _ARTICLES.get(id)
    if article is None:
        raise ToolError(f"no article with id {id!r}")
    return article


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8000, stateless_http=True)
