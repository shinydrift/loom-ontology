"""End to end over a real Iceberg table — M1's definition of done.

Seeds a local Iceberg warehouse, then reads it back through the whole stack: catalog port ->
pyiceberg -> Arrow -> DuckDB SQL -> resolver -> MCP tool dispatch. Nothing is stubbed.

It runs the *shipped example* rather than a bespoke fixture, copied to a tmp dir, so a broken
`examples/retail` fails CI instead of quietly rotting.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from datetime import UTC
from decimal import Decimal
from pathlib import Path

import pytest

from loom import build
from loom.config import find_config, load_config
from loom.errors import Diagnostics

pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
pytest.importorskip("duckdb", reason="needs the [duckdb] extra")

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "retail"


def _load_seed_module(path: Path):
    spec = importlib.util.spec_from_file_location("retail_seed", path / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    """A seeded copy of examples/retail, with its ontology and config loaded."""
    root = tmp_path_factory.mktemp("retail")
    target = root / "retail"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns(".warehouse"))

    _load_seed_module(target).seed(target)

    diag = Diagnostics()
    config = load_config(find_config(target / "ontology"), diag)
    ontology, _ = build(target / "ontology")
    diag.raise_if_errors()
    # The directory comes back too, for the one test that drives `loom query` rather than the API.
    return ontology, config, target


@pytest.fixture(scope="module")
def catalogs(project):
    from loom.catalog import open_catalogs

    return open_catalogs(project[1])


@pytest.fixture(scope="module")
def resolver(project, catalogs):
    from loom.resolver import build_resolver

    return build_resolver(project[0], project[1], catalogs)


# ---- catalog + physical validation ---------------------------------------------


def test_the_example_validates_against_its_own_warehouse(project, catalogs):
    """The spec and the seeded tables genuinely agree — the contract holds on real metadata."""
    from loom.loader import load_dir
    from loom.validator import check_physical, validate

    ontology, config, _ = project
    diag = Diagnostics()
    loaded = load_dir(Path(config.source).parent / "ontology", diag)
    validate(loaded, diag)
    check_physical(loaded, catalogs, diag)
    assert [e.render() for e in diag.errors] == []
    assert [w.render() for w in diag.warnings] == []


def test_catalog_introspection_reports_real_iceberg_types(catalogs):
    schema = catalogs["local"].describe("sales.orders")
    assert {c.name: c.iceberg_type for c in schema.columns.values()} == {
        "id": "string",
        "customer_id": "string",
        # Normalized to the spelling PropType.iceberg_type() produces — pyiceberg says
        # "decimal(12, 2)" with a space.
        "total_amount": "decimal(12,2)",
        "created_at": "timestamptz",
    }
    assert schema.columns["id"].required is True


def test_daily_sales_performance_is_a_physical_precomputed_table(catalogs):
    rows = catalogs["local"].scan("sales.daily_sales_performance").to_pylist()
    assert len(rows) == 6
    feb_11 = next(row for row in rows if str(row["sales_date"]) == "2026-02-11")
    assert feb_11["gross_sales"] == Decimal("450.00")
    assert feb_11["order_count"] == 1
    assert feb_11["unique_customers"] == 1
    assert feb_11["source_table"] == "sales.orders"
    assert isinstance(feb_11["source_snapshot_id"], int)


def test_physical_validation_catches_a_spec_that_drifts_from_the_table(project, catalogs):
    """Rename a column in the spec only, and the pass says so."""
    from loom.loader import load_dir
    from loom.validator import check_physical

    _, config, _ = project
    ontology_dir = Path(config.source).parent / "ontology"
    original = (ontology_dir / "customer.yaml").read_text()
    try:
        (ontology_dir / "customer.yaml").write_text(original.replace("column: full_name", "column: fullname"))
        diag = Diagnostics()
        check_physical(load_dir(ontology_dir, diag), catalogs, diag)
        messages = " | ".join(e.message for e in diag.errors)
        assert "maps to column 'fullname', which does not exist" in messages
    finally:
        (ontology_dir / "customer.yaml").write_text(original)


# ---- reads ---------------------------------------------------------------------


def test_get_returns_a_real_row(resolver):
    assert resolver.get("Customer", "c1") == {
        "customerId": "c1",
        "name": "Ada Lovelace",
        "tier": "gold",
        "ltv": 48210.50,
    }


def test_get_a_missing_key_is_none(resolver):
    assert resolver.get("Customer", "nobody") is None


def test_get_preserves_decimal_and_timestamp_types(resolver):
    from datetime import datetime
    from decimal import Decimal

    order = resolver.get("Order", "o1")
    assert order["total"] == Decimal("1299.99")
    assert order["placedAt"] == datetime(2026, 1, 4, 12, 0, tzinfo=UTC)


def test_list_is_ordered_by_primary_key(resolver):
    assert [c["customerId"] for c in resolver.list("Customer")] == ["c1", "c2", "c3", "c4"]


def test_search_substring_is_case_insensitive(resolver):
    """`name` is declared searchable, so "LOVE" finds "Ada Lovelace"."""
    assert [c["customerId"] for c in resolver.search("Customer", {"name": "LOVE"})] == ["c1"]


def test_search_matches_a_substring_across_rows(resolver):
    assert [c["customerId"] for c in resolver.search("Customer", {"name": "ace"})] == ["c1", "c2"]


def test_search_on_an_enum_is_exact(resolver):
    assert [c["customerId"] for c in resolver.search("Customer", {"tier": "gold"})] == ["c1"]


def test_search_filters_are_anded(resolver):
    assert resolver.search("Customer", {"name": "ace", "tier": "silver"}) == [
        {"customerId": "c2", "name": "Grace Hopper", "tier": "silver", "ltv": 12750.0}
    ]


def test_a_date_range_selects_a_month(resolver):
    """M7's acceptance case, and the query this object type exists to answer. Not expressible at
    all before typed filters: `salesDate` is a date, which `searchable` could not even name."""
    rows = resolver.search(
        "DailySalesPerformance", {"salesDate": {"gte": "2026-02-01", "lt": "2026-03-01"}}
    )
    assert [str(r["salesDate"]) for r in rows] == ["2026-02-11", "2026-02-14"]


def test_a_range_and_a_substring_are_anded_across_properties(resolver):
    rows = resolver.search(
        "DailySalesPerformance",
        {"salesDate": {"gte": "2026-03-01"}, "sourceTable": {"eq": "sales.orders"}},
    )
    assert [str(r["salesDate"]) for r in rows] == ["2026-03-02", "2026-03-09", "2026-03-17"]


def test_an_open_range_over_a_decimal_compares_as_a_decimal(resolver):
    """`grossSales` is `decimal(14,2)`; the bound is coerced before it reaches the engine."""
    rows = resolver.search("DailySalesPerformance", {"grossSales": {"gt": "2000.00"}})
    assert [str(r["salesDate"]) for r in rows] == ["2026-03-02"]


def test_an_ordering_comparison_does_not_return_a_null_row(resolver):
    """c3 has no `ltv`. Undecided is not admitted — and with no negation in this grammar, that is
    also exactly SQL's answer, which is why the two can't disagree."""
    assert [c["customerId"] for c in resolver.search("Customer", {"ltv": {"gt": 0}})] == ["c1", "c2", "c4"]
    assert [c["customerId"] for c in resolver.search("Customer", {"ltv": {"eq": None}})] == ["c3"]


def test_membership_selects_the_union_of_its_values(resolver):
    """The query that cost N calls before: one filter, several values, over real data."""
    rows = resolver.search("Customer", {"tier": {"in": ["gold", "silver"]}})
    assert [c["customerId"] for c in rows] == ["c1", "c2"]


def test_membership_of_one_value_is_the_equality_it_abbreviates(resolver):
    """Asserted over the warehouse as well as in the grammar, because the null is the case where
    SQL's own `IN` would disagree — and c3 is the row that shows it."""
    for value in ("gold", None):
        assert resolver.search("Customer", {"tier": {"in": [value]}}) == resolver.search(
            "Customer", {"tier": {"eq": value}}
        )
    assert [c["customerId"] for c in resolver.search("Customer", {"ltv": {"in": [None]}})] == ["c3"]


