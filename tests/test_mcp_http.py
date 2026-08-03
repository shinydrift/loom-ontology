"""`loom serve` over HTTP, driven as a real MCP client across a real socket.

`test_mcp_stdio.py` is the same idea over a pipe and the two are deliberately not merged: what they
have in common is asserted once, without a transport, in `test_mcp_registry.py`
(`test_both_transports_are_handed_one_server`). What is here is only what a *socket* can break.

**Two clients on one server is the whole reason this file exists.** Over stdio, two clients are two
processes: two `Resolver`s, two `ActionRuntime`s, two DuckDB connections, and nothing they do says
anything about sharing. Over HTTP they are one of each, and the questions that were unreachable
become a config away —

- `DuckDBEngine` holds one connection and registers every scan under `t0` / `t1` / `m0`, which are
  constants in `resolver.py` and therefore the *same three names* for every object type in every
  ontology. Two overlapping reads do not merely contend, they overwrite each other's relation.
- One `ActionRuntime` and one `Resolver` serve every caller for the life of the process.

Both are safe because a served process answers one tool call at a time — see `build_mcp_server`,
whose premise `test_mcp_registry.py` asserts structurally. The tests below are the other half of
that claim: overlapping callers, and answers that are still each caller's own.

**What is still not here: a conflict produced by a real race.** M4's first slice recorded that as
"nothing a client can schedule over stdio reaches inside a spawned process", and that reason does
not survive this transport — the MCP SDK dispatches tool calls concurrently, so HTTP demonstrably
*can* carry an interleave. The reason it is still not producible is now narrower and worth stating
correctly: the served process **serializes**, so no two served runs overlap at all; and a competing
commit from outside the process would still have to land inside the window between one attempt's
read and its write, three attempts running, which nothing outside the process can schedule without
the hook M3 declined to add ("a hook nothing in production calls is a hook that drifts"). So the
conflict's wire form stays asserted against `LoomMCPServer.call` in `test_mcp_registry.py`, and what
two clients against one row produce here is the *other* outcome — the second run reads the row the
first one wrote, and the rule the spec declares refuses it.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
pytest.importorskip("duckdb", reason="needs the [duckdb] extra")
pytest.importorskip("mcp", reason="needs the [mcp] extra")

import httpx2  # noqa: E402 - a transitive dependency of mcp, imported after the skip guard

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "retail"
TIMEOUT = 60
STARTUP_TIMEOUT = 45
ACTOR = "agent:http-test"
PROTOCOL_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Served:
    url: str
    project: Path
    stdout: Path
    stderr: Path


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """One `loom serve` over HTTP, on a loopback bind, with writes on.

    Loopback because that is the only bind `mcp.writes: true` is allowed on — the config refuses the
    combination anywhere else, and `test_config.py` owns that assertion. It is also the deployment
    this transport is actually for: an agent runtime on the same machine, connecting to an address
    instead of spawning a process."""
    import importlib.util

    project = tmp_path_factory.mktemp("serve-http") / "retail"
    shutil.copytree(EXAMPLE, project, ignore=shutil.ignore_patterns(".warehouse"))
    port = _free_port()
    config = project / "loom.yaml"
    # Replaced rather than appended: a second `transport:` key would be a duplicate pyyaml resolves
    # silently, and the whole point of the file is that it says what the deployment is.
    config.write_text(
        config.read_text().replace("  transport: stdio\n", f"  transport: http\n  port: {port}\n")
        + f"  writes: true\n  actor: {ACTOR}\n"
    )

    spec = importlib.util.spec_from_file_location("http_seed", project / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.seed(project)

    stdout, stderr = project / "serve.out", project / "serve.err"
    url = f"http://127.0.0.1:{port}/mcp"
    with stdout.open("w") as out, stderr.open("w") as err:
        # `-m loom.cli` rather than the console script, for the reason the stdio test gives: it runs
        # under the same interpreter as the test, so a venv the script isn't on PATH for still works.
        process = subprocess.Popen(
            [sys.executable, "-m", "loom.cli", "serve", str(project / "ontology")],
            stdout=out,
            stderr=err,
        )
        try:
            _await_listening(process, url, stderr)
            yield Served(url=url, project=project, stdout=stdout, stderr=stderr)
        finally:
            process.terminate()
            process.wait(timeout=30)


def _await_listening(process, url: str, stderr: Path) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"loom serve exited with {process.returncode}:\n{stderr.read_text()}")
        try:
            httpx2.post(url, json={}, headers=PROTOCOL_HEADERS, timeout=2)
            return  # any answer at all means the socket is up; the content is not the point here
        except httpx2.HTTPError:
            time.sleep(0.2)
    raise RuntimeError(f"loom serve never listened on {url}:\n{stderr.read_text()}")


# ---- driving it as a client ------------------------------------------------------


async def _drive(url: str, calls: list[tuple[str, dict]]):
    """One client: connect, initialize, list, make every call in order, disconnect."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            listing = await session.list_tools()
            results = [await session.call_tool(name, args) for name, args in calls]
            return init, listing, results


