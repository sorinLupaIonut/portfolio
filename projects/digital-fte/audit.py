"""The audit ledger: every meaningful thing the Worker does, on its own pool.

Two tables, written from two places:

  audit_log              what happened, in the canonical action vocabulary
  capability_invocations one row per skill or MCP tool call, with outcome and latency

The pool here is deliberately separate from the customer-data MCP server's pool.
The audit subsystem must not reach the database through the boundary it audits,
or a compromised boundary can edit its own record.

A note on transactions, because it looks like a violation and is not. AGENTS.md
requires a state-changing action and its audit row to commit together. For the
MCP tools that rule is honoured *inside* the server: issue_refund writes the
refund, the order update and its `refund_issued` row in one transaction. What
this module writes is a different, weaker claim -- "the Worker called this tool"
-- observed from outside a process boundary it cannot join a transaction across.
Both rows are correct and both belong. Do not "fix" one into the other.
"""

import asyncio
import json
import os
import sys
import time
from typing import Any

import asyncpg

from agents import RunHooks

# audit_log.action is CHECK-constrained to exactly this vocabulary; a typo here
# fails the insert rather than writing a row nobody will ever query for.
ACTION_MESSAGE_RECEIVED = "message_received"
ACTION_MESSAGE_SENT = "message_sent"
ACTION_SKILL_ACTIVATED = "skill_activated"
ACTION_CAPABILITY_INVOKED = "capability_invoked"
ACTION_GUARDRAIL_TRIPPED = "guardrail_tripped"
# Written by the Worker when a human rejects an approval-gated tool at the
# terminal prompt, so the refusal is a row and not just an absence. The MCP
# server writes its own 'refund_blocked' for an internal guard refusal; the
# actor column ('worker' vs 'customer-data-mcp') tells the two apart.
ACTION_REFUND_BLOCKED = "refund_blocked"

# The Worker's own rows. The MCP server writes its rows as 'customer-data-mcp',
# so the replay query can tell "the Worker asked" from "the server did".
ACTOR = "worker"

# The tool the Skills capability exposes; calling it is what "activating a skill"
# means in lazy mode. There is no skill hook to use instead.
LOAD_SKILL_TOOL = "load_skill"

# Tool results carry whole ticket bodies. Cap them, and say so in the row, so a
# short payload is never mistaken for a small result.
RESULT_LIMIT = 2000

# Two different layers produce these, and only measurement found the second one.
# FastMCP formats a server-side ToolError as "Error executing tool <name>: ...";
# the SDK's own default_tool_error_function produces the other when the call
# fails before the server answers. Substring, not startswith: the text arrives
# nested inside a content block, not at position 0.
ERROR_MARKERS = (
    "Error executing tool",
    "An error occurred while running the tool",
)


def _coerce(result: object) -> str:
    """Turn whatever on_tool_end handed us into text.

    The hook annotates `result` as `str` and does not deliver one: measured, it
    is a dict for load_skill, a list of content blocks for an MCP tool, and a
    dict for a failed one. Slicing or calling .startswith on those raises, and an
    exception in a hook aborts the user's turn -- so everything goes through here.
    """
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str)
    except (TypeError, ValueError):
        return str(result)


def _payload(value: object) -> str:
    """A jsonb-ready string, capped, with truncation made visible.

    Two shapes, deliberately. A dict or list that serialises and fits is stored
    as itself, so Decision 8's replay can read `payload->>'message'` directly
    rather than parsing JSON nested inside a JSON string. Anything else -- too
    large, or not serialisable -- falls back to a {text, truncated} envelope.
    Callers that want the first shape should pass a dict.
    """
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    else:
        if isinstance(value, (dict, list)) and len(text) <= RESULT_LIMIT:
            return text
    return json.dumps({"text": text[:RESULT_LIMIT], "truncated": len(text) > RESULT_LIMIT})


def _arguments_json(arguments: object) -> Any:
    """Tool arguments arrive as a JSON *string*; give jsonb the object.

    Stored raw they would nest as an escaped string inside the payload, so
    reading one argument back means parsing JSON out of JSON.
    """
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except (TypeError, ValueError):
            return arguments
    return arguments if arguments is not None else {}


def _status_for(result_text: str) -> str:
    """'ok' or 'error', per capability_invocations' CHECK constraint.

    A guard refusal from issue_refund also lands here as 'error'. That is
    deliberate: from outside the server a deliberate refusal and a server bug
    look identical, and guessing between them by matching refusal wording would
    break the moment that wording changes. The authoritative distinction is the
    server's own `refund_blocked` row, written inside the transaction.
    """
    return "error" if any(marker in result_text for marker in ERROR_MARKERS) else "ok"