def test_membership_and_another_filter_are_still_anded(resolver):
    rows = resolver.search("Customer", {"tier": {"in": ["gold", "silver"]}, "name": "ace"})
    assert [c["customerId"] for c in rows] == ["c1", "c2"]


def test_nullable_property_comes_back_as_none(resolver):
    assert resolver.get("Customer", "c3")["ltv"] is None


def test_paging_is_stable_across_pages(resolver):
    """The ORDER BY is what makes this true; without it page 2 could repeat page 1."""
    page1 = resolver.list("Customer", limit=2, offset=0)
    page2 = resolver.list("Customer", limit=2, offset=2)
    assert [c["customerId"] for c in page1] == ["c1", "c2"]
    assert [c["customerId"] for c in page2] == ["c3", "c4"]


# ---- traversal -----------------------------------------------------------------


def test_the_cli_pages_with_the_offset_its_own_refusal_names(project, capsys):
    """`loom query` mirrors the generated tools, and had no `--offset` while every paged tool did —
    so the refusal above the page cap named a flag the command did not take, and row 501 of anything
    was unreachable from the CLI. Driven through `main` rather than the resolver, because the
    missing half was the argument and not the read."""
    from loom.cli import main

    ontology = str(project[2] / "ontology")
    assert main(["query", "Customer", ontology, "--limit", "2", "--offset", "0"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(["query", "Customer", ontology, "--limit", "2", "--offset", "2"]) == 0
    second = json.loads(capsys.readouterr().out)

    assert [r["customerId"] for r in first] == ["c1", "c2"]
    assert [r["customerId"] for r in second] == ["c3", "c4"]


def test_reverse_traverse_returns_the_linked_objects(resolver):
    """Customer -> orders, via the link's reverseName."""
    orders = resolver.traverse("Customer", "c2", "orders")
    assert [o["orderId"] for o in orders] == ["o3", "o4", "o5"]


def test_forward_traverse_joins_back_to_the_one_side(resolver):
    """Order -> placedBy. The join column is not Order's primary key, so this is a real JOIN."""
    assert [c["customerId"] for c in resolver.traverse("Order", "o3", "placedBy")] == ["c2"]
    assert resolver.traverse("Order", "o3", "placedBy")[0]["name"] == "Grace Hopper"


def test_traverse_from_an_object_with_no_links_is_empty_not_an_error(resolver):
    assert resolver.traverse("Customer", "c3", "orders") == []


def test_traverse_of_a_nonexistent_anchor_is_empty(resolver):
    assert resolver.traverse("Customer", "nobody", "orders") == []


def test_traverse_pages(resolver):
    assert [o["orderId"] for o in resolver.traverse("Customer", "c2", "orders", limit=2)] == ["o3", "o4"]
    assert [o["orderId"] for o in resolver.traverse("Customer", "c2", "orders", limit=2, offset=2)] == ["o5"]


# ---- the MCP surface over real data --------------------------------------------


def test_the_generated_tools_answer_from_the_warehouse(project, catalogs):
    from loom.mcp.server import build_server

    server, _ = build_server(project[0], project[1], catalogs)
    assert set(server.tools) == {
        "get_customer",
        "search_customer",
        "list_customer",
        "get_order",
        "search_order",
        "list_order",
        "get_support_ticket",
        "search_support_ticket",
        "list_support_ticket",
        "match_support_ticket",
        "get_daily_sales_performance",
        "search_daily_sales_performance",
        "list_daily_sales_performance",
        "traverse",
    }

    text, is_error = server.call("get_customer", {"key": "c1"})
    assert is_error is False
    assert json.loads(text)["object"]["name"] == "Ada Lovelace"

    text, is_error = server.call("get_daily_sales_performance", {"key": "2026-03-02"})
    performance = json.loads(text)["object"]
    assert is_error is False
    assert performance["grossSales"] == "2100.00"
    assert performance["orderCount"] == 1
    assert performance["sourceTable"] == "sales.orders"


def test_the_date_range_answers_through_the_generated_tool(project, catalogs):
    """The same acceptance query an agent would send, through tool dispatch and its schema."""
    from loom.mcp.server import build_server

    server, _ = build_server(project[0], project[1], catalogs)
    schema = server.tools["search_daily_sales_performance"].input_schema
    operators = schema["properties"]["filter"]["properties"]["salesDate"]["anyOf"][1]["properties"]
    assert set(operators) == {"eq", "ne", "in", "gt", "gte", "lt", "lte"}

    text, is_error = server.call(
        "search_daily_sales_performance",
        {"filter": {"salesDate": {"gte": "2026-02-01", "lt": "2026-03-01"}}},
    )
    payload = json.loads(text)
    assert is_error is False
    assert [row["salesDate"] for row in payload["objects"]] == ["2026-02-11", "2026-02-14"]
    assert payload["hasMore"] is False


def test_loom_query_takes_the_same_range_the_tool_takes(project, capsys):
    """The dev command mirrors the generated tools, in the one encoding a shell has."""
    from loom.cli import main

    argv = [
        "query",
        "DailySalesPerformance",
        str(project[2] / "ontology"),
        "--filter",
        "salesDate.gte=2026-02-01",
        "--filter",
        "salesDate.lt=2026-03-01",
    ]
    assert main(argv) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["salesDate"] for row in rows] == ["2026-02-11", "2026-02-14"]


def test_loom_query_builds_a_membership_list_by_repeating_the_flag(project, capsys):
    """No separator, because a comma is a legal character in a string value — splitting on one
    would either forbid it or turn a single value into two wrong ones, silently."""
    from loom.cli import main

    argv = [
        "query",
        "Customer",
        str(project[2] / "ontology"),
        "--filter",
        "tier.in=gold",
        "--filter",
        "tier.in=silver",
    ]
    assert main(argv) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["customerId"] for row in rows] == ["c1", "c2"]


