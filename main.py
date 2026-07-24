"""Minimal terminal chat agent: a SandboxAgent on a local sandbox."""

import asyncio
import json
import os
import sys
import uuid
from collections.abc import Sequence
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from agents import Runner
from agents.exceptions import (
    InputGuardrailTripwireTriggered,
    OutputGuardrailTripwireTriggered,
    ToolInputGuardrailTripwireTriggered,
    ToolOutputGuardrailTripwireTriggered,
)
from agents.extensions.memory import SQLAlchemySession
from agents.mcp import MCPServer, MCPServerStreamableHttp
from agents.run import RunConfig
from agents.sandbox import SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import (
    Capabilities,
    LocalDirLazySkillSource,
    SkillMetadata,
    Skills,
)
from agents.sandbox.entries import LocalDir
from agents.sandbox.sandboxes.docker import (
    DockerSandboxClient,
    DockerSandboxClientOptions,
    DockerSandboxSession,
)
from docker import from_env as docker_from_env
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine

from audit import (
    ACTION_GUARDRAIL_TRIPPED,
    ACTION_MESSAGE_RECEIVED,
    ACTION_MESSAGE_SENT,
    ACTION_REFUND_BLOCKED,
    AuditHooks,
    AuditLog,
)
from guardrails import no_card_numbers, redact_card_numbers

# Sandbox capabilities need custom tools, which only the gpt-5 family accepts:
# anything older (gpt-4.1-mini and friends) fails with a 400 on `tools`.
DEFAULT_MODEL = "gpt-5-mini"

# UnixLocalSandboxClient is macOS/Linux only, so this base runs the sandbox in Docker.
SANDBOX_IMAGE = "python:3.14-slim"

# Deliberately relative: LocalDir resolves src against the SDK process's cwd, so the
# worker has to be started from the project root. An absolute path that points at
# nothing loads zero skills without raising, which is the worst way for this to fail.
SKILLS_DIR = Path(".claude/skills")

# The same folder also holds the skills the *build* agent uses (skill-creator,
# mcp-builder, neon-postgres). Those are development-plane tools -- neon-postgres in
# particular teaches direct Postgres access, which is what the Worker must never do --
# so name the Worker's own skills instead of shipping whatever is in the directory.
# An allowlist fails safe: a build-time skill installed later cannot leak into runtime.
WORKER_SKILLS = frozenset({"summarize-ticket", "find-similar-cases", "escalate-with-context"})

# The scoped customer-data server: a separate process the Worker reaches by URL,
# not a subprocess it spawns. It has to be running first -- see its README.
CUSTOMER_DATA_URL = os.getenv("CUSTOMER_DATA_URL", "http://127.0.0.1:8000/mcp")


class WorkerSkillSource(LocalDirLazySkillSource):
    """A lazy skill source that exposes only the skills the Worker owns.

    The capability has no include/exclude of its own, and filtering the index here
    also covers load_skill, which resolves names through this same list.
    """

    names: frozenset[str]

    def list_skill_metadata(self, **kwargs: Any) -> list[SkillMetadata]:
        found = {skill.name: skill for skill in super().list_skill_metadata(**kwargs)}
        # A name that matches nothing would just drop that skill from the index, and a
        # skill missing from the index can never fire -- with no error anywhere. Since
        # the point of naming them is to be explicit, say so instead of shipping short.
        if missing := sorted(self.names - found.keys()):
            raise ValueError(
                f"Named skills not found in {self.source.src}: {', '.join(missing)}. "
                f"Present: {', '.join(sorted(found)) or '(none)'}"
            )
        return [found[name] for name in sorted(self.names)]


def disable_sandbox_pty() -> None:
    """Force the sandbox shell onto its non-PTY exec path.

    On Docker + Windows the PTY path returns empty output for every command:
    `echo hello` comes back blank with exit 0, and so does reading a file. The
    worker can still run commands, it just cannot see any result -- which
    silently breaks Skills, since load_skill only copies SKILL.md and the model
    is expected to read it through the shell. The non-PTY path works correctly.
    """
    DockerSandboxSession.supports_pty = lambda self: False  # type: ignore[method-assign]


