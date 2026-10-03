"""Four fixed read-only tools over Streamable HTTP."""

import os
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from incidentops.mcp.backend import (
    EvidenceLimit,
    HTTPBackend,
    Service,
    backend_lifespan,
    configured_url,
)


def create_server(checkout_base_url: str, inventory_base_url: str) -> MCPServer:
    server = MCPServer(
        "IncidentOps observability",
        lifespan=backend_lifespan(checkout_base_url, inventory_base_url),
        subscriptions=False,
    )
    annotations = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    async def probe_arguments_only(ctx, call_next):
        if ctx.method == "tools/call" and ctx.params.get("name") == "probe_checkout":
            arguments = ctx.params.get("arguments", {})
            if type(arguments) is not dict or arguments:
                return CallToolResult(
                    is_error=True,
                    content=[
                        TextContent(type="text", text="Probe accepts no arguments")
                    ],
                )
        return await call_next(ctx)

    server.middleware.append(probe_arguments_only)

    @server.tool(annotations=annotations, structured_output=True)
    async def probe_checkout(ctx: Context[HTTPBackend]) -> dict[str, Any]:
        """Observe a fixed checkout calculation without changing business evidence."""
        return await ctx.request_context.lifespan_context.read("checkout", "probe")

    @server.tool(annotations=annotations, structured_output=True)
    async def get_service_health(
        service: Service, ctx: Context[HTTPBackend]
    ) -> dict[str, Any]:
        """Read process health; this does not establish business-request health."""
        return await ctx.request_context.lifespan_context.read(service, "health")

    @server.tool(annotations=annotations, structured_output=True)
    async def get_metrics(
        service: Service, ctx: Context[HTTPBackend]
    ) -> dict[str, Any]:
        """Read current business-request counters from the selected service."""
        return await ctx.request_context.lifespan_context.read(service, "metrics")

    @server.tool(annotations=annotations, structured_output=True)
    async def query_logs(
        service: Service, ctx: Context[HTTPBackend], limit: EvidenceLimit = 20
    ) -> dict[str, Any]:
        """Read the latest log records, newest first, preserving untrusted text."""
        logs = await ctx.request_context.lifespan_context.read(service, "logs")
        return {"logs": list(reversed(logs[-limit:]))}

    return server


if __name__ == "__main__":
    create_server(
        configured_url("CHECKOUT_BASE_URL"), configured_url("INVENTORY_BASE_URL")
    ).run(
        transport="streamable-http",
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8000")),
        json_response=True,
        stateless_http=True,
    )