def test_an_empty_membership_list_is_refused_through_the_tool(project, catalogs):
    """The second refusal this grammar makes, as an `isError` an agent can act on rather than as
    an empty page it cannot tell from a search that found nothing."""
    from loom.mcp.server import build_server

    server, _ = build_server(project[0], project[1], catalogs)
    text, is_error = server.call("search_customer", {"filter": {"tier": {"in": []}}})
    assert is_error is True
    assert "matches no row" in text


def test_a_bare_null_filter_is_refused_through_the_tool(project, catalogs):
    """An `isError` result an agent can act on, naming the spelling that means what it asked."""
    from loom.mcp.server import build_server

    server, _ = build_server(project[0], project[1], catalogs)
    text, is_error = server.call("search_customer", {"filter": {"name": None}})
    assert is_error is True
    assert "a bare null is not a filter value" in text
    assert '{"eq": null}' in text


def test_a_tool_call_carries_money_as_a_string(project, catalogs):
    from loom.mcp.server import build_server

    server, _ = build_server(project[0], project[1], catalogs)
    text, _ = server.call("get_order", {"key": "o1"})
    assert json.loads(text)["object"]["total"] == "1299.99"


def test_traverse_tool_walks_the_link(project, catalogs):
    from loom.mcp.server import build_server

    server, _ = build_server(project[0], project[1], catalogs)
    text, is_error = server.call("traverse", {"objectType": "Customer", "key": "c2", "link": "orders"})
    payload = json.loads(text)
    assert is_error is False
    assert payload["targetObjectType"] == "Order"
    assert [o["orderId"] for o in payload["objects"]] == ["o3", "o4", "o5"]


