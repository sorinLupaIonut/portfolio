"""server.py — the one gateway (Concept 4 scaffold).

A single FastMCP app, one public URL, streamable HTTP, bound to 127.0.0.1:8000. This is the
whole product's front door: every tool the model calls arrives here. Invariant 1 (one gateway)
lives in this file — we never split into more than one server.

Tools are grouped only by an underscore name-prefix, because MCP tool names allow letters,
digits, `_` and `-` but never a dot. The prefix *is* the namespace:

    config_*  — liveness / rules (config_health is deliberately NOT session-gated)
    domain_*  — the reading library (domain_get_item), behind the session gate
    user_*    — per-person memory (the reader's shelf), behind the session gate

OAuth (Concept 8) is wired around the given `auth.py`. This server is an OAuth 2.1 *resource
server* only: a `RemoteAuthProvider` gates the MCP endpoint (an unauthenticated call gets HTTP 401)
and publishes `/.well-known/oauth-protected-resource` (RFC 9728). The token check is NOT
re-implemented here — the verifier delegates to `auth.verified_claims`, so auth.py's four checks
(signature, issuer, audience, expiry) are exactly what accept or reject a token.

The session contract (Concept 10) sits on top. `begin_session` is the front door: it reads the
verified `sub` (from the OAuth token, or `DEV_SUB` under AUTH_DISABLED), ensures the reader's row,
and hands back the librarian's rules + persona (from `config_store`), the reader's shelf (from
`db`), and a fresh signed session token (from the given `session.py`). Every `domain_*`/`user_*`
tool then requires that session token as its first argument — `require_session` at the top of the
body, before any work — so the model is *structurally* forced to call `begin_session` first, and a
call with no session fails closed (invariant 4). Identity is only ever the verified `sub`; no tool
reads a `user_id` argument (invariant 3). Each real tool signs off with a one-line reminder to
present its result in the librarian's voice, so the persona holds over a long conversation.

The one escape hatch is `AUTH_DISABLED=1` (the Part 5 live demo, and only then): the auth provider
is dropped entirely so the gateway is reachable over a tunnel with no sign-in. Never default it on.

Run it:  uv run python -m connector_app.server   (serves http://127.0.0.1:8000/mcp)
"""

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import anyio
import psycopg
from dotenv import load_dotenv

# Load .env BEFORE importing auth.py — it reads AUTH_ISSUER / AUTH_JWKS_URL / RESOURCE_URL at import.
load_dotenv()

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.auth import TokenVerifier
from fastmcp.server.dependencies import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl

from connector_app import config_store, db

# Import the session helpers by name so a tool's `session: str` argument does not shadow the module.
from connector_app.session import SessionError, new_session_token, require_session

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


# --- identity + the session gate (Concept 10) --------------------------------------------------
def _verified_sub() -> str:
    """The one trusted identity for begin_session: the verified OAuth `sub`.

    With auth on, the RemoteAuthProvider has already verified the token and sealed the `sub` into
    the AccessToken; we just read it back — no second decode. Under AUTH_DISABLED=1 (Part 5 only)
    there is no token, so we fall back to the fixed DEV_SUB. This is the ONLY place the auth-off
    switch changes who the reader is; never default AUTH_DISABLED on.
    """
    if AUTH_DISABLED:
        return os.environ["DEV_SUB"]
    token = get_access_token()
    if token is None:  # the provider gates /mcp, so this should be unreachable — fail closed anyway
        raise ToolError("not authenticated")
    return token.claims["sub"]


def _gate(session: str) -> str:
    """Require a valid session token and return the sub it carries, or fail closed.

    This is the structural gate (invariant 4): every domain_*/user_* tool calls it first, before
    any work, so a call with no (or a bad) session is refused instead of served. The `sub` comes
    from the session token that begin_session minted from the verified OAuth `sub` — never from a
    tool argument the model controls (invariant 3).
    """
    try:
        return require_session(session)
    except SessionError as e:
        raise ToolError(str(e)) from e


def _signed(payload: dict[str, Any]) -> dict[str, Any]:
    """Append the librarian's one-line 'present it in my voice' sign-off to a tool result."""
    return {**payload, "present": config_store.PRESENT_REMINDER}