def ensure_docker_cli_on_path() -> None:
    """Put Docker Desktop's bin folder on PATH for this process only.

    On Windows it installs per-user and stays out of PATH, so pulling the sandbox
    image fails on a missing docker-credential-desktop helper.
    """
    if os.name != "nt":
        return
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return
    bin_dir = Path(local_app_data) / "Programs" / "DockerDesktop" / "resources" / "bin"
    if bin_dir.is_dir() and str(bin_dir) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def to_asyncpg_url(url: str) -> str:
    """Turn Neon's libpq connection string into the one SQLAlchemy's asyncpg driver wants.

    asyncpg has no idea what `sslmode` or `channel_binding` are; it takes `ssl`.
    `prepared_statement_cache_size=0` is for Neon's pooled endpoint (`-pooler` in the
    host): PgBouncer in transaction mode hands the same session to different clients,
    so a cached prepared statement can arrive at a connection that never prepared it.
    """
    parsed = urlsplit(url)
    query = {
        key: value
        for key, value in parse_qsl(parsed.query)
        if key not in {"sslmode", "channel_binding"}
    }
    query.setdefault("ssl", "require")
    query.setdefault("prepared_statement_cache_size", "0")
    return urlunsplit(("postgresql+asyncpg", parsed.netloc, parsed.path, urlencode(query), ""))


def connect_customer_data() -> MCPServerStreamableHttp:
    """The Worker's only route to business data.

    Not connected here -- the caller enters it as an async context manager, and
    the connection has to outlive every turn of the chat loop.
    """
    return MCPServerStreamableHttp(
        name="customer-data",
        params={"url": CUSTOMER_DATA_URL},
        # The gate lives here, on how the server is registered -- not inside the
        # tool. issue_refund moves money, so it pauses the run for a human yes/no
        # before it executes; the two read tools are absent from the list and run
        # untouched. In Claude Code or OpenCode the client's own permission prompt
        # is this same gate; require_approval is the standalone Worker's version.
        # "always" means every issue_refund call is gated, regardless of amount --
        # the amount ceiling is a separate concern the gate deliberately doesn't make.
        require_approval={"always": {"tool_names": ["issue_refund"]}},
        # The three tools never change while the server is up, so fetching the
        # list once instead of on every run saves a round trip per turn.
        cache_tools_list=True,
        # The default is 5s, and one call there does an OpenAI embedding request
        # plus a Neon query. A timeout that fires mid-write is worse than a slow
        # answer: the client gives up while the server is still committing.
        client_session_timeout_seconds=30,
        # max_retry_attempts stays at 0 deliberately. issue_refund moves money,
        # and a retry after a response is lost in transit pays twice. A failed
        # refund the human can see beats a silent double refund.
    )


def approval_arguments(raw_item: Any) -> dict[str, Any]:
    """Pull a gated tool call's arguments out of an approval item as a plain dict.

    The model sends them as a JSON string. Our MCP tools each take a single
    `params` object, so they arrive wrapped as {"params": {...}}; unwrap that one
    level so the human sees order_id/amount_cents/reason, not a nested blob.
    """
    raw = getattr(raw_item, "arguments", None)
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        return {"_raw": raw}
    if isinstance(parsed, dict) and set(parsed) == {"params"} and isinstance(parsed["params"], dict):
        return parsed["params"]
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}


def render_approval(tool_name: str, arguments: dict[str, Any]) -> str:
    """The block the human reads before signing off on a gated tool call."""
    lines = [f"\n[approval] {tool_name} needs your sign-off before it runs:"]
    for key, value in arguments.items():
        shown = value
        # amount_cents is the honest storage unit but an unfriendly thing to eyeball;
        # show the dollars beside it so a wrong order of magnitude is obvious.
        if key == "amount_cents" and isinstance(value, int):
            shown = f"{value} (${value / 100:,.2f})"
        lines.append(f"    {key:<13}{shown}")
    lines.append("  approve? [y/N] ")
    return "\n".join(lines)