class AuditLog:
    """The audit pool and its writers."""

    def __init__(self, database_url: str, session_id: str) -> None:
        self._database_url = database_url
        self.session_id = session_id
        self._pool: asyncpg.Pool | None = None
        self.failures = 0

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._database_url,
            # Neon's pooled endpoint is PgBouncer in transaction mode: it hands one
            # server session to different clients, so a cached prepared statement
            # can arrive on a connection that never prepared it. Without this the
            # very first query errors.
            statement_cache_size=0,
            min_size=1,
            max_size=3,
            # asyncpg has no pre-ping, so the defence is to retire a connection
            # before the pooler does rather than to test it on the way out. The
            # default is 300s, which is longer than a customer takes to reply --
            # exactly the window in which a dead connection gets handed back.
            max_inactive_connection_lifetime=60,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def __aenter__(self) -> "AuditLog":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def ensure_conversation(self, user_id: str, metadata: dict[str, Any]) -> None:
        """Create the conversations row this session's audit rows hang off.

        Not optional. capability_invocations.conversation_id is NOT NULL with a
        foreign key to conversations(session_id), so without this row every
        capability insert fails. SQLAlchemySession does not write it -- it owns
        agent_sessions/agent_messages, which are its own tables.
        """
        assert self._pool is not None
        await self._pool.execute(
            """
            INSERT INTO conversations (session_id, user_id, metadata)
            VALUES ($1, $2, $3::jsonb)
            ON CONFLICT (session_id) DO NOTHING
            """,
            self.session_id,
            user_id,
            json.dumps(metadata),
        )

    async def close_conversation(self) -> None:
        """Close the row and roll the trace up into it.

        Derived from audit_log rather than counted in memory: the ledger is
        already the record, so a rollup computed from it cannot disagree with
        what was audited. No model call -- this is a factual summary, not a
        written one.

        What deliberately does NOT go here: the turns themselves. SQLAlchemySession
        owns those in agent_messages, and copying them would create a second store
        that drifts. This row holds business metadata and the closing summary.
        """
        assert self._pool is not None
        stats = await self._pool.fetchrow(
            """
            WITH t AS (SELECT * FROM audit_log WHERE conversation_id = $1)
            SELECT
              (SELECT payload->>'message' FROM t
                 WHERE action = 'message_received' ORDER BY id LIMIT 1) AS opened_with,
              (SELECT result->>'message' FROM t
                 WHERE action = 'message_sent' ORDER BY id DESC LIMIT 1) AS last_reply,
              count(*) FILTER (WHERE action = 'message_received') AS turns,
              count(*) FILTER (WHERE action = 'message_sent') AS replies,
              count(*) FILTER (WHERE action = 'guardrail_tripped') AS guardrail_trips,
              coalesce(array_agg(DISTINCT payload->'arguments'->>'skill_name')
                         FILTER (WHERE action = 'skill_activated'), '{}') AS skills,
              coalesce(array_agg(DISTINCT target)
                         FILTER (WHERE action = 'capability_invoked'), '{}') AS tools
            FROM t
            """,
            self.session_id,
        )

        turns = stats["turns"] or 0
        opened = (stats["opened_with"] or "").strip()
        parts = [f"{turns} turn(s), {stats['replies'] or 0} reply(ies)."]
        if opened:
            snippet = opened if len(opened) <= 160 else opened[:157] + "..."
            parts.append(f'Opened with: "{snippet}"')
        if stats["skills"]:
            parts.append(f"Skills: {', '.join(sorted(stats['skills']))}.")
        if stats["tools"]:
            parts.append(f"Tools: {', '.join(sorted(stats['tools']))}.")
        if stats["guardrail_trips"]:
            parts.append(f"Guardrail tripped {stats['guardrail_trips']}x.")
        if not turns:
            parts = ["No turns; the session opened and closed without a message."]

        await self._pool.execute(
            """
            UPDATE conversations
               SET ended_at = coalesce(ended_at, now()),
                   summary  = $2,
                   metadata = metadata || $3::jsonb
             WHERE session_id = $1
            """,
            self.session_id,
            " ".join(parts),
            json.dumps(
                {
                    "turns": turns,
                    "replies": stats["replies"] or 0,
                    "skills_loaded": sorted(stats["skills"] or []),
                    "tools_called": sorted(stats["tools"] or []),
                    "guardrail_trips": stats["guardrail_trips"] or 0,
                    "last_reply": (stats["last_reply"] or "")[:500] or None,
                }
            ),
        )

    async def record_action(
        self,
        action: str,
        *,
        target: str | None = None,
        payload: object = None,
        result: object = None,
    ) -> None:
        assert self._pool is not None
        await self._pool.execute(
            """
            INSERT INTO audit_log (conversation_id, actor, action, target, payload, result)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
            """,
            self.session_id,
            ACTOR,
            action,
            target,
            _payload(payload if payload is not None else {}),
            _payload(result) if result is not None else None,
        )

    async def record_capability(
        self,
        capability: str,
        *,
        arguments: object,
        result: object,
        status: str,
        latency_ms: int | None,
    ) -> None:
        assert self._pool is not None
        await self._pool.execute(
            """
            INSERT INTO capability_invocations
                (conversation_id, capability, arguments, result, status, latency_ms)
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6)
            """,
            self.session_id,
            capability,
            _payload(arguments),
            _payload(result),
            status,
            latency_ms,
        )

    def warn(self, where: str, error: Exception) -> None:
        """An audit write failed. Say so loudly; never take the turn down with it.

        Unless AUDIT_STRICT is set, which re-raises instead. Swallowing is right
        in production -- a customer's turn should survive a failed insert -- but
        it is exactly wrong under a debugger, where a swallowed exception is a
        stderr line that scrolls past instead of a breakpoint that stops.
        """
        self.failures += 1
        print(f"AUDIT FAILED [{where}]: {type(error).__name__}: {error}", file=sys.stderr)
        if os.getenv("AUDIT_STRICT"):
            raise error


