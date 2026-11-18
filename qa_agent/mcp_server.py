"""Official MCP 2.3 stdio server. stdout is reserved for JSON-RPC."""
from typing import Any

from mcp import types
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict

from .models import Identifier
from .runtime import execute_suite


class ExecutionArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    suite_id: Identifier
    entorno_id: Identifier


async def _validate_call(ctx, call_next):
    if ctx.method == "tools/call":
        try:
            params = ctx.params
            if not isinstance(params, dict) or params.get("name") != "ejecutar_plan":
                raise ValueError
            ExecutionArguments.model_validate(params.get("arguments", {}))
        except (ValueError, TypeError):
            return types.CallToolResult(is_error=True, content=[types.TextContent(type="text", text="Invalid execution arguments")])
    return await call_next(ctx)


server = MCPServer("qa-agent", log_level="ERROR", middleware=[_validate_call])


@server.tool(name="ejecutar_plan", structured_output=True)
async def ejecutar_plan(suite_id: Identifier, entorno_id: Identifier) -> dict[str, Any]:
    """Execute a locally registered, explicitly approved generated suite."""
    try:
        # Deliberately run on this process's main thread: runtime owns SIGTERM
        # cleanup of its pytest process group. Execution is sequential in the MVP.
        run = execute_suite(suite_id, entorno_id)
        return {"run_id": run.run_id, "status": run.status.value,
                "result_path": run.result_path, "evidence_paths": run.evidence_paths}
    except Exception:
        # Do not echo untrusted IDs, profile data, exception reprs or credentials.
        return {"error": "Execution rejected; check registration, approval and local profile"}


def main():
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
