"""server.py — the one gateway (Concept 4 scaffold).

A single FastMCP app, one public URL, streamable HTTP, bound to 127.0.0.1:8000. This is the
whole product's front door: every tool the model calls arrives here. Invariant 1 (one gateway)
lives in this file — we never split into more than one server.

Tools are grouped only by an underscore name-prefix, because MCP tool names allow letters,
digits, `_` and `-` but never a dot. The prefix *is* the namespace:

    config_*  — liveness / rules (config_health is deliberately NOT session-gated)
    domain_*  — the reading library (domain_get_item)
    user_*    — per-person memory (added at Concept 10, behind the session gate)

OAuth (Concept 8) is now wired around the given `auth.py`. This server is an OAuth 2.1
*resource server* only: a `RemoteAuthProvider` gates the MCP endpoint (an unauthenticated call
gets HTTP 401) and publishes `/.well-known/oauth-protected-resource` (RFC 9728). The actual token
check is NOT re-implemented here — the provider's token verifier delegates to `auth.verified_claims`,
so auth.py's four checks (signature, issuer, audience, expiry) are exactly what accept or reject a
token. We wire auth.py; we never duplicate it. The database and the session gate still arrive at
Concept 10. `domain_get_item` reads real articles from `seed/articles.json`.

The one escape hatch is `AUTH_DISABLED=1` (the Part 5 live demo, and only then): the auth provider
is dropped entirely so the gateway is reachable over a tunnel with no sign-in. Never default it on.

Run it:  uv run python -m connector_app.server   (serves http://127.0.0.1:8000/mcp)
"""

import json
import os
from pathlib import Path
from typing import Any

import anyio
from dotenv import load_dotenv

# Load .env BEFORE importing auth.py — it reads AUTH_ISSUER / AUTH_JWKS_URL / RESOURCE_URL at import.
load_dotenv()

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.auth import TokenVerifier
from mcp.server.auth.provider import AccessToken
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl

# --- OAuth resource-server wiring (Concept 8) --------------------------------------------------
# AUTH_DISABLED=1 is the Part 5 live-demo switch only: drop the auth layer so the gateway is open
# over a tunnel. Everywhere else the RemoteAuthProvider below enforces a verified token.
AUTH_DISABLED = os.getenv("AUTH_DISABLED", "0") == "1"


def _build_auth_provider() -> RemoteAuthProvider | None:
    """Build the OAuth resource-server provider, or None when AUTH_DISABLED=1.

    The provider produces the 401 and serves the RFC 9728 discovery document. Token validation is
    delegated to the given `auth.py` (never re-implemented): `verify_token` returns None on any
    AuthError, which FastMCP turns into HTTP 401.
    """
    if AUTH_DISABLED:
        return None

    from connector_app import auth  # imported here so AUTH_DISABLED=1 needs no AUTH_* env set

    class AuthPyTokenVerifier(TokenVerifier):
        """Delegates the four checks to auth.verified_claims — auth.py stays the single source."""

        async def verify_token(self, token: str) -> AccessToken | None:
            try:
                # auth.verified_claims does a blocking JWKS fetch; keep the event loop free.
                claims = await anyio.to_thread.run_sync(auth.verified_claims, token)
            except auth.AuthError:
                return None  # any of the four checks failing ⇒ 401
            sub = claims["sub"]  # the one safe identity, straight from the verified token
            return AccessToken(
                token=token,
                client_id=sub,
                scopes=[],
                subject=sub,
                claims=claims,
            )

    return RemoteAuthProvider(
        token_verifier=AuthPyTokenVerifier(),
        authorization_servers=[AnyHttpUrl(auth.AUTH_ISSUER)],
        base_url=auth.RESOURCE_URL,  # this server's canonical URL = the token audience (RFC 8707)
    )


mcp: FastMCP = FastMCP("reading-room", auth=_build_auth_provider())

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
