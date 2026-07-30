"""stdio MCP server — the thin adapter from `ToolSpec` to the MCP SDK.

Intentionally almost logic-free. Every decision about what the agent can see and do was already
made by the registry and enforced by the resolver; this module dispatches and serializes. Keeping
it that thin is what lets the interesting guarantees be tested without a transport.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..config import LoomConfig
from ..model import Ontology
from ..resolver import Resolver, ResolverError
from .registry import ToolSpec, build_tools


@dataclass
class LoomMCPServer:
    """The tool set plus dispatch. Holds no transport state, so it is directly testable."""

    tools: Mapping[str, ToolSpec]
    server_name: str = "loom"

    @classmethod
    def from_resolver(cls, resolver: Resolver, server_name: str = "loom") -> LoomMCPServer:
        return cls(tools={t.name: t for t in build_tools(resolver)}, server_name=server_name)

    def call(self, name: str, arguments: dict | None) -> tuple[str, bool]:
        """Dispatch one tool call. Returns `(text, is_error)`.

        A ResolverError is a *usage* error — an unknown object type, a bad link name, a value that
        isn't the declared type — and its message already names the valid alternatives. So it comes
        back as tool-call content rather than a protocol error: that's the form an agent can
        actually recover from on the next turn.
        """
        tool = self.tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self.tools))
            return f"unknown tool '{name}'. Available: {known}", True
        try:
            result = tool.handler(arguments or {})
        except ResolverError as e:
            return str(e), True
        except Exception as e:
            return f"{type(e).__name__}: {e}", True
        return json.dumps(result, indent=2, default=str), False


def build_server(ontology: Ontology, config: LoomConfig, catalogs: Mapping[str, Any] | None = None):
    """Assemble ontology + config into a `(LoomMCPServer, Resolver)` pair."""
    from ..resolver import build_resolver

    resolver = build_resolver(ontology, config, catalogs)
    return LoomMCPServer.from_resolver(resolver, server_name=config.mcp.name), resolver


async def serve_stdio(loom_server: LoomMCPServer) -> None:  # pragma: no cover - needs a live stdio peer
    """Run the tool set over stdio until the client disconnects."""
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    async def on_list_tools(_ctx, _params) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(name=t.name, description=t.description, inputSchema=t.input_schema)
                for t in loom_server.tools.values()
            ]
        )

    async def on_call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        text, is_error = loom_server.call(params.name, dict(params.arguments or {}))
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=is_error)

    server: Server = Server(
        loom_server.server_name,
        instructions=(
            "This server exposes a Loom ontology: typed objects, declared links, and governed "
            "reads. Use get_/search_/list_ tools for objects and `traverse` to follow a link. "
            "There is no SQL interface."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
