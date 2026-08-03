"""The generated agent surface.

These tests import no MCP SDK and open no transport — that's the reason the registry is separate
from the server. The load-bearing assertion is `test_no_tool_can_take_a_query`: the framework's
central claim is that an ontology compiles to typed verbs, so if a raw-SQL escape hatch ever
appears in the generated surface, it fails here. The write tools widened that surface, so the
assertion now walks every nested object in a schema rather than the two levels it knew about.

The `run_` half is driven through the same `FakeRowCatalog` the runtime's own tests use, which is
what lets one assertion here be about the *process* rather than the tool: that catalog implements
`RowWriter` and `EditLogWriter` and deliberately not `CatalogWriter`, so a server built over it
proves a serving process can change rows and no schema at all.
"""

import asyncio
import datetime as dt
import inspect
import json
from decimal import Decimal
from pathlib import Path

import pytest

from loom import build
from loom.action import APPLIED, CONFLICT, PREVIEWED, REFUSED, UNKNOWN_ACTOR, ActionRuntime
from loom.catalog.base import CatalogError, writer_for
from loom.config import LoomConfig, McpConfig
from loom.mcp.registry import DRY_RUN_ARG, PARAMETERS_ARG, RESERVED_RUN_ARGS, build_tools, json_safe, snake_case
from loom.mcp.server import LoomMCPServer, build_mcp_server, build_server
from loom.query.engine import Capabilities, CompiledQuery
from loom.resolver import MAX_PAGE_SIZE, Resolver

# The runtime's fakes, not a second pair of them: a fake catalog defined twice is two answers to
# "what does a catalog do", and the interesting assertions below are about the *same* object the
# action tests drive directly.
from test_action import CUSTOMERS, FakeRowCatalog, Interloper

VALID = Path(__file__).parent / "fixtures" / "valid"

# Anything an agent could use to smuggle in its own query.
FORBIDDEN_FIELDS = {"sql", "query", "where", "filter_expr", "expression", "predicate", "statement", "table", "column"}


class StubEngine:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def capabilities(self):
        return Capabilities(name="stub")

    def compile(self, plan):
        self.plan = plan
        return CompiledQuery(sql="<stub>")

    def execute(self, compiled):
        return self.rows


@pytest.fixture
def ontology():
    ont, _ = build(VALID)
    return ont


def _tools(ontology, rows=(), runtime=None, actor=None):
    resolver = Resolver(ontology=ontology, engine=StubEngine(rows))
    return {t.name: t for t in build_tools(resolver, runtime, actor)}


def _runtime(ontology, catalog=None):
    """A runtime over the row-writable fake. `rest_main` is the catalog the fixture binds."""
    return ActionRuntime(ontology=ontology, catalogs={"rest_main": catalog or FakeRowCatalog()})


def _objects(schema):
    """Every object schema inside one input schema, including the root.

    The forbidden-field check has to walk rather than look in two known places: `run_` tools nest
    the declared parameters, so a rule that only knew about `filter` would stop covering the surface
    exactly as the surface grew a write half."""
    found = [schema]
    for child in (schema.get("properties") or {}).values():
        if isinstance(child, dict) and child.get("type") == "object":
            found.extend(_objects(child))
    return found


def test_the_generated_tool_set_is_exactly_the_spec_contract(ontology):
    assert set(_tools(ontology)) == {
        "get_customer",
        "search_customer",
        "list_customer",
        "get_order",
        "search_order",
        "list_order",
        "traverse",
    }


def test_actions_are_not_exposed_unless_the_deployment_asked_for_them(ontology):
    """The spec declaring an action is not the same statement as a deployment serving it.

    Before this slice the fixture's actions produced no tools because none existed. They produce
    none here for a different and permanent reason: no runtime was supplied, which is what
    `mcp.writes: false` does. The surface is what the deployment permits."""
    assert ontology.actions
    assert not [name for name in _tools(ontology) if name.startswith("run_")]


