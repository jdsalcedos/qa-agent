"""Synchronous CLI facade over a real official-SDK asynchronous stdio client."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

from mcp import Client, StdioServerParameters, stdio_client
from mcp.client.stdio import DEFAULT_INHERITED_ENV_VARS

from .mcp_server import ExecutionArguments
from .runtime import executor_environment
from .store import Store


async def _execute_via_mcp(suite_id: str, environment_id: str, store: Store) -> dict:
    ExecutionArguments(suite_id=suite_id, entorno_id=environment_id)
    profile = store.load_profile(environment_id)
    env = executor_environment(profile, store)
    # The SDK merges default HOME/PATH/etc even when env is explicit. Override
    # those defaults so none can carry ambient configuration/secrets to Python.
    env = {name: "" for name in DEFAULT_INHERITED_ENV_VARS} | env
    package_root = str(Path(__file__).resolve().parent.parent)
    bootstrap = "import sys,runpy; sys.path.insert(0,sys.argv[1]); runpy.run_module('qa_agent.mcp_server',run_name='__main__')"
    params = StdioServerParameters(command=sys.executable,
                                   args=["-I", "-B", "-c", bootstrap, package_root],
                                   env=env, cwd=store.root)
    store.root.mkdir(parents=True, exist_ok=True)
    # Server and SDK stderr are never forwarded raw to a user-facing log.
    with open(os.devnull, "w") as stderr:
        async with Client(stdio_client(params, errlog=stderr),
                          read_timeout_seconds=profile.process_timeout_seconds + 20) as client:
            tools = await client.list_tools()
            if "ejecutar_plan" not in {tool.name for tool in tools.tools}:
                raise RuntimeError("MCP execution tool was not discovered")
            result = await client.call_tool("ejecutar_plan", {"suite_id": suite_id, "entorno_id": environment_id})
            data = result.structured_content
            if result.is_error or not isinstance(data, dict) or "run_id" not in data:
                raise RuntimeError("MCP execution was rejected")
            # Resolve the returned ID through trusted local persistence rather
            # than trusting arbitrary paths in a protocol response.
            run = store.load_run(data["run_id"])
            return {"run_id": run.run_id, "status": run.status.value,
                    "result_path": run.result_path, "evidence_paths": run.evidence_paths}


def execute_via_mcp(suite_id: str, environment_id: str, *, store: Store | None = None) -> dict:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("The synchronous MCP facade must be called outside an event loop")
    try:
        return asyncio.run(_execute_via_mcp(suite_id, environment_id, store or Store()))
    except Exception:
        # SDK exception chains can contain raw protocol data. No repr/traceback
        # from them crosses this boundary, even on malformed transport responses.
        raise RuntimeError("MCP execution failed; inspect local approval/profile and run history") from None