async def resolve_approvals(
    state: Any, interruptions: Sequence[Any], audit: AuditLog
) -> None:
    """Prompt for each gated tool call and record the decision on the run state.

    Approval needs no audit row of its own: the tool then runs, and the
    capability_invoked row the hooks write IS the proof it was approved. A
    rejection is the opposite -- the tool never runs, so absence would be the
    only trace. That is why a refusal gets its own explicit refund_blocked row.
    """
    for item in interruptions:
        tool_name = getattr(item.raw_item, "name", None) or getattr(item, "tool_name", None) or "tool"
        arguments = approval_arguments(item.raw_item)
        print(render_approval(tool_name, arguments), end="")
        answer = (await asyncio.to_thread(input, "")).strip().lower()
        if answer == "y":
            state.approve(item)
            print("  -> approved\n")
            continue
        reason = "rejected by human at approval gate"
        state.reject(item, rejection_message=reason)
        print("  -> rejected\n")
        # refund_blocked is issue_refund's word. If some other tool is ever gated,
        # the rejection still lives in the model's history via rejection_message;
        # it just doesn't borrow a domain action that would misdescribe it.
        if tool_name == "issue_refund":
            await audit.record_action(
                ACTION_REFUND_BLOCKED,
                target=f"mcp:{tool_name}",
                payload={"arguments": arguments, "reason": reason},
            )


def build_agent(model: str, mcp_servers: Sequence[MCPServer]) -> SandboxAgent[None]:
    return SandboxAgent(
        name="Worker",
        model=model,
        mcp_servers=list(mcp_servers),
        instructions=(
            "You are a customer-support Worker. Be accurate and concise, and never "
            "invent customer, order, or ticket details you were not given -- the "
            "customer-data tools are your only route to that data.\n\n"
            # Without the second sentence the model stalls on identifiers: told to
            # look things up, it reaches for lookup_customer, finds it needs a UUID
            # nobody has, and asks for one instead of searching. Measured: it went
            # straight to asking for ids in 1 of 4 runs, calling no tool at all.
            "When someone describes a problem, search past resolved cases before "
            "doing anything else. Do that even when you have no ids: the search "
            "takes a description, not an id. Ask for identifiers only after you "
            "have searched, and only for the step that actually needs one.\n\n"
            # load_skill returns {'status': 'loaded'} and nothing else, which reads like
            # the skill is now in effect. It is not: it only copied files. Without this
            # the model sometimes answers straight from the index description, which
            # names the output's sections but carries none of the rules behind them.
            "load_skill only copies a skill's files into the workspace. It tells you "
            "nothing about what the skill says, so after calling it, read that skill's "
            "SKILL.md before you answer. The description you picked the skill from is a "
            "routing summary; the instructions you have to follow are in the file."
        ),
        # Deterministic and free: no model call, so it cannot drift or add latency.
        # Tripping raises out of Runner.run before the model is invoked, which is
        # the point -- a pasted card number never reaches OpenAI or the session store.
        input_guardrails=[no_card_numbers],
        # Capabilities REPLACE the defaults rather than extending them, so the
        # filesystem and shell the sandbox relies on have to be carried along.
        # lazy_from reads each SKILL.md's metadata up front but copies a skill's
        # files into the sandbox only when the model actually asks for it.
        capabilities=Capabilities.default()
        + [
            Skills(
                lazy_from=WorkerSkillSource(
                    source=LocalDir(src=SKILLS_DIR), names=WORKER_SKILLS
                )
            )
        ],
    )


