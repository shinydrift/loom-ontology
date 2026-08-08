#!/usr/bin/env python3
"""A dashboard over the retail ontology, wired to Loom the only way a dashboard can be.

    python examples/retail/dashboard/app.py        # then open http://127.0.0.1:8080

**The browser is not the client. This process is.** `serve_http` sets `allowed_origins=[]` and
leaves DNS-rebinding protection on, with the reason written in its own docstring: *no browser is a
legitimate client of this endpoint, so any request that carries an `Origin` at all is one to
refuse.* So a page cannot `fetch()` Loom's MCP endpoint, and the server-side hop below is not a
convenience — it is the only topology available. Which is the honest one anyway: an agent runtime
holding an MCP session and a UI in front of it is exactly what this is a model of.

**What this file is not allowed to be is a second way into the lake.** It imports `loom` to *start*
a server (the `--mcp-url` half of this file is the same dashboard with that server started by
somebody else), and after that it reaches the data through one MCP session like any other client.
The whole browser-facing data plane is a single route:

    POST /api/call  {"name": "search_customer", "arguments": {...}}  ->  session.call_tool(...)

One passthrough, no per-panel endpoints. That is a deliberate constraint rather than a shortcut: a
route per panel is a route per panel's worth of opportunity to reach past the tool surface — to add
a filter the ontology never declared, to join two objects the spec never linked, to read a column a
policy withholds. With one generic route there is nowhere to put any of that. Every number in the
UI is a `run_`/`get_`/`search_`/`list_`/`traverse` call, and the tool rail down the right-hand side
of the page is the proof: it is not a log the UI writes about itself, it is the same objects the
fetch layer sends and receives.

The one route that is *not* a tool call is `POST /api/refresh`, and it is marked as such in the UI.
See `_refresh_aggregate`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE.parent
REPO = EXAMPLE.parents[1]

# Run from a checkout without installing. Harmless when the package is installed — an existing
# `loom` on the path wins, because these are appended-to-front only if not already importable.
for candidate in (REPO / "src", EXAMPLE):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

DEFAULT_UI_PORT = 8080
STARTUP_TIMEOUT = 45.0
PROTOCOL_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


# ---- the ontology and the deployment ---------------------------------------------


def load_project(config_path: Path):
    """The spec from `../ontology`, the deployment from this directory's own `loom.yaml`.

    Named explicitly rather than discovered. `find_config` would walk from the ontology dir up to
    `../loom.yaml` — the stdio, read-only deployment the README documents — and quietly serve that
    instead, which is the sort of thing you find out about three panels later."""
    from loom import build
    from loom.config import load_config
    from loom.errors import Diagnostics

    diag = Diagnostics()
    config = load_config(config_path, diag)
    ontology, ont_diag = build(str(EXAMPLE / "ontology"))
    diag.warnings.extend(ont_diag.warnings)
    diag.raise_if_errors()
    assert config is not None
    return ontology, config


def warehouse_ready(config) -> bool:
    catalog = config.catalogs.get("local")
    if catalog is None or not catalog.warehouse:
        return False
    return Path(catalog.warehouse.removeprefix("file://")).exists()


# ---- the MCP server half ---------------------------------------------------------


async def _await_listening(url: str, server_task: asyncio.Task) -> None:
    """Wait for the socket, and give up the moment the server task dies instead of at the deadline.

    A refusal from `build_resolver` — an engine that cannot serve a declared filter, a policy that
    names a caller this transport cannot attest — surfaces as the task raising, and that is the
    thing worth reporting. Waiting 45 seconds to say "never listened" would bury it."""
    import httpx2

    deadline = time.monotonic() + STARTUP_TIMEOUT
    async with httpx2.AsyncClient() as client:
        while time.monotonic() < deadline:
            if server_task.done():
                await server_task  # re-raises whatever it failed with
                raise RuntimeError("the MCP server stopped before it listened")
            with contextlib.suppress(httpx2.HTTPError):
                # Any answer at all means the socket is up; the content is not the point.
                await client.post(url, json={}, headers=PROTOCOL_HEADERS, timeout=2)
                return
            await asyncio.sleep(0.15)
    raise RuntimeError(f"the MCP server never listened on {url}")


def build_loom_server(ontology, config):
    """Assemble the served surface up front, so a refusal is a startup error and not a blank panel.

    This mirrors `cmd_serve`: `build_server` is where a policy is bound and an engine is negotiated
    with, and both can refuse. Better to fail here than to advertise tools that fail on every call."""
    from loom.mcp.server import build_server

    return build_server(ontology, config)


# ---- the MCP client half ---------------------------------------------------------


class LoomClient:
    """One MCP session, held for the life of the process, plus the tool listing it advertised.

    Serialized behind a lock. The served process answers one tool call at a time regardless (see
    `build_mcp_server`), so this costs nothing real and buys a tool rail whose order is the order
    the calls actually happened in."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._stack: contextlib.AsyncExitStack | None = None
        self._session: Any = None
        self._lock = asyncio.Lock()
        self.server_name = "loom"
        self.tools: list[dict[str, Any]] = []

    async def connect(self) -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        stack = contextlib.AsyncExitStack()
        read, write = await stack.enter_async_context(streamable_http_client(self.url))
        session = await stack.enter_async_context(ClientSession(read, write))
        init = await session.initialize()
        listing = await session.list_tools()
        self._stack, self._session = stack, session
        self.server_name = init.server_info.name
        self.tools = [
            {
                "name": t.name,
                "description": t.description,
                # The SDK's own field, under whichever spelling this version uses.
                "inputSchema": getattr(t, "input_schema", None) or getattr(t, "inputSchema", {}),
            }
            for t in sorted(listing.tools, key=lambda t: t.name)
        ]

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = self._session = None

    async def call(self, name: str, arguments: dict) -> dict:
        """One tool call, plus what it cost. Returns the shape the browser's rail renders.

        `isError` is passed through untouched and deliberately not folded into an HTTP status.
        It answers *did this call become a run* — never *did it succeed* — so a `refused` action
        comes back `isError: false` with a body saying why, and the UI has to read the body. A proxy
        that turned that into a 4xx would be answering a question the tool layer refused to answer.
        """
        if self._session is None:
            raise RuntimeError("not connected")
        started = time.perf_counter()
        async with self._lock:
            result = await self._session.call_tool(name, arguments)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        text = "".join(getattr(block, "text", "") for block in (result.content or []))
        try:
            payload: Any = json.loads(text) if text else None
        except json.JSONDecodeError:
            # A usage error comes back as prose — an unknown link name, a value that isn't the
            # declared type. Handed on as-is; the UI shows it beside the call that caused it.
            payload = {"message": text}
        return {
            "name": name,
            "arguments": arguments,
            "isError": bool(getattr(result, "isError", None) or getattr(result, "is_error", False)),
            "result": payload,
            "ms": elapsed_ms,
        }