def test_one_tool_per_action_named_from_the_api_name(ontology):
    tools = _tools(ontology, runtime=_runtime(ontology))
    assert {n for n in tools if n.startswith("run_")} == {
        "run_upgrade_tier",
        "run_create_order",
        "run_forget_customer",
    }
    assert len(tools) == 7 + len(ontology.actions)


def test_no_tool_can_take_a_query(ontology):
    """The framework's central claim, as an assertion — now over the write half too."""
    for name, tool in _tools(ontology, runtime=_runtime(ontology)).items():
        for schema in _objects(tool.input_schema):
            fields = set(schema.get("properties") or {})
            assert not (fields & FORBIDDEN_FIELDS), f"{name} exposes {fields & FORBIDDEN_FIELDS}"


def test_a_run_tools_top_level_is_only_loom_s_own_argument_names(ontology):
    """The other half of the no-query rule, and the one the write surface made necessary.

    A declared parameter can be named anything — including `table` or `query` — so the check above
    would fail on a spec nobody should be prevented from writing. What has to hold instead is that
    spec-derived names never reach the top level of a tool, where Loom's own arguments live and mean
    something. Nested, a parameter called `table` is a declared parameter of a declared action,
    typed and bound, and no more a query surface than a column name is."""
    for name, tool in _tools(ontology, runtime=_runtime(ontology)).items():
        if name.startswith("run_"):
            assert set(tool.input_schema["properties"]) == set(RESERVED_RUN_ARGS), name


def test_every_input_schema_is_closed(ontology):
    """additionalProperties: false, so an unexpected argument is rejected rather than ignored."""
    for name, tool in _tools(ontology, runtime=_runtime(ontology)).items():
        for schema in _objects(tool.input_schema):
            assert schema["additionalProperties"] is False, name


def test_tool_names_are_derived_from_api_names():
    assert snake_case("Customer") == "customer"
    assert snake_case("PurchaseOrder") == "purchase_order"
    assert snake_case("HTTPRequest") == "http_request"


def test_input_schemas_come_from_the_type_system(ontology):
    tools = _tools(ontology)
    # Customer's primary key is a string property.
    assert tools["get_customer"].input_schema["properties"]["key"]["type"] == "string"
    assert tools["get_customer"].input_schema["required"] == ["key"]
    # `tier` is an enum, so the agent is handed its declared values — in both filter spellings.
    tier = tools["search_customer"].input_schema["properties"]["filter"]["properties"]["tier"]
    bare, operators = tier["anyOf"]
    assert bare["enum"] == ["bronze", "silver", "gold"]
    assert operators["properties"]["eq"]["anyOf"][0]["enum"] == ["bronze", "silver", "gold"]


def test_the_operators_a_property_advertises_are_a_function_of_its_type(ontology):
    """An enum is a declared set, so it is testable and not orderable; a string adds substring."""
    props = _tools(ontology)["search_customer"].input_schema["properties"]["filter"]["properties"]
    assert set(props["tier"]["anyOf"][1]["properties"]) == {"eq", "ne"}
    assert set(props["name"]["anyOf"][1]["properties"]) == {
        "eq", "ne", "gt", "gte", "lt", "lte", "contains",
    }


def test_only_the_equality_operators_admit_a_null(ontology):
    """§5's 'null is a value you can test, not one you can order', readable in the schema."""
    name = _tools(ontology)["search_customer"].input_schema["properties"]["filter"]["properties"]["name"]
    operators = name["anyOf"][1]["properties"]
    assert {"type": "null"} in operators["eq"]["anyOf"]
    assert {"type": "null"} in operators["ne"]["anyOf"]
    assert "anyOf" not in operators["gte"] and operators["gte"] == {"type": "string"}


