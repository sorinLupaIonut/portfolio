"""Minimal terminal chat agent: a SandboxAgent on a local sandbox."""

import asyncio
import os
import sys
from pathlib import Path

from agents import Runner
from agents.run import RunConfig
from agents.sandbox import SandboxAgent, SandboxRunConfig
from agents.sandbox.sandboxes.docker import DockerSandboxClient, DockerSandboxClientOptions
from docker import from_env as docker_from_env
from dotenv import load_dotenv

# Sandbox capabilities need custom tools, which only the gpt-5 family accepts:
# anything older (gpt-4.1-mini and friends) fails with a 400 on `tools`.
DEFAULT_MODEL = "gpt-5-mini"

# UnixLocalSandboxClient is macOS/Linux only, so this base runs the sandbox in Docker.
SANDBOX_IMAGE = "python:3.14-slim"


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


def build_agent(model: str) -> SandboxAgent[None]:
    return SandboxAgent(
        name="Worker",
        model=model,
        instructions="You are a helpful assistant. Answer briefly and plainly.",
    )


async def main() -> None:
    load_dotenv()
    ensure_docker_cli_on_path()
    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY is not set. Put it in .env and try again.")

    model = os.getenv("AGENT_MODEL", DEFAULT_MODEL)
    agent = build_agent(model)
    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=DockerSandboxClient(docker_from_env()),
            options=DockerSandboxClientOptions(image=SANDBOX_IMAGE),
        ),
        workflow_name="Digital FTE chat",
    )

    print(f"Chat with the Worker ({model}). Type 'exit' to quit.\n")
    while True:
        try:
            message = input("you > ").strip()
        except EOFError:
            break
        if not message:
            continue
        if message.lower() in {"exit", "quit"}:
            break
        result = await Runner.run(agent, message, run_config=run_config)
        print(f"worker > {result.final_output}\n")


if __name__ == "__main__":
    asyncio.run(main())