def _run(url: str, calls: list[tuple[str, dict]]):
    return asyncio.run(asyncio.wait_for(_drive(url, calls), TIMEOUT))


async def _overlap(url: str, per_client: list[list[tuple[str, dict]]]):
    """Every client connected and calling at the same time, rather than one after another.

    `asyncio.gather` is what makes these overlap *in the server's inbox*; whether they overlap
    inside it is the thing under test."""
    async with asyncio.timeout(TIMEOUT):
        return await asyncio.gather(*(_drive(url, calls) for calls in per_client))


def _payload(results, index):
    return json.loads(results[index].content[0].text)


@pytest.fixture(scope="module")
def session(served):
    """One client's read calls, in order — the half that should be identical to stdio."""
    return _run(
        served.url,
        [
            ("get_customer", {"key": "c1"}),
            ("search_customer", {"filter": {"tier": "gold"}}),
            ("traverse", {"objectType": "Customer", "key": "c2", "link": "orders", "limit": 2}),
            ("traverse", {"objectType": "Customer", "key": "c1", "link": "bogus"}),
            (
                "search_daily_sales_performance",
                {"filter": {"salesDate": {"gte": "2026-02-01", "lt": "2026-03-01"}}},
            ),
        ],
    )


# ---- the surface, across a socket ------------------------------------------------


def test_the_server_identifies_itself_from_loom_yaml(session):
    init, _, _ = session
    assert init.server_info.name == "loom-retail"


def test_the_advertised_surface_is_the_one_stdio_advertises(session):
    """A transport is not an input to §7. The same thirteen tools, from the same spec, over a socket."""
    _, listing, _ = session
    assert sorted(t.name for t in listing.tools) == [
        "get_customer",
        "get_daily_sales_performance",
        "get_order",
        "list_customer",
        "list_daily_sales_performance",
        "list_order",
        "run_forget_customer",
        "run_record_order",
        "run_upgrade_tier",
        "search_customer",
        "search_daily_sales_performance",
        "search_order",
        "traverse",
    ]


def test_no_advertised_tool_offers_a_query_escape_hatch(session):
    """`test_no_tool_can_take_a_query` is the framework's central claim; this is it re-checked on
    the bytes a client actually received, because **a transport must not widen the surface**. The
    walk is nested rather than two levels deep, for the reason the registry's copy gives: the write
    tools put declared names inside `parameters`."""
    forbidden = {"sql", "query", "where", "expression", "predicate", "statement", "table", "column"}

    def objects(schema):
        found = [schema]
        for child in (schema.get("properties") or {}).values():
            if isinstance(child, dict) and child.get("type") == "object":
                found.extend(objects(child))
        return found

    _, listing, _ = session
    for tool in listing.tools:
        for schema in objects(tool.input_schema):
            fields = set(schema.get("properties") or {})
            assert not (fields & forbidden), f"{tool.name} exposes {fields & forbidden}"


def test_reads_cross_the_wire(session):
    _, _, results = session
    assert results[0].is_error is False
    assert _payload(results, 0)["object"]["name"] == "Ada Lovelace"
    assert [c["customerId"] for c in _payload(results, 1)["objects"]] == ["c1"]
    traversed = _payload(results, 2)
    assert traversed["targetObjectType"] == "Order"
    assert [o["orderId"] for o in traversed["objects"]] == ["o3", "o4"]
    # A typed range, over a socket: the acceptance query, answering identically to stdio.
    assert [r["salesDate"] for r in _payload(results, 4)["objects"]] == ["2026-02-11", "2026-02-14"]


def test_a_bad_link_is_an_error_result_the_agent_can_recover_from(session):
    _, _, results = session
    assert results[3].is_error is True
    assert "available: orders" in results[3].content[0].text


# ---- two clients, one server -----------------------------------------------------


