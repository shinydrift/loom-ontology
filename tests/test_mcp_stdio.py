"""`loom serve` driven as a real MCP client over stdio.

The rest of the MCP tests exercise `LoomMCPServer` in-process, which is the right level for tool
shape and dispatch. This one spawns the actual CLI and speaks the protocol to it, because the thin
adapter in mcp/server.py is exactly where an SDK API change breaks things silently — the tool set
would still be correct and nothing else would notice.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
pytest.importorskip("duckdb", reason="needs the [duckdb] extra")
pytest.importorskip("mcp", reason="needs the [mcp] extra")

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "retail"
TIMEOUT = 60


@pytest.fixture(scope="module")
def served_ontology(tmp_path_factory):
    """A seeded copy of the example, ready to be served out of a tmp dir."""
    import importlib.util

    target = tmp_path_factory.mktemp("serve") / "retail"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns(".warehouse"))

    spec = importlib.util.spec_from_file_location("stdio_seed", target / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.seed(target)
    return target / "ontology"


async def _drive(ontology_dir: Path, calls: list[tuple[str, dict]]):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # `-m loom.cli` rather than the `loom` script: it runs under the same interpreter as the test,
    # so this works in a venv the console script isn't on PATH for.
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "loom.cli", "serve", str(ontology_dir)]
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            listing = await session.list_tools()
            results = [await session.call_tool(name, args) for name, args in calls]
            return init, listing, results


def _run(ontology_dir: Path, calls: list[tuple[str, dict]]):
    return asyncio.run(asyncio.wait_for(_drive(ontology_dir, calls), TIMEOUT))


@pytest.fixture(scope="module")
def session(served_ontology):
    """One server run covering every assertion below — spawning a process per test is slow."""
    return _run(
        served_ontology,
        [
            ("get_customer", {"key": "c1"}),
            ("search_customer", {"filter": {"tier": "gold"}}),
            ("traverse", {"objectType": "Customer", "key": "c2", "link": "orders", "limit": 2}),
            ("traverse", {"objectType": "Customer", "key": "c1", "link": "bogus"}),
        ],
    )


def test_the_server_identifies_itself_from_loom_yaml(session):
    init, _, _ = session
    assert init.server_info.name == "loom-retail"


def test_list_tools_returns_the_generated_surface(session):
    _, listing, _ = session
    assert sorted(t.name for t in listing.tools) == [
        "get_customer",
        "get_order",
        "list_customer",
        "list_order",
        "search_customer",
        "search_order",
        "traverse",
    ]


def test_advertised_schemas_survive_the_protocol(session):
    """The input schema an agent actually receives, not the one the registry built."""
    _, listing, _ = session
    tool = next(t for t in listing.tools if t.name == "get_customer")
    assert tool.input_schema["properties"]["key"]["type"] == "string"
    assert tool.input_schema["required"] == ["key"]
    assert tool.input_schema["additionalProperties"] is False


def test_no_advertised_tool_offers_a_query_escape_hatch(session):
    _, listing, _ = session
    for tool in listing.tools:
        assert not ({"sql", "query", "where", "table"} & set(tool.input_schema.get("properties", {})))


def test_get_over_the_wire_returns_the_row(session):
    _, _, results = session
    payload = json.loads(results[0].content[0].text)
    assert results[0].is_error is False
    assert payload["object"]["name"] == "Ada Lovelace"


def test_search_over_the_wire_filters(session):
    _, _, results = session
    payload = json.loads(results[1].content[0].text)
    assert [c["customerId"] for c in payload["objects"]] == ["c1"]


def test_traverse_over_the_wire_pages(session):
    _, _, results = session
    payload = json.loads(results[2].content[0].text)
    assert payload["targetObjectType"] == "Order"
    assert [o["orderId"] for o in payload["objects"]] == ["o3", "o4"]
    assert payload["hasMore"] is True


def test_a_bad_link_is_an_error_result_the_agent_can_recover_from(session):
    """Not a protocol-level failure: the content names the links that do exist."""
    _, _, results = session
    assert results[3].is_error is True
    assert "available: orders" in results[3].content[0].text
