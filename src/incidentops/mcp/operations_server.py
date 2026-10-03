"""Deployment reads only; synthetic control operations are not MCP tools."""

import os
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

from incidentops.mcp.backend import (
    EvidenceLimit,
    HTTPBackend,
    backend_lifespan,
    configured_url,
)


def create_server(checkout_base_url: str) -> MCPServer:
    server = MCPServer(
        "IncidentOps operations",
        lifespan=backend_lifespan(checkout_base_url),
        subscriptions=False,
    )

    @server.tool(
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def get_recent_deployments(
        ctx: Context[HTTPBackend],
        service: Literal["checkout"] = "checkout",
        limit: EvidenceLimit = 20,
    ) -> dict[str, Any]:
        """Read current version and recent real deployment entries, newest first."""
        data = await ctx.request_context.lifespan_context.read(service, "deployments")
        return {
            **data,
            "deployment_history": list(reversed(data["deployment_history"][-limit:])),
        }

    return server


if __name__ == "__main__":
    create_server(configured_url("CHECKOUT_BASE_URL")).run(
        transport="streamable-http",
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8000")),
        json_response=True,
        stateless_http=True,
    )