def test_overlapping_reads_do_not_answer_with_each_others_rows(served):
    """The `t0` hazard, aimed at directly.

    Two clients read two *different* object types at once. The aliases they compile to are the same
    three constants, and the relation registration that binds them is on one shared connection — so
    if a call could be suspended between `con.register` and `con.execute`, a Customer read would
    come back holding Orders. Each round is a fresh pair of overlapping requests, and every answer
    has to be the one its own caller asked for."""
    rounds = 4
    for _ in range(rounds):
        outcomes = asyncio.run(
            _overlap(
                served.url,
                [
                    [("get_customer", {"key": "c1"}), ("list_customer", {"limit": 3})],
                    [("get_order", {"key": "o1"}), ("list_order", {"limit": 3})],
                ],
            )
        )
        (_, _, customer_side), (_, _, order_side) = outcomes

        assert _payload(customer_side, 0)["object"]["customerId"] == "c1"
        assert _payload(customer_side, 1)["objectType"] == "Customer"
        assert all("customerId" in row for row in _payload(customer_side, 1)["objects"])

        assert _payload(order_side, 0)["object"]["orderId"] == "o1"
        assert _payload(order_side, 1)["objectType"] == "Order"
        assert all("orderId" in row for row in _payload(order_side, 1)["objects"])


@pytest.fixture(scope="module")
def two_writers(served):
    """Two clients, one server, one row, both asking for the same change at the same time."""
    return asyncio.run(
        _overlap(
            served.url,
            [
                [("run_upgrade_tier", {"parameters": {"customer": "c3", "newTier": "gold"}})],
                [("run_upgrade_tier", {"parameters": {"customer": "c3", "newTier": "gold"}})],
            ],
        )
    )


def test_two_clients_writing_one_row_are_serialized_and_both_get_a_true_answer(two_writers):
    """The assertion this whole transport slice is for, and the one stdio could not make.

    One process, one `ActionRuntime`, one row, two callers in flight together. Because tool calls
    are serialized, the second run does not read what the first read: it reads what the first
    *wrote*, and `newTier != object.tier` — the rule the spec declares — is then false. So the
    outcome is exactly one `applied` and one `refused`, and the refusal is the ordinary one a
    precondition produces rather than a conflict.

    Which client wins is not asserted, because it is not a promise: what is promised is that neither
    of them is told something untrue. Both are `isError: false`, because both became runs."""
    payloads = []
    for _, _, results in two_writers:
        assert results[0].is_error is False
        payloads.append(_payload(results, 0))

    statuses = sorted(p["status"] for p in payloads)
    assert statuses == ["applied", "refused"]

    applied = next(p for p in payloads if p["status"] == "applied")
    assert applied["before"]["tier"] == "bronze" and applied["after"]["tier"] == "gold"
    assert applied["concurrency"].startswith("enforced")
    assert applied["editId"]

    refused = next(p for p in payloads if p["status"] == "refused")
    assert [f["code"] for f in refused["failures"]] == ["validation_failed"]
    assert refused["failures"][0]["message"] == "New tier must differ from the current tier"


def test_the_lake_holds_the_row_and_the_record(served, two_writers):
    """Read back through pyiceberg while the server is still up. "The tool said applied" and "the
    table changed" are different claims, and a served process is the case where the second one is
    worth checking separately."""
    from loom.action import EditLog
    from loom.catalog import open_catalogs
    from loom.config import find_config, load_config
    from loom.errors import Diagnostics

    diag = Diagnostics()
    config = load_config(find_config(served.project / "ontology"), diag)
    diag.raise_if_errors()
    catalog = open_catalogs(config)["local"]

    row = next(r for r in catalog.scan("crm.customers").to_pylist() if r["id"] == "c3")
    assert row["tier"] == "gold"
    # The columns the ontology never mentions, carried across a served write untouched.
    assert row["region"] == "apac" and row["segments"] is None

    history = EditLog(catalog=catalog).history()
    assert sorted(e["status"] for e in history) == ["applied", "refused"]
    # One actor for both callers — which is what `mcp.actor` is, and the reason a non-loopback bind
    # refuses to serve writes at all rather than recording strangers under a name nobody checked.
    assert {e["actor"] for e in history} == {ACTOR}


# ---- the protocol itself ---------------------------------------------------------


def _raw_session(client: httpx2.Client, url: str) -> dict:
    """Initialize by hand, and return the headers a subsequent request needs."""
    initialize = client.post(
        url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "raw", "version": "0"},
            },
        },
        headers=PROTOCOL_HEADERS,
        timeout=TIMEOUT,
    )
    assert initialize.status_code == 200
    headers = {
        **PROTOCOL_HEADERS,
        "mcp-session-id": initialize.headers["mcp-session-id"],
        "MCP-Protocol-Version": "2025-06-18",
    }
    client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
    return headers


