"""The generated agent surface.

These tests import no MCP SDK and open no transport — that's the reason the registry is separate
from the server. The load-bearing assertion is `test_no_tool_can_take_a_query`: the framework's
central claim is that an ontology compiles to typed verbs, so if a raw-SQL escape hatch ever
appears in the generated surface, it fails here.
"""

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest

from loom import build
from loom.mcp.registry import build_tools, json_safe, snake_case
from loom.mcp.server import LoomMCPServer
from loom.query.engine import Capabilities, CompiledQuery
from loom.resolver import MAX_PAGE_SIZE, Resolver

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


def _tools(ontology, rows=()):
    resolver = Resolver(ontology=ontology, engine=StubEngine(rows))
    return {t.name: t for t in build_tools(resolver)}


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


def test_no_action_tools_yet(ontology):
    """The fixture declares two actions; the write surface arrives with the action runtime."""
    assert ontology.actions
    assert not [name for name in _tools(ontology) if name.startswith("run_")]


def test_no_tool_can_take_a_query(ontology):
    """The framework's central claim, as an assertion."""
    for name, tool in _tools(ontology).items():
        fields = set(tool.input_schema.get("properties", {}))
        nested = tool.input_schema["properties"].get("filter", {}).get("properties", {})
        assert not (fields & FORBIDDEN_FIELDS), f"{name} exposes {fields & FORBIDDEN_FIELDS}"
        assert not (set(nested) & FORBIDDEN_FIELDS), f"{name}.filter exposes a query field"


def test_every_input_schema_is_closed(ontology):
    """additionalProperties: false, so an unexpected argument is rejected rather than ignored."""
    for name, tool in _tools(ontology).items():
        assert tool.input_schema["additionalProperties"] is False, name


def test_tool_names_are_derived_from_api_names():
    assert snake_case("Customer") == "customer"
    assert snake_case("PurchaseOrder") == "purchase_order"
    assert snake_case("HTTPRequest") == "http_request"


def test_input_schemas_come_from_the_type_system(ontology):
    tools = _tools(ontology)
    # Customer's primary key is a string property.
    assert tools["get_customer"].input_schema["properties"]["key"]["type"] == "string"
    assert tools["get_customer"].input_schema["required"] == ["key"]
    # `tier` is an enum, so the agent is handed its declared values.
    tier = tools["search_customer"].input_schema["properties"]["filter"]["properties"]["tier"]
    assert tier["enum"] == ["bronze", "silver", "gold"]


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