# ---- the one thing that is not a tool call ---------------------------------------


def _refresh_aggregate(config) -> dict:
    """Rebuild `sales.daily_sales_performance` from `sales.orders`.

    **Not a Loom tool, and the UI says so where it is offered.** `DailySalesPerformance` is an
    ingestion-time aggregate — `sales_performance.py` builds it with pyiceberg and calls itself "not
    a new Loom query primitive" — so `run_record_order` writes a row to `sales.orders` and the sales
    chart does not move until something recomputes the rollup. That gap is the tradeoff a
    precomputed aggregate *is*, and hiding it behind a tool-shaped button would be the dashboard
    telling a more comfortable story than the lake supports. So it sits here, on its own route, in
    its own colour, labelled as the ingestion side.

    Blocking pyiceberg work; the caller runs it off the event loop."""
    from sales_performance import refresh_daily_sales_performance
    from seed import open_sql_catalog

    catalog = open_sql_catalog(config)
    refresh_daily_sales_performance(catalog)
    table = catalog.load_table("sales.daily_sales_performance")
    return {"rows": table.scan().to_arrow().num_rows}


# ---- the web app -----------------------------------------------------------------


def build_app(client: LoomClient, config, *, writes: bool):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse
    from starlette.routing import Route

    async def index(_request: Request):
        return FileResponse(HERE / "index.html")

    async def surface(_request: Request):
        """What the deployment turned out to be — read off the session, never off the config.

        The tool list is the server's answer to `tools/list`, so a mask that removed a filter or a
        `writes: false` that withheld the `run_` tools shows up here as an absence rather than as a
        flag the UI has to be told to believe."""
        return JSONResponse(
            {
                "serverName": client.server_name,
                "mcpUrl": client.url,
                "actor": config.mcp.actor,
                "writes": writes,
                "tools": client.tools,
            }
        )

    async def call(request: Request):
        body = await request.json()
        name = body.get("name")
        if not isinstance(name, str) or not any(t["name"] == name for t in client.tools):
            # Not a permission check — the server would refuse an unknown tool by itself, and says
            # so better. This keeps the rail honest: everything in it is a call that was made.
            return JSONResponse({"error": f"no tool named {name!r} on this deployment"}, status_code=400)
        return JSONResponse(await client.call(name, body.get("arguments") or {}))

    async def refresh(_request: Request):
        return JSONResponse(await asyncio.to_thread(_refresh_aggregate, config))

    return Starlette(
        routes=[
            Route("/", index),
            Route("/api/surface", surface),
            Route("/api/call", call, methods=["POST"]),
            Route("/api/refresh", refresh, methods=["POST"]),
        ]
    )


