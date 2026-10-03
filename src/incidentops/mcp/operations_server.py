"""Internal deployment reads and one explicit rollback capability."""

import os
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent, ToolAnnotations

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

    async def rollback_arguments_only(ctx, call_next):
        # SDK function signatures otherwise ignore surplus arguments. Reject them
        # for the sole mutation instead of silently accepting a URL/body/path.
        if (
            ctx.method == "tools/call"
            and ctx.params.get("name") == "rollback_deployment"
        ):
            arguments = ctx.params.get("arguments")
            if not isinstance(arguments, dict) or set(arguments) != {
                "service",
                "target_version",
            }:
                return CallToolResult(
                    is_error=True,
                    content=[
                        TextContent(
                            type="text",
                            text="Rollback accepts only service and target_version",
                        )
                    ],
                )
        return await call_next(ctx)

    server.middleware.append(rollback_arguments_only)

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

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=True,
    )
    async def rollback_deployment(
        ctx: Context[HTTPBackend],
        service: Literal["checkout"],
        target_version: Literal["v1", "v2"],
    ) -> dict[str, Any]:
        """Rollback to a previously deployed checkout version; same target is a no-op.

        Internal capability: IncidentOps enforces human approval before invoking it.
        """
        return await ctx.request_context.lifespan_context.rollback_deployment(
            service, target_version
        )

    return server


if __name__ == "__main__":
    create_server(configured_url("CHECKOUT_BASE_URL")).run(
        transport="streamable-http",
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8000")),
        json_response=True,
        stateless_http=True,
    )