def test_an_operator_name_cannot_shadow_a_property_name(ontology):
    """§7's namespace rule, one level deeper: property names and operator names never share a
    level, so a spec may declare a property called `gte`."""
    filter_schema = _tools(ontology)["search_customer"].input_schema["properties"]["filter"]
    assert set(filter_schema["properties"]) <= set(ontology.object_types["Customer"].properties)
    for prop_schema in filter_schema["properties"].values():
        assert set(prop_schema["anyOf"][1]["properties"]) & set(filter_schema["properties"]) == set()


def test_search_exposes_only_declared_searchable_properties(ontology):
    """`ltv` is a real property but not searchable, so it is not part of the query surface."""
    props = _tools(ontology)["search_customer"].input_schema["properties"]["filter"]["properties"]
    assert set(props) == {"name", "tier"}


def test_descriptions_come_from_the_spec(ontology):
    tools = _tools(ontology)
    assert "by its customerId" in tools["get_customer"].description
    assert "orders -> Order (many_to_one)" in tools["traverse"].description


def test_paging_caps_are_advertised_in_the_schema(ontology):
    limit = _tools(ontology)["list_customer"].input_schema["properties"]["limit"]
    assert limit["maximum"] == MAX_PAGE_SIZE and limit["minimum"] == 1


def test_traverse_enumerates_valid_starting_types(ontology):
    schema = _tools(ontology)["traverse"].input_schema
    assert schema["properties"]["objectType"]["enum"] == ["Customer", "Order"]
    assert schema["required"] == ["objectType", "key", "link"]


def test_no_traverse_tool_without_links():
    """A link-free ontology shouldn't advertise a tool that can't do anything."""
    ont, _ = build(VALID)
    linkless = type(ont)(object_types=ont.object_types, link_types={}, actions=ont.actions)
    resolver = Resolver(ontology=linkless, engine=StubEngine())
    assert "traverse" not in {t.name for t in build_tools(resolver)}


# ---- results -------------------------------------------------------------------


def test_get_result_distinguishes_missing_from_empty(ontology):
    found = _tools(ontology, rows=[{"customerId": "c1"}])["get_customer"].handler({"key": "c1"})
    assert found["found"] is True and found["object"] == {"customerId": "c1"}

    missing = _tools(ontology, rows=[])["get_customer"].handler({"key": "zzz"})
    assert missing["found"] is False and missing["object"] is None


def test_paged_results_tell_the_agent_whether_to_keep_going(ontology):
    """Without hasMore, "the page filled up" and "that's everything" look identical."""
    two_rows = [{"customerId": "c1"}, {"customerId": "c2"}]
    full = _tools(ontology, rows=two_rows)["list_customer"].handler({"limit": 2})
    assert full["count"] == 2 and full["hasMore"] is True

    partial = _tools(ontology, rows=two_rows)["list_customer"].handler({"limit": 5})
    assert partial["count"] == 2 and partial["hasMore"] is False


def test_traverse_result_names_the_type_it_returned(ontology):
    result = _tools(ontology, rows=[{"orderId": "o1"}])["traverse"].handler(
        {"objectType": "Customer", "key": "c2", "link": "orders"}
    )
    assert result["targetObjectType"] == "Order"
    assert result["cardinality"] == "many_to_one"
    assert result["objects"] == [{"orderId": "o1"}]


def test_decimals_survive_as_strings_not_floats():
    """The reason to declare decimal(12,2) is that the value must not go through a float."""
    assert json_safe(Decimal("1299.99")) == "1299.99"
    assert json.loads(json.dumps(json_safe({"total": Decimal("0.10")})))["total"] == "0.10"


def test_temporal_values_are_iso_encoded():
    assert json_safe(dt.date(2026, 3, 2)) == "2026-03-02"
    assert json_safe(dt.datetime(2026, 3, 2, 12, 30, tzinfo=dt.UTC)) == "2026-03-02T12:30:00+00:00"


def test_results_are_json_serializable(ontology):
    rows = [{"total": Decimal("17.50"), "placedAt": dt.datetime(2026, 3, 9, tzinfo=dt.UTC)}]
    result = _tools(ontology, rows=rows)["list_order"].handler({})
    json.dumps(result)  # must not raise


