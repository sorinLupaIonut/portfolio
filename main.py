"""Minimal terminal chat agent: a SandboxAgent on a local sandbox."""

import asyncio
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from agents import Runner
from agents.extensions.memory import SQLAlchemySession
from agents.run import RunConfig
from agents.sandbox import SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Capabilities, LocalDirLazySkillSource, Skills
from agents.sandbox.entries import LocalDir
from agents.sandbox.sandboxes.docker import (
    DockerSandboxClient,
    DockerSandboxClientOptions,
    DockerSandboxSession,
)
from docker import from_env as docker_from_env
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine

# Sandbox capabilities need custom tools, which only the gpt-5 family accepts:
# anything older (gpt-4.1-mini and friends) fails with a 400 on `tools`.
DEFAULT_MODEL = "gpt-5-mini"

# UnixLocalSandboxClient is macOS/Linux only, so this base runs the sandbox in Docker.
SANDBOX_IMAGE = "python:3.14-slim"

# Deliberately relative: LocalDir resolves src against the SDK process's cwd, so the
# worker has to be started from the project root. An absolute path that points at
# nothing loads zero skills without raising, which is the worst way for this to fail.
SKILLS_DIR = Path(".claude/skills")


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


def build_agent(model: str) -> SandboxAgent[None]:
    return SandboxAgent(
        name="Worker",
        model=model,
        instructions=(
            "You are a customer-support Worker. Be accurate and concise, and never "
            "invent customer, order, or ticket details you were not given."
        ),
        # Capabilities REPLACE the defaults rather than extending them, so the
        # filesystem and shell the sandbox relies on have to be carried along.
        # lazy_from reads each SKILL.md's metadata up front but copies a skill's
        # files into the sandbox only when the model actually asks for it.
        capabilities=Capabilities.default()
        + [Skills(lazy_from=LocalDirLazySkillSource(source=LocalDir(src=SKILLS_DIR)))],
    )


async def main() -> None:
    load_dotenv()
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
    agent = build_agent(model)
    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=DockerSandboxClient(docker_from_env()),
            options=DockerSandboxClientOptions(image=SANDBOX_IMAGE),
        ),
        workflow_name="Digital FTE chat",
    )

    # Pass SESSION_ID to resume an earlier conversation; otherwise every run is a new one.
    session_id = os.getenv("SESSION_ID") or f"cli-{uuid.uuid4()}"
    engine = create_async_engine(to_asyncpg_url(database_url))
    session = SQLAlchemySession(session_id, engine=engine, create_tables=True)

    print(f"Chat with the Worker ({model}). Type 'exit' to quit.")
    print(f"session: {session_id}\n")
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
            result = await Runner.run(agent, message, run_config=run_config, session=session)
            print(f"worker > {result.final_output}\n")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