@contextmanager
def _store_guard() -> Iterator[None]:
    """Turn a store outage into an honest, clean refusal (invariant 4).

    If the database is unreachable (`psycopg.Error`) or unconfigured (`db` raises `RuntimeError`),
    the wrapped tool re-raises as a `ToolError` the model is told to relay verbatim: the session
    can't continue. Crucially it does NOT fall back to an empty shelf — a store that is *down* must
    never look like a reader who simply has *nothing saved*. We refuse; we never invent state.
    """
    try:
        yield
    except (psycopg.Error, RuntimeError) as e:
        raise ToolError(
            "the reading room can't reach your shelf right now, so this session can't continue"
        ) from e


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
        title="Begin a reading-room session",
        readOnlyHint=False,
        openWorldHint=False,
    )
)
def begin_session() -> dict[str, Any]:
    """Check the reader in. Call this first — every other reading-room tool needs its token.

    Reads the reader's identity from the verified OAuth `sub` (never from an argument), makes sure
    they have a row, and hands back everything the model needs for the visit: the librarian's
    cooperative rules and persona, the reader's own shelf, and a fresh signed session token to pass
    to every domain_*/user_* call. The rules include the fail-closed instruction (invariant 4).
    """
    sub = _verified_sub()
    with _store_guard():
        db.ensure_user(sub)
        state = db.read_state(sub)
    return {
        "rules": config_store.rules(),
        "persona": config_store.persona(),
        "state": state,
        "session": new_session_token(sub),
    }


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get article by id",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def domain_get_item(session: str, id: str) -> dict[str, Any]:
    """Fetch one library article by its id from the seed library. Requires a session token.

    Refuses without a valid session (invariant 4) — the gate runs before the lookup. An unknown id
    raises rather than inventing an article: the model never gets fabricated content.
    """
    _gate(session)
    article = _ARTICLES.get(id)
    if article is None:
        raise ToolError(f"no article with id {id!r}")
    return _signed(article)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Read the reader's shelf",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def user_get_state(session: str) -> dict[str, Any]:
    """Return this reader's shelf: reading list, finished articles, and notes. Requires a session.

    Identity is the `sub` inside the session token, never an argument (invariant 3).
    """
    sub = _gate(session)
    with _store_guard():
        return _signed({"state": db.read_state(sub)})


@mcp.tool(
    annotations=ToolAnnotations(
        title="Save an article to the reading list",
        readOnlyHint=False,
        openWorldHint=False,
    )
)
def user_save_article(session: str, article_id: str) -> dict[str, Any]:
    """Add a library article to this reader's reading list. Requires a session.

    Validates the article exists in the library before saving (fail closed — no phantom ids). The
    reader is the `sub` in the session token; the write is a read-modify-write of their one state doc.
    """
    sub = _gate(session)
    if article_id not in _ARTICLES:
        raise ToolError(f"no article with id {article_id!r}")
    with _store_guard():
        state = db.read_state(sub)
        if article_id not in state["reading_list"]:
            state["reading_list"].append(article_id)
        return _signed({"state": db.save_state(sub, state)})


@mcp.tool(
    annotations=ToolAnnotations(
        title="Mark an article finished",
        readOnlyHint=False,
        openWorldHint=False,
    )
)
def user_mark_read(session: str, article_id: str) -> dict[str, Any]:
    """Mark a library article as finished for this reader. Requires a session.

    Validates the article exists first. Identity is the session `sub`, never an argument.
    """
    sub = _gate(session)
    if article_id not in _ARTICLES:
        raise ToolError(f"no article with id {article_id!r}")
    with _store_guard():
        state = db.read_state(sub)
        if article_id not in state["finished"]:
            state["finished"].append(article_id)
        return _signed({"state": db.save_state(sub, state)})


@mcp.tool(
    annotations=ToolAnnotations(
        title="Set a note on an article",
        readOnlyHint=False,
        openWorldHint=False,
    )
)
def user_set_note(session: str, article_id: str, note: str) -> dict[str, Any]:
    """Attach (or replace) this reader's note on a library article. Requires a session.

    Validates the article exists first. The note is stored under the reader's `sub`, never an id
    the model named (invariant 3).
    """
    sub = _gate(session)
    if article_id not in _ARTICLES:
        raise ToolError(f"no article with id {article_id!r}")
    with _store_guard():
        state = db.read_state(sub)
        state["notes"][article_id] = note
        return _signed({"state": db.save_state(sub, state)})


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8000, stateless_http=True)