async def main() -> None:
    load_dotenv()
    # The Windows console defaults to cp1252, and the model reaches for characters
    # it cannot encode (non-breaking hyphens, curly quotes). Printing one raises
    # UnicodeEncodeError and takes down a turn whose work already succeeded.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ensure_docker_cli_on_path()
    disable_sandbox_pty()
    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY is not set. Put it in .env and try again.")
    database_url = os.getenv("NEON_DATABASE_URL")
    if not database_url:
        sys.exit("NEON_DATABASE_URL is not set. Put it in .env and try again.")
    if not SKILLS_DIR.is_dir():
        sys.exit(f"No skills at {SKILLS_DIR}. Start the worker from the project root.")

    model = os.getenv("AGENT_MODEL", DEFAULT_MODEL)
    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=DockerSandboxClient(docker_from_env()),
            options=DockerSandboxClientOptions(image=SANDBOX_IMAGE),
        ),
        workflow_name="Digital FTE chat",
    )

    # Pass SESSION_ID to resume an earlier conversation; otherwise every run is a new one.
    session_id = os.getenv("SESSION_ID") or f"cli-{uuid.uuid4()}"
    engine = create_async_engine(
        to_asyncpg_url(database_url),
        # A chat loop idles for as long as the human takes to read and reply, and
        # Neon's pooler closes an idle connection well inside that. Without a
        # pre-ping, SQLAlchemy hands the next turn a connection the server has
        # already dropped, and the turn dies on "connection is closed" -- after
        # message_received is written, so the conversation ends mid-trace.
        # to_thread keeps the event loop alive during input(); it cannot keep a
        # socket alive. This is the part that does.
        pool_pre_ping=True,
        # Belt and braces: retire connections before the pooler decides to.
        pool_recycle=300,
    )
    session = SQLAlchemySession(session_id, engine=engine, create_tables=True)

    async with AsyncExitStack() as stack:
        try:
            customer_data = await stack.enter_async_context(connect_customer_data())
        except Exception as error:
            # Worth catching rather than letting the raw connection error out:
            # a stopped server and a broken one look identical in the traceback,
            # and the fix for the common case is "start the other terminal".
            sys.exit(
                f"Could not reach the customer-data server at {CUSTOMER_DATA_URL}\n"
                f"  {type(error).__name__}: {error}\n\n"
                f"It is a separate process and has to be running first:\n"
                f"  uv run python customer-data-mcp/server.py"
            )

        tool_names = [tool.name for tool in await customer_data.list_tools()]
        agent = build_agent(model, [customer_data])

        # Startup is the one place an audit failure is allowed to be fatal: a
        # Worker that cannot write its ledger should not take a customer's turn
        # at all. Once the loop is running the rule inverts -- see AuditHooks.
        audit = await stack.enter_async_context(AuditLog(database_url, session_id))
        await audit.ensure_conversation(
            user_id=os.getenv("WORKER_USER_ID", "cli-user"),
            metadata={"model": model, "interface": "cli"},
        )
        hooks = AuditHooks(audit, mcp_tool_names=set(tool_names))

        print(f"Chat with the Worker ({model}). Type 'exit' to quit.")
        print(f"session    : {session_id}")
        print(f"mcp tools  : {', '.join(tool_names)}")
        print(f"audited    : load_skill, {', '.join(tool_names)}\n")
        try:
            while True:
                # input() blocks the event loop, and Neon's pooler drops the idle
                # connection while it is blocked; the next turn then fails mid-write.
                try:
                    message = (await asyncio.to_thread(input, "you > ")).strip()
                except EOFError:
                    break
                if not message:
                    continue
                if message.lower() in {"exit", "quit"}:
                    break

                # Redacted before storage, not after: this row is written before
                # Runner.run, so the guardrail has not run yet. Without redaction
                # the audit log would keep a verbatim copy of the very thing the
                # guardrail exists to block.
                await audit.record_action(
                    ACTION_MESSAGE_RECEIVED,
                    payload={"message": redact_card_numbers(message)},
                )
                try:
                    result = await Runner.run(
                        agent, message, run_config=run_config, session=session, hooks=hooks
                    )
                    # A gated tool (issue_refund) does not execute -- it pauses the
                    # run and comes back as an interruption. Resolve each, then
                    # resume from the run state. A resume can surface a fresh
                    # interruption, so this is a loop, not an if.
                    while result.interruptions:
                        state = result.to_state()
                        await resolve_approvals(state, result.interruptions, audit)
                        result = await Runner.run(
                            agent, state, run_config=run_config, session=session, hooks=hooks
                        )
                except (
                    InputGuardrailTripwireTriggered,
                    OutputGuardrailTripwireTriggered,
                    ToolInputGuardrailTripwireTriggered,
                    ToolOutputGuardrailTripwireTriggered,
                ) as tripped:
                    # A trip is an exception, not a hook -- this except block is
                    # the only place the row can be written. The turn ends here,
                    # so it deliberately produces no message_sent.
                    await audit.record_action(
                        ACTION_GUARDRAIL_TRIPPED,
                        target=type(tripped).__name__,
                        payload={"detail": str(tripped)},
                    )
                    print(
                        "worker > I can't accept card numbers in chat. "
                        "Please remove it and describe the problem instead.\n"
                    )
                    continue

                await audit.record_action(
                    ACTION_MESSAGE_SENT,
                    payload={"model": model},
                    result={"message": redact_card_numbers(str(result.final_output))},
                )
                print(f"worker > {result.final_output}\n")
        finally:
            # In a finally block, so an exception here would replace whatever
            # actually went wrong with a misleading one. Closing the row is the
            # least important thing happening at this point.
            try:
                await audit.close_conversation()
            except Exception as error:  # noqa: BLE001
                audit.warn("close_conversation", error)
            if audit.failures:
                print(f"\n{audit.failures} audit write(s) failed this session.", file=sys.stderr)
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