async def run(args) -> int:
    import uvicorn

    config_path = Path(args.config).resolve()
    ontology, config = load_project(config_path)

    if not warehouse_ready(config):
        print(
            "error: no warehouse yet — run `python examples/retail/seed.py` first",
            file=sys.stderr,
        )
        return 1

    server_task: asyncio.Task | None = None
    if args.mcp_url:
        url, writes = args.mcp_url, True
        print(f"dashboard → an MCP server you started: {url}", file=sys.stderr)
    else:
        server, resolver = build_loom_server(ontology, config)
        url, writes = config.mcp.address(), config.mcp.writes
        print(
            f"loom (in-process) — {ontology.summary()} → {len(server.tools)} tool(s) over "
            f"{config.mcp.transport} at {url}",
            file=sys.stderr,
        )
        masked = {name: resolver.masked(name) for name in ontology.object_types}
        withheld = ", ".join(f"{ot}.{p}" for ot, props in masked.items() for p in props)
        print(f"  governance · withholding {withheld}" if withheld else "  governance · nothing withheld", file=sys.stderr)
        print(f"  writes · {'on' if writes else 'off'}, recorded as actor {config.mcp.actor!r}", file=sys.stderr)
        from loom.mcp.server import serve_http

        server_task = asyncio.create_task(serve_http(server, config.mcp))

    try:
        if server_task is not None:
            await _await_listening(url, server_task)
        client = LoomClient(url)
        await client.connect()
        print(f"  session · {client.server_name}, {len(client.tools)} tool(s) advertised", file=sys.stderr)

        app = build_app(client, config, writes=writes)
        print(f"\ndashboard → http://{args.host}:{args.port}\n", file=sys.stderr)
        web = uvicorn.Server(
            uvicorn.Config(app, host=args.host, port=args.port, log_level="warning", access_log=False)
        )
        try:
            await web.serve()
        finally:
            await client.aclose()
    finally:
        if server_task is not None:
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dashboard", description="A dashboard over the retail ontology, served through Loom's MCP tools"
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address for the dashboard itself")
    parser.add_argument("--port", type=int, default=DEFAULT_UI_PORT, help="port for the dashboard itself")
    parser.add_argument(
        "--config",
        default=str(HERE / "loom.yaml"),
        help="the deployment to serve (default: this directory's loom.yaml)",
    )
    parser.add_argument(
        "--mcp-url",
        default=None,
        metavar="URL",
        help="attach to a `loom serve` you started, instead of starting one in this process",
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
