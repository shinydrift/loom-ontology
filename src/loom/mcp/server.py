"""The MCP server — the thin adapter from `ToolSpec` to the MCP SDK, over stdio or HTTP.

Intentionally almost logic-free. Every decision about what the agent can see and do was already
made by the registry and enforced by the resolver; this module dispatches and serializes. Keeping
it that thin is what lets the interesting guarantees be tested without a transport.

**Both transports are handed the same server.** `build_mcp_server` is the only place the tool set,
the instructions and the dispatch rule are turned into an SDK object, and `serve_stdio` and
`serve_http` differ only in what they hand it a pair of streams from. That is what makes spec §7's
claim — the generated surface is a function of the spec and nothing else — survive a second
transport: there is no seam here for one to widen the surface through.

**One call at a time, and it is proved rather than hoped.** See `build_mcp_server`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..action import ActionError, ActionRuntime
from ..config import LoomConfig, McpConfig
from ..embed.match import Matcher
from ..embed.provider import EmbeddingError
from ..governance import PolicyProgram
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
        program: PolicyProgram | None = None,
        matcher: Matcher | None = None,
    ) -> LoomMCPServer:
        return cls(
            tools={t.name: t for t in build_tools(resolver, runtime, actor, program, matcher)},
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

        **An argument the tool does not accept is refused here, before the handler.** It is the same
        category as the unknown tool name above and takes the same path: a usage error, `is_error`
        true, because it never became a run — and for a `run_` tool that is the honest answer rather
        than an exception to the paragraph above, since nothing reached the runtime and there is no
        `status` to report. `ToolSpec.argument_refusal` says which arguments and why.
        """
        tool = self.tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self.tools))
            return f"unknown tool '{name}'. Available: {known}", True
        refusal = tool.argument_refusal(arguments or {})
        if refusal is not None:
            return refusal, True
        try:
            result = tool.handler(arguments or {})
        except (ResolverError, ActionError, EmbeddingError) as e:
            # Every refusal Loom *authored* reaches a caller as the sentence it was written as.
            # `EmbeddingError` was not on this list and could reach it — a `match_` call on a
            # deployment without the `embed` extra came back `EmbeddingError: 'provider: local'
            # needs fastembed…`, which is the one refusal in the whole surface that hands an agent
            # a Python class name to parse around.
            return str(e), True
        except Exception as e:
            # The fallback keeps the class name on purpose: anything arriving here is a bug rather
            # than a decision, and the type is the most useful thing a bug report can carry.
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

    **The ranked plane extends that claim rather than qualifying it**, which is worth saying because
    it arrived with a port that can *delete*. `bind_matching` builds each `VectorStore` with no
    `VectorWriter`, and `vector_writer_for()` — the one place a handle becomes a writable one — is
    not called here or anywhere the surface can reach. So the same fake serves `match_` too: this
    process can rank a sidecar and cannot maintain one, and `loom embed` stays the only thing that
    can remove a vector.

    And when `mcp.writes` is off — the default — the question does not arise: no runtime is built,
    so the process is exactly the read-only one M1 shipped.

    The catalogs are opened once here and handed to both halves. `build_resolver` and the runtime
    would each open their own otherwise, which is two connections and two chances to disagree about
    what the lake currently looks like.

    **Where the principal stops, now that a policy can name one.** `_run_tool`'s handler reads
    `auth.current_principal()` per call, and it is read for exactly two things: the edit log records
    a *name* beside `mcp.actor`, and `PolicyProgram.select` turns claims into a decided `PolicySet`.
    **The resolver is still handed no identity**, permanently rather than for want of one — what a
    handler passes down is a set in which every guard is already answered and every `principal.`
    reference already a literal, because a principal is constant for the duration of a call.

    So this function holds one *binding* per plane rather than one `Resolver` and one
    `ActionRuntime`, and the difference is smaller than it sounds: the ontology, the engine and the
    catalogs are still built once and shared by every call, and what is per call is a `replace()` of
    a three-field frozen dataclass. **Per-call scope, not per-call construction.** For a deployment
    whose policies name nobody it is not even that — `select` returns the same object, so
    `governed_by` returns the same resolver, which is asserted rather than assumed.

    Three predictions are corrected rather than left standing, and the third is this slice's:

    - M4 predicted M5 would move this for governance. It did not: a policy that names no principal
      is the same for every caller.
    - This docstring and `build_mcp_server` then both predicted that the milestone landing
      attestation would have to make these per-caller *and* fix the `t0`/`t1`/`m0` aliases. It did
      not, and the reason is worth keeping: what forces per-caller *objects* is a policy that varies
      per caller, and what forces the alias fix is **concurrency**. They are still two forces. The
      policies now do vary, and the aliases are still fine: handlers are synchronous, calls do not
      overlap, and the per-call value is threaded through objects that stay shared. The alias problem
      belongs to whatever milestone makes a handler `async`.
    - `registry.build_tools` predicted that this milestone would make the **tool set** per caller
      too. It did not, and cannot: a mask may not be conditioned, so the announcement half of a
      policy is the same for every caller and the tool set is built once, from `announcing()`."""
    from ..action import bind_writes
    from ..catalog import open_catalogs
    from ..embed.match import bind_matching
    from ..resolver import bind_reads

    open_cats = catalogs if catalogs is not None else open_catalogs(config)
    # `bind_reads`/`bind_writes` rather than `build_resolver`/`build_runtime`, which are those two
    # plus `for_(None)`. This is the one surface that *can* name a caller, so it is the one surface
    # that does not decide its policies at build: it keeps the binding and selects per call. Every
    # refusal is unchanged and still fires here, because they all live in the pairing.
    reads = bind_reads(ontology, config, open_cats)
    # Through the write binding rather than constructing a runtime here, so this process governs its
    # write half through the same function `loom run` does. Both halves bind the same policies from
    # the same config against the same ontology, which is what stops a served surface and a dev
    # command from withholding different things.
    writes = bind_writes(ontology, config, open_cats) if config.mcp.writes else None
    # A third binding, and it is `None` for most deployments. It holds the provider and one
    # *unwritable* view of each sidecar — see `bind_matching` — so the sentence above about what a
    # serving process holds gains one clause and loses nothing: a catalog that implements
    # `VectorWriter` is still never exchanged for one here, so this process can rank vectors and
    # cannot maintain them. `loom embed` is the only thing that can.
    matching = bind_matching(ontology, config, open_cats)
    if not config.mcp.attests:
        # The refusal every other surface reaches by *needing* a decided policy set, reached here on
        # purpose instead. A stdio server is the case: it can carry policies naming a caller and can
        # never attest one, and finding that out per call would be a server that started and then
        # failed every read. `select(None)` is the same function `loom query` refuses through, so
        # there is one sentence and one place it comes from.
        reads.program.select(None)
    server = LoomMCPServer.from_resolver(
        # The announcing pair builds the tool set: masks, which every caller shares, and nothing
        # that can read a row. `program` is what the handlers select with, per call.
        reads.announcing(),
        server_name=config.mcp.name,
        runtime=writes.announcing() if writes is not None else None,
        actor=config.mcp.actor,
        program=reads.program,
        matcher=matching,
    )
    # The announcing resolver, deliberately: what a caller of this function wants it for is the
    # banner — what this deployment withholds and which policies withhold it — and reading with it
    # is refused rather than answered with a set nobody selected. The reads happen through the
    # server, per call, where a caller has a name.
    return server, reads.announcing()


def build_mcp_server(loom_server: LoomMCPServer):
    """The SDK `Server` both transports run. Built here once so neither can drift from the other.

    **This server answers one tool call at a time, and that is a decision.** The MCP SDK dispatches
    `on_call_tool` concurrently — two clients on one HTTP server genuinely interleave, which was
    measured rather than assumed. What serializes Loom is one rung down: `on_call_tool` awaits
    nothing, `LoomMCPServer.call` is an ordinary function, and every `ToolSpec.handler` is an
    ordinary function too, so a call runs to completion without ever yielding the event loop. That
    is a proof rather than a convention — a synchronous callable *cannot* be interleaved — and
    `test_mcp_registry.py` asserts the premise so that making any of them `async` fails a test
    instead of quietly changing what the process guarantees.

    It is deliberate because the alternative is not a transport's to choose. Three pieces of shared
    state sit under here and none of them belong to this layer:

    - `DuckDBEngine` holds **one** connection for the process, and `execute()` calls
      `con.register(scan.alias, arrow)` before every query. The aliases are `t0` / `t1` / `m0` —
      module constants in `resolver.py`, identical for every object type in every ontology — so two
      concurrent reads do not merely race on the same table, they race on the same three names, and
      the loser answers with the winner's rows. That is the query layer's to fix.
    - `build_server` holds one binding per plane for the lifetime of the process, and a handler
      derives a `Resolver` from it per call. That was written as "the same change M5 needs in order
      to filter by principal", and then as belonging to the milestone that attests a principal,
      "which is where the second half of it — this alias problem — has to be solved anyway". **Both
      halves of that prediction were wrong, and the milestone it named is where it was found out.**
      Policies now do vary per caller and the derivation is a `replace()` of a frozen three-field
      dataclass — the ontology, the engine, its one connection and the catalogs are still built once
      and shared. What forces the alias fix is two calls **in flight at once**, which is a different
      force entirely: it belongs to whatever milestone makes a handler `async`, and until one does,
      the assertion below is what keeps it unnecessary.
    - The runtime's retry loop reasons about competing commits, not about competing callers in one
      process.

    So the concurrency question is answered by keeping the answer stdio always had, and the cost is
    stated out loud in the banner and the README instead of being discovered: a slow query does not
    queue behind another call, it blocks the server.

    A lock was the obvious alternative and is worse. Over synchronous handlers it can never be
    contended, so it is code with no behaviour — and its only effect would be to keep the guarantee
    silently alive the day somebody makes a handler `async`, turning a correctness question into an
    unexplained performance one. The assertion fails loudly instead."""
    import mcp.types as types
    from mcp.server.lowlevel import Server

    async def on_list_tools(_ctx, _params) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(name=t.name, description=t.description, inputSchema=t.input_schema)
                for t in loom_server.tools.values()
            ]
        )

    async def on_call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        # No await between here and the result: see this function's docstring.
        text, is_error = loom_server.call(params.name, dict(params.arguments or {}))
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=is_error)

    # Named in the instructions rather than left to be discovered in the tool list, because it is
    # the one tool whose *absence* an agent would otherwise read as "this data cannot be searched
    # that way" rather than "this deployment did not switch it on". When it is there, saying when to
    # reach for it is the whole difference between an agent that finds a chargeback and one that
    # greps for the word.
    rankable = [t for t in loom_server.tools if t.startswith("match_")]
    match_note = (
        " When a search by keyword comes back empty and the answer would be in prose, use "
        "match_<object>: it ranks rows by meaning rather than by wording, and its `filter` argument "
        "narrows before the ranking."
        if rankable
        else ""
    )
    writable = [t for t in loom_server.tools if t.startswith("run_")]
    write_note = (
        " Use run_<action> to change one object through a declared action: its result is typed, so "
        "read `status` and `failures[].code` rather than treating a refusal as a broken call, and "
        "pass dryRun to see what a run would do without doing it."
        if writable
        else " This deployment is read-only: no action is exposed."
    )
    # The build that generated this surface, in the one field the protocol has for it. It is the
    # same `loom_version` stamped into every recorded apply, every recorded load and every edit-log
    # row, and printed by `loom --version` — so a client asking a live server which Loom it is
    # talking to and an auditor reading what that server wrote cannot get two answers. Left empty,
    # the SDK reports `version: ""` and substitutes nothing.
    from ..migrate.meta import loom_version

    server: Server = Server(
        loom_server.server_name,
        version=loom_version(),
        instructions=(
            "This server exposes a Loom ontology: typed objects, declared links, and governed "
            "reads. Use get_/search_/list_ tools for objects and `traverse` to follow a link. "
            "There is no SQL interface." + match_note + write_note
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    return server


async def serve_stdio(loom_server: LoomMCPServer) -> None:  # pragma: no cover - needs a live stdio peer
    """Run the tool set over stdio until the client disconnects."""
    from mcp.server.stdio import stdio_server

    server = build_mcp_server(loom_server)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _authenticated(endpoint, mcp: McpConfig):
    """Wrap the MCP endpoint in the bearer-token stack, or hand it back untouched.

    Untouched is the whole of "this deployment attests nobody", and it is what every deployment
    before M6 was. There is no partial state between the two.

    **Wrapped around the endpoint rather than mounted on the app, and that is a bug's worth of
    reason.** The SDK's `RequireAuthMiddleware` does not check `scope["type"]` before deciding a
    request is unauthenticated, so at app level it treats the ASGI **lifespan** scope as a request
    with no user and answers it with a `401`. Starlette's own `AuthenticationMiddleware` guards
    against exactly this (`if scope["type"] not in ("http", "websocket")`); this one does not. The
    lifespan then never completes, `manager.run()` is never entered, and every real request fails
    with *Task group is not initialized* — a startup failure that surfaces as a `500` on the first
    tool call rather than as anything at startup. Wrapping the route's endpoint means these three
    only ever see the scopes of the one path they are meant to protect.

    **A token is required, not merely honoured.** With `auth:` declared, a request without a valid
    one is a `401` and never reaches a tool. The alternative — accept it and treat the caller as
    principal-less — would give one deployment two classes of caller, and the un-tokened class would
    be recorded as `principal: null` while running exactly the same writes. That is the same shape
    §6.1 refuses for policies (a caller that can arrange to be unnamed is a back door around the
    naming), applied one layer down, and it is why `RequireAuthMiddleware` is in this stack rather
    than just the two that populate the context.

    **A `401` does not contradict §7's `isError` rule; it is the other question.** An HTTP status
    answers *did this exchange happen*, `isError` answers *did this call become a run*. A rejected
    token is an exchange that did not happen — the same category as a rejected `Host` or a malformed
    body, both of which already answer non-`200` — and no tool outcome produces it. The rule that
    every tool result is a `200` carrying content is untouched, because an unauthenticated request
    never produces a tool result at all.

    Three middlewares, outermost first, and each does one thing this codebase should not reimplement:
    `AuthenticationMiddleware` runs Loom's verifier over the `Authorization` header,
    `AuthContextMiddleware` puts the result where a synchronous handler can read it, and
    `RequireAuthMiddleware` turns its absence into the `401`. What the SDK does not supply — and the
    only part that is a *judgement* rather than plumbing — is whether a token is believable, which is
    `auth.TokenVerifier` and the reason that module exists."""
    if mcp.auth is None:
        return endpoint
    from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
    from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
    from starlette.middleware.authentication import AuthenticationMiddleware

    from ..auth import build_verifier

    verifier = build_verifier(mcp.auth)
    # Innermost out: refuse what is not authenticated, publish what is, and authenticate first.
    guarded = RequireAuthMiddleware(AuthContextMiddleware(endpoint), required_scopes=[])
    return AuthenticationMiddleware(guarded, backend=BearerAuthBackend(token_verifier=verifier))


class _Endpoint:
    """The session manager as a bare ASGI app, so Starlette routes to it instead of wrapping it."""

    def __init__(self, manager) -> None:
        self._manager = manager

    async def __call__(self, scope, receive, send) -> None:
        await self._manager.handle_request(scope, receive, send)


async def serve_http(loom_server: LoomMCPServer, mcp: McpConfig) -> None:  # pragma: no cover - driven by test_mcp_http
    """Run the same tool set over MCP's streamable HTTP transport, until killed.

    Where this differs from stdio is only in what a process stops being. A spawned server belongs to
    the client that spawned it; this one belongs to whoever can reach the address, which is why
    `McpConfig` draws its limits on the bind rather than on the transport and why the config refuses
    a write surface on a non-loopback bind before this function is ever called.

    Three choices inside it are worth naming:

    **`json_response=True`.** The alternative is an SSE stream per response, and this server has
    nothing to stream: it sends no notifications, no progress and no partial results, so the stream
    would carry exactly one message and close. A plain JSON body is also what makes the status-code
    claim checkable by anything, rather than only by an SDK client.

    **DNS-rebinding protection stays on**, with the `Host` allow-list from `McpConfig`. It is the
    one attack that reaches a loopback-bound server from outside the machine: a browser on a hostile
    page resolves a name it controls to 127.0.0.1 and posts to it. The `Origin` allow-list is left
    empty on purpose — no browser is a legitimate client of this endpoint, so any request that
    carries an `Origin` at all is one to refuse.

    **No access log.** uvicorn writes its access log to stdout, and `cmd_serve` keeps every
    human-facing line on stderr. Not because stdout is the transport any more — over HTTP it is not
    — but because the banner is diagnostics, and one output shape that survives a third transport is
    worth more than one that is right for two."""
    import contextlib

    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.applications import Starlette
    from starlette.routing import Route

    manager = StreamableHTTPSessionManager(
        app=build_mcp_server(loom_server),
        json_response=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(mcp.host_allow_list()),
            allowed_origins=[],
        ),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    # A `Route` rather than a `Mount`, and `_Endpoint` rather than the bound method, for one reason
    # each. Mounting `/mcp` makes Starlette answer `POST /mcp` with a 307 to `/mcp/` — harmless for
    # a client that follows redirects on a POST and a silent failure for one that does not, which is
    # not a thing to leave in the path of every request. And `Route` wraps a plain function or bound
    # method as a request/response handler, so the ASGI app has to arrive as something that is
    # neither.
    app = Starlette(
        routes=[Route(mcp.path, _authenticated(_Endpoint(manager), mcp), methods=["GET", "POST", "DELETE"])],
        lifespan=lifespan,
    )
    await uvicorn.Server(
        uvicorn.Config(app, host=mcp.host, port=mcp.port, log_level="warning", access_log=False)
    ).serve()