def test_an_agent_sending_a_key_as_the_wrong_json_type_still_gets_its_row(project, catalogs):
    """`total` is a decimal; a filter arriving as a JSON number must not be compared as a float."""
    from loom.mcp.server import build_server

    server, _ = build_server(project[0], project[1], catalogs)
    text, is_error = server.call("search_order", {"filter": {"orderId": "o1"}})
    assert is_error is False
    assert json.loads(text)["count"] == 1


# ---- governance, with nothing stubbed -------------------------------------------


def test_a_policy_withholds_from_both_callers_alike(project, catalogs):
    """M5's claim at the only altitude that settles it: real Iceberg, real Arrow, real DuckDB SQL,
    real tool dispatch, and the same property missing from both answers.

    The `loom query` half is the resolver — the object *is* the same object, because the
    withholding happens below both callers rather than being applied twice — and the SQL is the
    reason it cannot be otherwise: `lifetime_value` is not in the SELECT, so there is no result set
    for a surface to filter, forget to filter, or be talked out of filtering."""
    from dataclasses import replace

    from loom.governance import Policy
    from loom.mcp.server import build_server
    from loom.resolver import build_resolver

    ontology, config, _ = project
    governed = replace(config, policies=(Policy(name="hide-ltv", object_type="Customer", mask=("ltv",)),))

    resolver = build_resolver(ontology, governed, catalogs)
    direct = resolver.get("Customer", "c1")
    assert direct == {"customerId": "c1", "name": "Ada Lovelace", "tier": "gold"}

    server, _ = build_server(ontology, governed, catalogs)
    text, is_error = server.call("get_customer", {"key": "c1"})
    assert is_error is False
    payload = json.loads(text)
    assert payload["object"] == direct
    assert payload["masked"] == ["ltv"]
    assert "lifetime_value" not in text

    # And the ungoverned build of the same project still reads it, so the absence above is the
    # policy rather than a property that was never there.
    assert build_resolver(ontology, config, catalogs).get("Customer", "c1")["ltv"] == 48210.5