# ---- dispatch ------------------------------------------------------------------


def test_server_dispatch_returns_json(ontology):
    resolver = Resolver(ontology=ontology, engine=StubEngine([{"customerId": "c1"}]))
    server = LoomMCPServer.from_resolver(resolver)
    text, is_error = server.call("get_customer", {"key": "c1"})
    assert is_error is False
    assert json.loads(text)["object"] == {"customerId": "c1"}


def test_a_usage_error_comes_back_as_recoverable_content(ontology):
    """The message already names the valid alternatives, which is what an agent needs to retry."""
    resolver = Resolver(ontology=ontology, engine=StubEngine())
    server = LoomMCPServer.from_resolver(resolver)
    text, is_error = server.call("traverse", {"objectType": "Customer", "key": "c1", "link": "nope"})
    assert is_error is True
    assert "available: orders" in text


def test_an_unknown_tool_name_lists_the_real_ones(ontology):
    resolver = Resolver(ontology=ontology, engine=StubEngine())
    server = LoomMCPServer.from_resolver(resolver)
    text, is_error = server.call("run_sql", {"sql": "select 1"})
    assert is_error is True
    assert "unknown tool 'run_sql'" in text and "get_customer" in text


# ---- run_<action>: shape ---------------------------------------------------------


def test_run_input_schemas_come_from_the_declared_parameters(ontology):
    """The whole argument for one tool per action: the schema *is* the action.

    `upgradeTier` takes an objectRef and an enum of two values; `createOrder` takes a string, a
    string and a `decimal(12,2)`. One generic `run(action, params)` could type neither."""
    tools = _tools(ontology, runtime=_runtime(ontology))

    upgrade = tools["run_upgrade_tier"].input_schema["properties"][PARAMETERS_ARG]
    assert upgrade["properties"]["newTier"]["enum"] == ["silver", "gold"]
    assert upgrade["properties"]["customer"]["description"] == "key of a Customer"
    assert sorted(upgrade["required"]) == ["customer", "newTier"]

    create = tools["run_create_order"].input_schema["properties"][PARAMETERS_ARG]
    # A decimal travels as a string, for the reason it was declared a decimal at all.
    assert create["properties"]["total"] == {"type": "string", "description": "decimal(12,2)"}


def test_dry_run_sits_beside_the_parameters_not_among_them(ontology):
    """Spec-derived names in a nested object, Loom's own names at the top — the rule that makes a
    parameter called `dryRun` impossible to collide with."""
    schema = _tools(ontology, runtime=_runtime(ontology))["run_upgrade_tier"].input_schema
    assert schema["properties"][DRY_RUN_ARG]["type"] == "boolean"
    assert DRY_RUN_ARG not in schema["properties"][PARAMETERS_ARG]["properties"]
    assert schema["required"] == [PARAMETERS_ARG]


def test_run_descriptions_come_from_the_spec_and_say_what_to_branch_on(ontology):
    description = _tools(ontology, runtime=_runtime(ontology))["run_upgrade_tier"].description
    assert description.startswith("Raise a customer to a higher membership tier.")
    assert "Modifies exactly one Customer, addressed by customerId" in description
    # The input schema cannot carry this, and an agent that doesn't know it will read a refusal as
    # a broken call.
    assert "`status`" in description and "`failures[].code`" in description


def test_a_non_active_element_is_labelled_rather_than_hidden(ontology):
    """`status` is read for the first time here. Hiding a deprecated action would leave `loom run`
    able to run something the tool surface denies — the back door `loom run` exists to not be."""
    from dataclasses import replace

    actions = dict(ontology.actions)
    actions["upgradeTier"] = replace(actions["upgradeTier"], status="deprecated")
    objects = dict(ontology.object_types)
    objects["Order"] = replace(objects["Order"], status="experimental")
    labelled = type(ontology)(object_types=objects, link_types=ontology.link_types, actions=actions)

    tools = _tools(labelled, runtime=_runtime(labelled))
    assert tools["run_upgrade_tier"].description.startswith("DEPRECATED — ")
    assert tools["get_order"].description.startswith("EXPERIMENTAL — ")
    # Still there, and still runnable. The label is the mechanism, not the absence.
    assert "run_upgrade_tier" in tools and "get_order" in tools