class AuditHooks(RunHooks):
    """Turns the SDK's tool lifecycle into audit rows.

    Two boundaries, one hook pair, because the SDK gives no skill hook: in lazy
    Skills mode a skill activates by the model calling `load_skill`, which is a
    FunctionTool like any other, and MCP tool calls arrive through the same pair.

    Every method body is wrapped: on_tool_start is awaited in an asyncio.gather
    alongside the tool invocation, so an exception escaping here does not just
    lose a row, it kills the user's turn (surfacing as a baffling
    "UserError: Error running tool ..."). An audit bug must never do that.
    """

    def __init__(self, audit: AuditLog, mcp_tool_names: set[str]) -> None:
        self.audit = audit
        self.mcp_tool_names = mcp_tool_names
        self._started: dict[str, float] = {}

    def _audited(self, name: str) -> bool:
        """Skills and MCP calls only.

        The sandbox shell (`exec_command`) and filesystem tools come through this
        same hook, and the Worker leans on the shell to read SKILL.md. Logging
        those would bury the three calls that matter under dozens that do not.
        They are sandbox mechanics, not capabilities.
        """
        return name == LOAD_SKILL_TOOL or name in self.mcp_tool_names

    async def on_tool_start(self, context, agent, tool) -> None:
        try:
            if not self._audited(tool.name):
                return
            call_id = getattr(context, "tool_call_id", None)
            if call_id:
                self._started[call_id] = time.perf_counter()
            arguments = getattr(context, "tool_arguments", None)
            is_skill = tool.name == LOAD_SKILL_TOOL
            # The prefix records which boundary was crossed, which the action
            # alone does not: a local @function_tool would also be a
            # capability_invoked, but it never leaves the process. Reading
            # `mcp:` in the target is how the replay tells the two apart.
            target = tool.name if is_skill else f"mcp:{tool.name}"
            # Written before the call, not after: if a tool hangs or the process
            # dies mid-call, the audit row survives with no matching capability
            # row, and that gap is the evidence. Write both at the end and a
            # crashed call leaves no trace at all.
            await self.audit.record_action(
                ACTION_SKILL_ACTIVATED if is_skill else ACTION_CAPABILITY_INVOKED,
                target=target,
                payload={"arguments": _arguments_json(arguments)},
            )
        except Exception as error:  # noqa: BLE001 - see the class docstring
            self.audit.warn(f"on_tool_start:{getattr(tool, 'name', '?')}", error)

    async def on_tool_end(self, context, agent, tool, result) -> None:
        try:
            if not self._audited(tool.name):
                return
            call_id = getattr(context, "tool_call_id", None)
            started = self._started.pop(call_id, None) if call_id else None
            latency_ms = int((time.perf_counter() - started) * 1000) if started else None
            text = _coerce(result)
            await self.audit.record_capability(
                tool.name,
                arguments=_arguments_json(getattr(context, "tool_arguments", None)),
                result=result,
                # on_tool_end fires for failures too, handing back an error string
                # rather than raising. Without this a blocked refund is recorded
                # as a success.
                status=_status_for(text),
                latency_ms=latency_ms,
            )
        except Exception as error:  # noqa: BLE001 - see the class docstring
            self.audit.warn(f"on_tool_end:{getattr(tool, 'name', '?')}", error)