class _Spy:
    """The real engine, with a note of what it was asked to compile. Wrapping rather than faking:
    the SQL asserted below is the SQL DuckDB actually ran."""

    def __init__(self, inner):
        self.inner = inner
        self.compiled = []

    def capabilities(self):
        return self.inner.capabilities()

    def compile(self, plan):
        compiled = self.inner.compile(plan)
        self.compiled.append(compiled)
        return compiled

    def execute(self, compiled):
        return self.inner.execute(compiled)


def test_a_governed_read_never_asks_the_warehouse_for_the_column(project, catalogs):
    """The projection is the enforcement, and this is what that buys: the column is not in the SQL,
    so it is not in the Arrow batch pulled out of Iceberg, so it never leaves the catalog. A mask
    applied to rows on the way out would be a mask that has already read the data it withholds."""
    from dataclasses import replace

    from loom.governance import Policy
    from loom.resolver import build_resolver

    ontology, config, _ = project
    governed = replace(config, policies=(Policy(name="hide-ltv", object_type="Customer", mask=("ltv",)),))
    resolver = build_resolver(ontology, governed, catalogs)
    resolver.engine = _Spy(resolver.engine)

    assert [c["customerId"] for c in resolver.list("Customer")] == ["c1", "c2", "c3", "c4"]
    (compiled,) = resolver.engine.compiled
    assert "lifetime_value" not in compiled.sql
    assert all("lifetime_value" not in scan.columns for scan in compiled.scans)


def test_a_row_predicate_withholds_the_row_from_every_surface(project, catalogs):
    """The row half of M5's claim at the altitude that settles it: real Iceberg, real Arrow, real
    DuckDB SQL, and a real `run_<action>` — with `c2` absent from every one of them.

    `c2` is the anchor worth choosing: it has three seeded orders, so *traverse from a customer you
    cannot get* is a claim with something to return if the anchor end were left ungoverned. That is
    the hole a predicate applied only to the landing type leaves — you cannot search a customer but
    you can traverse to one — and the reverse hop pins the other end."""
    from dataclasses import replace

    from loom.action import OBJECT_NOT_FOUND, build_runtime
    from loom.expr import parse as parse_expr
    from loom.governance import Policy
    from loom.mcp.server import build_server
    from loom.resolver import build_resolver

    ontology, config, _ = project
    governed = replace(
        config,
        mcp=replace(config.mcp, writes=True),
        policies=(
            Policy(name="no-silver", object_type="Customer", rows=parse_expr("object.tier != 'silver'")),
        ),
    )

    resolver = build_resolver(ontology, governed, catalogs)
    assert resolver.get("Customer", "c2") is None
    assert [row["customerId"] for row in resolver.list("Customer")] == ["c1", "c3", "c4"]
    # The anchor end: c2 has o3, o4 and o5, and none of them comes back.
    assert resolver.traverse("Customer", "c2", "orders") == []
    assert [o["orderId"] for o in resolver.traverse("Customer", "c1", "orders")] == ["o1", "o2"]
    # The landing end: o3 was placed by c2.
    assert resolver.traverse("Order", "o3", "placedBy") == []
    assert resolver.traverse("Order", "o1", "placedBy") == [resolver.get("Customer", "c1")]

    server, _ = build_server(ontology, governed, catalogs)
    payload = json.loads(server.call("get_customer", {"key": "c2"})[0])
    # Absent, not forbidden — and the envelope is the one a key that never existed produces.
    assert payload["found"] is False and payload["object"] is None
    assert json.loads(server.call("get_customer", {"key": "c9"})[0])["found"] is False
    # No tool mentions the filter: the rows are the data, so saying "some are withheld" is an
    # existence oracle over them.
    assert not any("no-silver" in tool.description for tool in server.tools.values())

    # And an agent cannot act on the row it cannot see.
    result = build_runtime(ontology, governed, catalogs).run(
        "upgradeTier", {"customer": "c2", "newTier": "gold"}, actor="ci"
    )
    assert result.failures[0].code == OBJECT_NOT_FOUND

    # The ungoverned build of the same project still reads c2 — and still reads it as `silver`, so
    # the refusal above changed nothing it was asked to change.
    assert build_resolver(ontology, config, catalogs).get("Customer", "c2")["tier"] == "silver"
