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

from ..action import ActionError, ActionRuntime
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
    def from_resolver(
        cls,
        resolver: Resolver,
        server_name: str = "loom",
        runtime: ActionRuntime | None = None,
        actor: str | None = None,
    ) -> LoomMCPServer:
        return cls(
            tools={t.name: t for t in build_tools(resolver, runtime, actor)},
            server_name=server_name,
        )

    def call(self, name: str, arguments: dict | None) -> tuple[str, bool]:
        """Dispatch one tool call. Returns `(text, is_error)`.

        **`is_error` answers "did this call become a run?", never "did it succeed?"**

        The read half set the precedent: a `ResolverError` is a *usage* error — an unknown object
        type, a bad link name, a value that isn't the declared type — and its message already names
        the valid alternatives, so it comes back as tool-call content rather than a protocol error,
        which is the form an agent can recover from on the next turn. An `ActionError` is the same
        category (a catalog the config never bound) and takes the same path.

        A `run_` tool that *reached* the runtime is different in kind, and its result is never an
        error here whatever it says. Three reasons, and the third is the one that settles it:

        - A refusal is the **expected** outcome of a precondition. The validation rule the spec
          author wrote did its job. Flagging that as an error tells an agent it used the tool wrong
          when it used the tool exactly right.
        - The outcome is four-way (`applied` · `previewed` · `refused` · `failed`) and one of the
          failure codes is retryable. A boolean can carry neither distinction, and the two it would
          have to collapse — "refused because a rule said no" and "failed after deciding to write" —
          are the two an agent must act on differently.
        - **`applied` with a failure beside it is a real shape, and the boolean gets it wrong.** A
          committed write whose edit-log append failed comes back `applied` plus a non-retryable
          `log_failed`. `is_error=True` would say the write did not happen. It did.

        So an agent branches on `status`, then `failures[].code`, then `retryable` — which the
        generated tool description says out loud, because the input schema cannot.
        """
        tool = self.tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self.tools))
            return f"unknown tool '{name}'. Available: {known}", True
        try:
            result = tool.handler(arguments or {})
        except (ResolverError, ActionError) as e:
            return str(e), True
        except Exception as e:
            return f"{type(e).__name__}: {e}", True
        return json.dumps(result, indent=2, default=str), False


def build_server(ontology: Ontology, config: LoomConfig, catalogs: Mapping[str, Any] | None = None):
    """Assemble ontology + config into a `(LoomMCPServer, Resolver)` pair.

    **What the serving process ends up holding**, because M3 made a claim here that this slice has
    to restate rather than inherit. The runtime asks for a `RowWriter` per run and never keeps one,
    and that used to be written as "nothing in a serving process holds a row-writable handle between
    calls". Under `loom serve` that sentence is worth less than it sounds: the process holds
    `Catalog`s, and a catalog that implements every port — every real one does — is a single
    function call from being a row writer whatever the runtime does with it. The true and narrower
    version is that nothing holds a row-writable *typed* reference, so `row_writer_for()` stays the
    one place the write plane is named at a call site.

    What replaces it is a claim a fake can actually prove: **a serving process can change the rows
    the spec's actions declare, and no schema at all.** Nothing here reaches for `writer_for()`,
    nothing in the runtime has a verb that would, and a catalog implementing `Catalog` + `RowWriter`
    + `EditLogWriter` and *not* `CatalogWriter` serves every tool in this set. Point an MCP client
    at a lake and it cannot migrate one.

    And when `mcp.writes` is off — the default — the question does not arise: no runtime is built,
    so the process is exactly the read-only one M1 shipped.

    The catalogs are opened once here and handed to both halves. `build_resolver` and the runtime
    would each open their own otherwise, which is two connections and two chances to disagree about
    what the lake currently looks like."""
    from ..catalog import open_catalogs
    from ..resolver import build_resolver

    open_cats = catalogs if catalogs is not None else open_catalogs(config)
    resolver = build_resolver(ontology, config, open_cats)
    runtime = ActionRuntime(ontology=ontology, catalogs=open_cats) if config.mcp.writes else None
    server = LoomMCPServer.from_resolver(
        resolver, server_name=config.mcp.name, runtime=runtime, actor=config.mcp.actor
    )
    return server, resolver


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

    writable = [t for t in loom_server.tools if t.startswith("run_")]
    write_note = (
        " Use run_<action> to change one object through a declared action: its result is typed, so "
        "read `status` and `failures[].code` rather than treating a refusal as a broken call, and "
        "pass dryRun to see what a run would do without doing it."
        if writable
        else " This deployment is read-only: no action is exposed."
    )
    server: Server = Server(
        loom_server.server_name,
        instructions=(
            "This server exposes a Loom ontology: typed objects, declared links, and governed "
            "reads. Use get_/search_/list_ tools for objects and `traverse` to follow a link. "
            "There is no SQL interface." + write_note
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