def test_an_http_status_never_disagrees_with_is_error(served, two_writers):
    """**Decided once, because a transport with real status codes invites re-litigating it.**

    Takes `two_writers` because it needs the row they left behind: `c3` is gold by then, so asking
    for gold again is refused by the rule the spec declares. Requested rather than relied on, so the
    test says what it depends on instead of depending on the order tests happen to run in.

    `isError` answers "did this call become a run", and an HTTP status answers "did this exchange
    happen" — different questions at different layers, so they are never two votes on one thing. A
    refused precondition is a 200 carrying content, exactly as it is over a pipe. If it were a 4xx,
    an agent's transport would raise before its own branch on `status` ever ran, and a rule the spec
    author wrote doing its job would arrive looking like a broken client.

    Asserted with a raw client rather than the SDK's, because the SDK would hide the number."""
    with httpx2.Client() as client:
        headers = _raw_session(client, served.url)
        response = client.post(
            served.url,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "run_upgrade_tier",
                    # c3 is gold by now, so the declared rule refuses this.
                    "arguments": {"parameters": {"customer": "c3", "newTier": "gold"}},
                },
            },
            headers=headers,
            timeout=TIMEOUT,
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["isError"] is False
        assert json.loads(result["content"][0]["text"])["status"] == "refused"

        # And the other direction: a usage error is content too, still under a 200.
        bad_link = client.post(
            served.url,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "traverse",
                    "arguments": {"objectType": "Customer", "key": "c1", "link": "bogus"},
                },
            },
            headers=headers,
            timeout=TIMEOUT,
        )
        assert bad_link.status_code == 200 and bad_link.json()["result"]["isError"] is True


def test_a_non_200_is_only_ever_about_the_exchange(served):
    """The complement of the rule above: the statuses this server *does* refuse with are all
    transport-level, and none of them can be produced by a tool.

    Both come from DNS-rebinding protection, which is on and derives its `Host` allow-list from the
    bind. It is the one attack that reaches a loopback-bound server from off the machine — a browser
    on a hostile page resolving a name it owns to 127.0.0.1 — and the `Origin` list is empty on
    purpose, because no browser is a legitimate client of this endpoint."""
    with httpx2.Client() as client:
        headers = _raw_session(client, served.url)
        listing = {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}}

        rebound = client.post(
            served.url, json=listing, headers={**headers, "Host": "evil.example"}, timeout=TIMEOUT
        )
        assert rebound.status_code == 421

        from_a_browser = client.post(
            served.url,
            json=listing,
            headers={**headers, "Origin": "https://evil.example"},
            timeout=TIMEOUT,
        )
        assert from_a_browser.status_code == 403


def test_the_endpoint_is_the_configured_path_with_no_redirect(served):
    """A POST that gets a 307 works only for a client that follows redirects on a POST body. The
    configured path answers directly, and nothing else answers at all."""
    with httpx2.Client() as client:
        elsewhere = client.post(
            served.url.replace("/mcp", "/somewhere-else"),
            json={"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
            headers=PROTOCOL_HEADERS,
            timeout=TIMEOUT,
        )
        assert elsewhere.status_code == 404

        initialize = client.post(
            served.url,
            json={
                "jsonrpc": "2.0",
                "id": 6,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "raw", "version": "0"},
                },
            },
            headers=PROTOCOL_HEADERS,
            timeout=TIMEOUT,
        )
        assert initialize.status_code == 200
        assert not initialize.history  # nothing was redirected on the way


# ---- the banner ------------------------------------------------------------------


def test_every_human_facing_line_goes_to_stderr(served):
    """The rule survives its own justification. It used to be "stdout is the transport", which an
    address-based transport makes false; it is kept because the banner is diagnostics, and a command
    with one output shape is worth more than one that is right for two transports and open again for
    the third. uvicorn's access log — the one thing here that would have written to stdout — is off
    for the same reason."""
    assert served.stdout.read_text() == ""

    banner = served.stderr.read_text()
    assert "13 tool(s) over http" in banner
    assert f"listening on {served.url}" in banner
    assert f"every run recorded as actor '{ACTOR}'" in banner
    # The scaling claim, said rather than discovered. A server that quietly answers one request at a
    # time is a support ticket.
    assert "one call at a time" in banner