# ---- run_<action>: dispatch ------------------------------------------------------


def _server(ontology, catalog, actor=None):
    resolver = Resolver(ontology=ontology, engine=StubEngine())
    runtime = ActionRuntime(ontology=ontology, catalogs={"rest_main": catalog})
    return LoomMCPServer.from_resolver(resolver, runtime=runtime, actor=actor)


def _call(server, name, args):
    text, is_error = server.call(name, args)
    return json.loads(text), is_error


def test_a_run_through_the_tool_writes_the_row(ontology):
    catalog = FakeRowCatalog()
    payload, is_error = _call(
        _server(ontology, catalog),
        "run_upgrade_tier",
        {PARAMETERS_ARG: {"customer": "c2", "newTier": "gold"}},
    )
    assert is_error is False
    assert payload["status"] == APPLIED
    assert payload["after"]["tier"] == "gold"
    assert catalog.row("crm.customers", "id", "c2")["tier"] == "gold"
    # Carried across, not nulled — the full-row read reaching an MCP caller unchanged.
    assert catalog.row("crm.customers", "id", "c2")["region"] == "amer"


def test_a_dry_run_previews_and_writes_nothing(ontology):
    catalog = FakeRowCatalog()
    payload, is_error = _call(
        _server(ontology, catalog),
        "run_upgrade_tier",
        {PARAMETERS_ARG: {"customer": "c2", "newTier": "gold"}, DRY_RUN_ARG: True},
    )
    assert is_error is False
    assert payload["status"] == PREVIEWED
    assert payload["after"]["tier"] == "gold"
    assert catalog.row("crm.customers", "id", "c2")["tier"] == "silver"
    assert catalog.writes == []
    # A preview holds nothing, and the result says so rather than printing a bare snapshot id.
    assert payload["concurrency"].startswith("not checked")
    assert payload["editId"] == "" and catalog.edits == []


def test_a_refusal_is_not_a_protocol_error(ontology):
    """The rule: `isError` answers "did this call become a run?", not "did the run succeed?".

    A validation rule returning false is the precondition doing its job. Flagging it would tell an
    agent it used the tool wrong when it used the tool exactly right."""
    payload, is_error = _call(
        _server(ontology, FakeRowCatalog()),
        "run_upgrade_tier",
        {PARAMETERS_ARG: {"customer": "c1", "newTier": "gold"}},  # c1 is already gold
    )
    assert is_error is False
    assert payload["status"] == REFUSED
    assert [f["code"] for f in payload["failures"]] == ["validation_failed"]
    # The spec author's own sentence, verbatim, over the wire.
    assert payload["failures"][0]["message"] == "New tier must differ from current tier"
    assert "retryable" not in payload["failures"][0]


def test_a_conflict_arrives_as_content_and_says_it_is_retryable(ontology):
    """The one retryable code. A boolean `isError` could not have said so."""
    from loom.action import MAX_ATTEMPTS

    catalog = Interloper(FakeRowCatalog(), strike_on=range(1, MAX_ATTEMPTS + 1))
    payload, is_error = _call(
        _server(ontology, catalog),
        "run_upgrade_tier",
        {PARAMETERS_ARG: {"customer": "c2", "newTier": "gold"}},
    )
    assert is_error is False
    assert payload["status"] == REFUSED
    failure = next(f for f in payload["failures"] if f["code"] == CONFLICT)
    assert failure["retryable"] is True
    assert failure["detail"]["attempts"] == MAX_ATTEMPTS
    assert failure["detail"]["contended"] is False  # `region` moved; the action reads neither


