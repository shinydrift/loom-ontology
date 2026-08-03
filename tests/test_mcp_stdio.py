"""`loom serve` driven as a real MCP client over stdio.

The rest of the MCP tests exercise `LoomMCPServer` in-process, which is the right level for tool
shape and dispatch. This one spawns the actual CLI and speaks the protocol to it, because the thin
adapter in mcp/server.py is exactly where an SDK API change breaks things silently — the tool set
would still be correct and nothing else would notice.

Since the write surface landed, that includes a row actually changing in a real Iceberg table
because an MCP client asked, and the record of it appearing in `_loom_meta.edits` under the actor
the deployment declared. Both are read back afterwards through pyiceberg rather than believed from
the response, because "the tool returned applied" and "the lake changed" are different claims.

**What is deliberately not here: a conflict produced by a real race.** A conflict needs a competing
commit to land inside the window between the server's read and its write, and nothing a client can
do over the protocol schedules that — the interleaving seam is the catalog port, which lives inside
the served process, and M3 settled that the seam is the port rather than a hook the runtime would
carry for tests. So the conflict's *wire form* is asserted in `test_mcp_registry.py` against
`LoomMCPServer.call`, which is the exact function this adapter calls and whose `(text, is_error)`
pair is what goes on the wire; what is asserted here is the half that only a transport can break —
that a refusal crosses it as content rather than as a protocol error.
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
ACTOR = "agent:stdio-test"


def _seeded(tmp_path_factory, name: str, config_extra: str = "") -> Path:
    """A seeded copy of the example, ready to be served out of a tmp dir."""
    import importlib.util

    target = tmp_path_factory.mktemp(name) / "retail"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns(".warehouse"))
    if config_extra:
        config = target / "loom.yaml"
        config.write_text(config.read_text() + config_extra)

    spec = importlib.util.spec_from_file_location("stdio_seed", target / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.seed(target)
    return target


@pytest.fixture(scope="module")
def served_project(tmp_path_factory):
    """The example with writes turned on and an actor declared — a deployment that said so."""
    return _seeded(tmp_path_factory, "serve-writes", f"  writes: true\n  actor: {ACTOR}\n")


@pytest.fixture(scope="module")
def served_ontology(served_project):
    return served_project / "ontology"


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
    """One server run covering every assertion below — spawning a process per test is slow.

    The three `run_upgrade_tier` calls are ordered on purpose: preview, then the real write, then
    the same call again — which now refuses, because the write it follows made the rule false."""
    return _run(
        served_ontology,
        [
            ("get_customer", {"key": "c1"}),
            ("search_customer", {"filter": {"tier": "gold"}}),
            ("traverse", {"objectType": "Customer", "key": "c2", "link": "orders", "limit": 2}),
            ("traverse", {"objectType": "Customer", "key": "c1", "link": "bogus"}),
            ("run_upgrade_tier", {"parameters": {"customer": "c3", "newTier": "gold"}, "dryRun": True}),
            ("run_upgrade_tier", {"parameters": {"customer": "c3", "newTier": "gold"}}),
            ("run_upgrade_tier", {"parameters": {"customer": "c3", "newTier": "gold"}}),
            ("run_forget_customer", {"parameters": {"customer": "nobody"}}),
            ("get_customer", {"key": "c3"}),
        ],
    )


@pytest.fixture(scope="module")
def readonly_session(tmp_path_factory):
    """The same example with the config left alone — the default a deployment gets."""
    project = _seeded(tmp_path_factory, "serve-readonly")
    return _run(project / "ontology", [])


def test_the_server_identifies_itself_from_loom_yaml(session):
    init, _, _ = session
    assert init.server_info.name == "loom-retail"


def test_list_tools_returns_the_generated_surface(session):
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


def test_a_default_deployment_advertises_no_way_to_write(readonly_session):
    """The example declares three actions and `mcp.writes` is unset, so the surface is M1's.

    This is the assertion that makes the default meaningful: an upgrade does not turn somebody's
    lake writable, and it takes a line in `loom.yaml` rather than a spec edit to change that."""
    _, listing, _ = readonly_session
    assert not [t.name for t in listing.tools if t.name.startswith("run_")]
    assert len(listing.tools) == 10


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


# ---- the write half --------------------------------------------------------------


def _payload(results, index):
    return json.loads(results[index].content[0].text)


def test_the_advertised_run_schema_types_the_declared_parameters(session):
    """The schema an agent actually receives for a write — nested parameters, `dryRun` beside them,
    closed at both levels."""
    _, listing, _ = session
    tool = next(t for t in listing.tools if t.name == "run_upgrade_tier")
    assert set(tool.input_schema["properties"]) == {"parameters", "dryRun"}
    parameters = tool.input_schema["properties"]["parameters"]
    assert parameters["properties"]["newTier"]["enum"] == ["silver", "gold"]
    assert sorted(parameters["required"]) == ["customer", "newTier"]
    assert tool.input_schema["additionalProperties"] is False
    assert parameters["additionalProperties"] is False
    assert "Raise a customer to a higher membership tier." in tool.description


def test_a_preview_crosses_the_wire_and_writes_nothing(session):
    _, _, results = session
    payload = _payload(results, 4)
    assert results[4].is_error is False
    assert payload["status"] == "previewed"
    assert payload["before"]["tier"] == "bronze" and payload["after"]["tier"] == "gold"
    # Nothing is held between a preview and the run after it, and the result says so rather than
    # letting a snapshot id imply otherwise.
    assert payload["concurrency"].startswith("not checked")
    assert payload["editId"] == ""


def test_a_write_lands_in_the_lake_because_an_mcp_client_asked(session):
    _, _, results = session
    payload = _payload(results, 5)
    assert results[5].is_error is False
    assert payload["status"] == "applied"
    assert payload["before"]["tier"] == "bronze" and payload["after"]["tier"] == "gold"
    assert payload["concurrency"].startswith("enforced")
    assert payload["editId"]
    # And the read half of the same process now sees it — one ontology, two surfaces over one lake.
    assert _payload(results, 8)["object"]["tier"] == "gold"


def test_a_refusal_crosses_the_wire_as_content_not_as_a_protocol_error(session):
    """The rule under the transport: `isError` says whether the call became a run, not whether the
    run succeeded. The third call repeats the second, which by then is against a gold customer."""
    _, _, results = session
    assert results[6].is_error is False
    payload = _payload(results, 6)
    assert payload["status"] == "refused"
    assert [f["code"] for f in payload["failures"]] == ["validation_failed"]
    assert payload["failures"][0]["message"] == "New tier must differ from the current tier"

    # And a refusal from the other end of the run — a key that names no row at all.
    assert results[7].is_error is False
    missing = _payload(results, 7)
    assert missing["status"] == "refused"
    assert [f["code"] for f in missing["failures"]] == ["object_not_found"]


def test_the_lake_holds_the_row_and_the_record_after_the_server_exits(session, served_project):
    """Read back through pyiceberg, not believed from the response. "The tool said applied" and
    "the table changed" are different claims, and only one of them is the product."""
    from loom.action import EditLog
    from loom.catalog import open_catalogs
    from loom.config import find_config, load_config
    from loom.errors import Diagnostics

    _, _, results = session
    diag = Diagnostics()
    config = load_config(find_config(served_project / "ontology"), diag)
    diag.raise_if_errors()
    catalog = open_catalogs(config)["local"]

    row = next(r for r in catalog.scan("crm.customers").to_pylist() if r["id"] == "c3")
    assert row["tier"] == "gold"
    # The columns the ontology never mentions, carried across a served write untouched.
    assert row["region"] == "apac" and row["segments"] is None

    history = EditLog(catalog=catalog).history()
    # Three runs named a row; the preview is not one of them, and the refusals are.
    assert [e["status"] for e in history] == ["applied", "refused", "refused"]
    assert {e["actor"] for e in history} == {ACTOR}
    assert history[0]["edit_id"] == _payload(results, 5)["editId"]
    assert history[0]["action"] == "upgradeTier" and history[0]["object_key"] == "c3"