def test_an_applied_run_that_could_not_be_logged_is_still_not_an_error(ontology):
    """The shape a boolean gets actively wrong: the write committed and a failure sits beside it.

    `isError=True` would tell a caller the write did not happen."""
    catalog = FakeRowCatalog(log_fails=True)
    payload, is_error = _call(
        _server(ontology, catalog),
        "run_upgrade_tier",
        {PARAMETERS_ARG: {"customer": "c2", "newTier": "gold"}},
    )
    assert is_error is False
    assert payload["status"] == APPLIED
    assert [f["code"] for f in payload["failures"]] == ["log_failed"]
    assert catalog.row("crm.customers", "id", "c2")["tier"] == "gold"


def test_a_call_that_never_became_a_run_is_an_error(ontology):
    """The other side of the same rule. An `ActionError` — a catalog the config never bound — takes
    the path a `ResolverError` takes: content an agent can read, flagged, and nothing recorded."""
    resolver = Resolver(ontology=ontology, engine=StubEngine())
    runtime = ActionRuntime(ontology=ontology, catalogs={})
    server = LoomMCPServer.from_resolver(resolver, runtime=runtime)
    text, is_error = server.call("run_upgrade_tier", {PARAMETERS_ARG: {"customer": "c2", "newTier": "gold"}})
    assert is_error is True
    assert "not declared in loom.yaml" in text


def test_an_undeclared_parameter_is_a_refusal_rather_than_a_schema_crash(ontology):
    """The schema says `additionalProperties: false`, but the runtime is not entitled to assume a
    client enforced it — so an unknown parameter is a typed refusal, not an exception."""
    payload, is_error = _call(
        _server(ontology, FakeRowCatalog()),
        "run_upgrade_tier",
        {PARAMETERS_ARG: {"customer": "c2", "newTier": "gold", "sql": "drop table"}},
    )
    assert is_error is False
    assert payload["status"] == REFUSED
    assert [f["code"] for f in payload["failures"]] == ["unknown_parameter"]


# ---- what a serving process is ---------------------------------------------------


def test_a_served_run_records_the_actor_the_deployment_declared(ontology):
    catalog = FakeRowCatalog()
    _call(
        _server(ontology, catalog, actor="agent:support-bot"),
        "run_upgrade_tier",
        {PARAMETERS_ARG: {"customer": "c2", "newTier": "gold"}},
    )
    assert [e["actor"] for e in catalog.edits] == ["agent:support-bot"]
    # And into the commit that changed the row, which is the attribution that is atomic with it.
    assert catalog.commits[("crm.customers", 2)]["loom.actor"] == "agent:support-bot"


def test_without_a_declared_actor_a_served_run_says_unknown(ontology):
    """Not `default_actor()`, which would name whoever started the process. stdio authenticates
    nobody, and a log that says it does not know beats one that confidently names the wrong
    principal."""
    catalog = FakeRowCatalog()
    _call(
        _server(ontology, catalog),
        "run_upgrade_tier",
        {PARAMETERS_ARG: {"customer": "c2", "newTier": "gold"}},
    )
    assert [e["actor"] for e in catalog.edits] == [UNKNOWN_ACTOR]
    # Everything else the record exists for is still there. The gap is the transport's, not the log's.
    recorded = catalog.edits[0]
    assert recorded["action"] == "upgradeTier" and recorded["object_key"] == "c2"
    assert recorded["status"] == APPLIED and json.loads(recorded["parameters"])["newTier"] == "gold"


def test_a_serving_process_can_change_rows_and_no_schema_at_all(ontology):
    """The claim that replaced M3's sentence about handles, and the one a fake can prove.

    `FakeRowCatalog` implements the read port, `RowWriter` and `EditLogWriter`, and deliberately not
    `CatalogWriter`. A server built over it serves every tool and writes a row — so nothing the tool
    surface reaches ever asked for a schema verb. No real catalog can demonstrate this, because a
    real one implements every port at once."""
    catalog = FakeRowCatalog()
    server = _server(ontology, catalog)

    with pytest.raises(CatalogError) as excinfo:
        writer_for(catalog)
    assert "does not support schema writes" in str(excinfo.value)

    _, is_error = _call(
        server, "run_upgrade_tier", {PARAMETERS_ARG: {"customer": "c2", "newTier": "gold"}}
    )
    assert is_error is False
    assert catalog.row("crm.customers", "id", "c2")["tier"] == "gold"


def test_build_server_builds_no_runtime_unless_mcp_writes_is_on(ontology):
    """The default is the read-only process M1 shipped, with no runtime in it at all."""
    catalogs = {"rest_main": FakeRowCatalog(rows=CUSTOMERS)}
    off = LoomConfig(mcp=McpConfig(name="loom"))
    server, _ = build_server(ontology, off, catalogs)
    assert not [n for n in server.tools if n.startswith("run_")]

    on = LoomConfig(mcp=McpConfig(name="loom", writes=True, actor="ci"))
    server, _ = build_server(ontology, on, catalogs)
    assert sorted(n for n in server.tools if n.startswith("run_")) == [
        "run_create_order",
        "run_forget_customer",
        "run_upgrade_tier",
    ]


# ---- what a second transport must not change ------------------------------------


def test_nothing_the_server_dispatches_can_yield_the_event_loop(ontology):
    """**Why an HTTP `loom serve` answers one call at a time**, as an assertion rather than a hope.

    The MCP SDK dispatches `on_call_tool` concurrently — two clients on one HTTP server genuinely
    interleave, which was measured before this was decided, so nothing above Loom is holding this
    line. What serializes a served process is one rung down and entirely structural: dispatch is a
    plain function and so is every handler, so a call runs to completion without a suspension point
    in it and the event loop has nowhere to switch. A synchronous callable *cannot* be interleaved;
    this is a proof, not a convention.

    It is asserted because it is load-bearing and invisible. `DuckDBEngine` holds one connection for
    the process and registers every scan under `t0` / `t1` / `m0` — constants in `resolver.py`,
    identical for every object type in every ontology — so two concurrent reads clobber each other's
    relation and the loser answers with the winner's rows. `build_server` builds one `Resolver` and
    one `ActionRuntime` for the process. None of that is the transport's to fix, and all of it is
    fine for exactly as long as this test passes.

    So: make a handler `async` and this fails, which is the point. What it is telling you is not
    "undo it" but "the three things above are now a correctness problem rather than a performance
    one, and they belong to the layers that own them"."""
    assert not inspect.iscoroutinefunction(LoomMCPServer.call)
    for name, tool in _tools(ontology, runtime=_runtime(ontology)).items():
        assert not inspect.iscoroutinefunction(tool.handler), name
        assert not inspect.isasyncgenfunction(tool.handler), name


def test_both_transports_are_handed_one_server(ontology):
    """§7's surface is a function of the spec, and a transport is not one of its inputs.

    `serve_stdio` and `serve_http` differ only in where they get a pair of streams; the tool set,
    the descriptions and the instructions come from `build_mcp_server` either way. Asserting it here
    — with no socket in sight — is what makes the claim about *both* transports rather than about
    whichever one a test happened to drive."""
    pytest.importorskip("mcp", reason="needs the [mcp] extra")

    loom_server = _server(ontology, FakeRowCatalog())
    sdk_server = build_mcp_server(loom_server)

    on_list_tools = sdk_server.get_request_handler("tools/list").handler
    listed = asyncio.run(on_list_tools(None, None))
    assert {t.name for t in listed.tools} == set(loom_server.tools)
    for advertised in listed.tools:
        built = loom_server.tools[advertised.name]
        assert advertised.description == built.description
        assert advertised.input_schema == built.input_schema
    assert "There is no SQL interface." in (sdk_server.instructions or "")
