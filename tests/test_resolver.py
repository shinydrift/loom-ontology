"""Resolver behaviour, checked against a recording engine.

No catalog, no DuckDB, no storage: what matters here is *which plan* an ontology operation
produces, since that's where the semantic guarantees live. Whether the plan then returns the right
rows is test_e2e_iceberg.py's job.
"""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from loom import build
from loom.query.engine import Capabilities, CompiledQuery
from loom.query.ir import ColumnRef, Compare, Const, Contains, Eq, GetByKey, Search, Traverse
from loom.resolver import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Resolver, ResolverError

VALID = Path(__file__).parent / "fixtures" / "valid"


def _eq(column, value):
    """What a caller's equality lowers to now: the node a policy's `==` lowers to."""
    return Compare("==", ColumnRef("t0", column), Const(value))


class RecordingEngine:
    """Captures the plans it is handed and returns canned rows."""

    def __init__(self, rows=()):
        self.plans = []
        self.rows = list(rows)

    def capabilities(self):
        return Capabilities(name="recording")

    def compile(self, plan):
        self.plans.append(plan)
        return CompiledQuery(sql="<recorded>")

    def execute(self, compiled):
        return self.rows

    @property
    def source(self):
        return self.plans[-1].source


@pytest.fixture
def ontology():
    ont, _ = build(VALID)
    return ont


@pytest.fixture
def typed(ontology):
    """The same spec with the numeric and timestamp properties also declared `searchable`.

    The coercion tests below are about what happens to a *value*, and every value worth coercing
    lives on a property the shared fixture does not declare searchable — which is the gate
    `_filters` now applies before it ever reads one. Widening the fixture itself is not an option:
    `test_mcp_registry` and `test_negotiate` both pin `Customer.searchable` at `(name, tier)`, and
    they are pinning it for the surface's sake. So the gate is opened here, on a copy, exactly as an
    author would open it — `searchable` is a declaration, and since M7 any scalar may be in it."""
    types = dict(ontology.object_types)
    types["Customer"] = replace(types["Customer"], searchable=("name", "tier", "ltv"))
    types["Order"] = replace(types["Order"], searchable=("orderId", "total", "placedAt"))
    return replace(ontology, object_types=types)


def _resolver(ontology, rows=()):
    return Resolver(ontology=ontology, engine=RecordingEngine(rows))


# ---- get -----------------------------------------------------------------------


def test_get_builds_a_get_by_key_over_the_backing_table(ontology):
    r = _resolver(ontology, rows=[{"customerId": "c1"}])
    assert r.get("Customer", "c1") == {"customerId": "c1"}
    src = r.engine.source
    assert isinstance(src, GetByKey)
    assert src.table.table == "crm.customers" and src.table.catalog == "rest_main"
    assert src.key_column == "id"  # the physical column, not the property name


def test_get_returns_none_when_absent(ontology):
    assert _resolver(ontology, rows=[]).get("Customer", "nope") is None


def test_get_projects_every_property_under_its_ontology_name(ontology):
    r = _resolver(ontology)
    r.get("Customer", "c1")
    projected = {(c.column, c.output) for c in r.engine.plans[-1].columns}
    assert projected == {
        ("id", "customerId"),
        ("full_name", "name"),
        ("tier", "tier"),
        ("lifetime_value", "ltv"),
    }


def test_get_reports_a_backing_table_that_violates_the_declared_key(ontology):
    """The spec says the primary key is unique; if the data disagrees, say so."""
    r = _resolver(ontology, rows=[{"customerId": "c1"}, {"customerId": "c1"}])
    with pytest.raises(ResolverError, match="violates the uniqueness the spec declares"):
        r.get("Customer", "c1")


def test_unknown_object_type_lists_the_known_ones(ontology):
    with pytest.raises(ResolverError, match="unknown object type 'Custmer' \\(known: Customer, Order\\)"):
        _resolver(ontology).get("Custmer", "c1")


# ---- search / list -------------------------------------------------------------


def test_searchable_string_matches_on_substring(ontology):
    r = _resolver(ontology)
    r.search("Customer", {"name": "ada"})
    assert r.engine.source.filters == (Contains("t0", "full_name", "ada"),)


def test_searchable_enum_matches_exactly(ontology):
    """Enum values are a closed set, so substring matching would only add ambiguity."""
    r = _resolver(ontology)
    r.search("Customer", {"tier": "gold"})
    assert r.engine.source.filters == (_eq("tier", "gold"),)


def test_a_property_not_declared_searchable_is_refused(ontology):
    """`searchable` decides a property is filterable *at all*, and this is where that is true.

    It used to be true only of the generated schema: `_search_tool` built the `filter` properties
    from this list while the resolver accepted any declared property, so `additionalProperties:
    false` was an announcement and a caller who ignored it could range-query `ltv` — the drift dates
    from M1, when the list only meant "matches on substring", and survived M7 widening it to mean
    what §2 rule 6 now says. A live MCP client found it by filtering on a property the tool never
    offered and getting rows back."""
    with pytest.raises(ResolverError, match="'Customer.ltv' is not declared searchable"):
        _resolver(ontology).search("Customer", {"ltv": 100})


def test_the_refusal_names_the_properties_that_would_have_worked(ontology):
    with pytest.raises(ResolverError, match="or filter on: name, tier"):
        _resolver(ontology).search("Customer", {"ltv": 100})


def test_declaring_it_searchable_is_all_it_takes(typed):
    """The other half of the refusal above: nothing about the *grammar* changed, so the same filter
    lowers to the same comparison the moment the spec offers the property."""
    r = _resolver(typed)
    r.search("Customer", {"ltv": 100})
    assert r.engine.source.filters == (_eq("lifetime_value", 100.0),)


def test_filtering_on_an_undeclared_property_is_refused(ontology):
    with pytest.raises(ResolverError, match="'Customer' has no property 'emial'"):
        _resolver(ontology).search("Customer", {"emial": "x"})


@pytest.mark.parametrize("filters", ["gold", ["gold"], 7])
def test_a_filter_that_is_not_an_object_is_refused_in_words(ontology, filters):
    """A tool call carries whatever JSON arrived, and this used to reach `.items()` and come back as
    a raw `AttributeError` — the same shape as the `KeyError: 'key'` the surface fixed one level
    up."""
    with pytest.raises(ResolverError, match="'filter' takes an object of property filters"):
        _resolver(ontology).search("Customer", filters)


def test_list_is_search_with_no_filters_ordered_by_key(ontology):
    r = _resolver(ontology)
    r.list("Customer")
    src = r.engine.source
    assert isinstance(src, Search) and src.filters == ()
    assert src.order_by == ("id",)


def test_reads_are_always_bounded(ontology):
    """There is no way to ask for an unbounded read — that's how one tool call would pull a
    whole table into an agent's context."""
    r = _resolver(ontology)
    r.list("Customer")
    assert r.engine.source.limit == DEFAULT_PAGE_SIZE
    r.list("Customer", limit=MAX_PAGE_SIZE)
    assert r.engine.source.limit == MAX_PAGE_SIZE


def test_a_limit_above_the_maximum_is_refused_rather_than_clamped(ontology):
    """This used to answer `min(limit, MAX_PAGE_SIZE)`, and the page envelope echoed the number the
    caller sent — so a client paging with `offset += limit` after asking for 10,000 skipped every
    row between 500 and 10,000 and was told `hasMore: false` when it got there. The lower bound was
    already a refusal; enforcing one bound and quietly rewriting the other is what let them
    disagree."""
    with pytest.raises(ResolverError, match=f"limit must be <= {MAX_PAGE_SIZE}, got 10000"):
        _resolver(ontology).list("Customer", limit=10_000)


@pytest.mark.parametrize("limit,offset,match", [(0, 0, "limit must be >= 1"), (5, -1, "offset must be >= 0")])
def test_nonsense_paging_is_refused(ontology, limit, offset, match):
    with pytest.raises(ResolverError, match=match):
        _resolver(ontology).list("Customer", limit=limit, offset=offset)


# ---- type coercion -------------------------------------------------------------


def test_string_digits_coerce_to_the_declared_numeric_type(typed):
    """An LLM sends `"100"`; a mismatched predicate would push down and match nothing."""
    r = _resolver(typed)
    r.search("Customer", {"ltv": "100.5"})
    assert r.engine.source.filters == (_eq("lifetime_value", 100.5),)


def test_decimal_filters_never_go_through_a_float(typed):
    r = _resolver(typed)
    r.search("Order", {"total": "1299.99"})
    assert r.engine.source.filters == (_eq("total_amount", Decimal("1299.99")),)


def test_an_operator_value_is_coerced_exactly_as_a_bare_one_is(typed):
    """One coercion for both spellings, or `{"gte": "100"}` would compare a string to a double."""
    r = _resolver(typed)
    r.search("Customer", {"ltv": {"gte": "100.5"}})
    assert r.engine.source.filters == (Compare(">=", ColumnRef("t0", "lifetime_value"), Const(100.5)),)


def test_timestamp_filters_are_parsed(typed):
    r = _resolver(typed)
    r.search("Order", {"placedAt": "2026-02-14T12:00:00+00:00"})
    (f,) = r.engine.source.filters
    assert f.right.value.year == 2026 and f.right.value.month == 2 and f.right.value.day == 14


def test_a_value_of_the_wrong_type_is_a_clear_error(typed):
    with pytest.raises(ResolverError, match="cannot read 'not-a-number' as double"):
        _resolver(typed).search("Customer", {"ltv": "not-a-number"})


def test_an_enum_value_outside_the_declared_set_is_refused(ontology):
    with pytest.raises(ResolverError, match="'platinum' is not one of: bronze, silver, gold"):
        _resolver(ontology).search("Customer", {"tier": "platinum"})


def test_a_bare_null_filter_is_refused_and_names_the_spelling_that_works(typed):
    """v0 answered `{"ltv": None}` as `IS NULL`. It is refused now, and the break is the point:
    JSON cannot tell a field an agent left blank from one it meant as null, so the v0 answer was a
    plausible non-empty result set for a caller who asked nothing."""
    with pytest.raises(ResolverError, match="a bare null is not a filter value"):
        _resolver(typed).search("Customer", {"ltv": None})


def test_null_is_a_value_where_the_caller_also_wrote_the_operator(typed):
    r = _resolver(typed)
    r.search("Customer", {"ltv": {"eq": None}})
    assert r.engine.source.filters == (_eq("lifetime_value", None),)


# ---- traverse ------------------------------------------------------------------


def test_forward_traverse_joins_from_the_declared_end(ontology):
    r = _resolver(ontology)
    r.traverse("Order", "o3", "placedBy")
    src = r.engine.source
    assert isinstance(src, Traverse)
    assert src.from_table.table == "sales.orders"  # anchored side
    assert src.to_table.table == "crm.customers"  # returned side
    assert src.from_column == "customer_id" and src.to_column == "id"
    assert src.anchor == Eq("t1", "id", "o3")  # Order's own primary key column
    assert [c.output for c in r.engine.plans[-1].columns] == ["customerId", "name", "tier", "ltv"]


def test_reverse_traverse_uses_the_reverse_name_and_flips_the_join(ontology):
    r = _resolver(ontology)
    r.traverse("Customer", "c2", "orders")
    src = r.engine.source
    assert src.from_table.table == "crm.customers"
    assert src.to_table.table == "sales.orders"
    assert src.from_column == "id" and src.to_column == "customer_id"
    assert src.anchor == Eq("t1", "id", "c2")
    assert [c.output for c in r.engine.plans[-1].columns] == ["orderId", "customerId", "total", "placedAt"]


def test_traverse_is_paged_and_ordered_like_a_search(ontology):
    r = _resolver(ontology)
    r.traverse("Customer", "c2", "orders", limit=2, offset=1)
    src = r.engine.source
    assert src.limit == 2 and src.offset == 1 and src.order_by == ("id",)


def test_an_undeclared_link_lists_what_is_available(ontology):
    with pytest.raises(ResolverError, match="'Customer' has no link 'invoices' \\(available: orders\\)"):
        _resolver(ontology).traverse("Customer", "c1", "invoices")


def test_a_link_is_not_traversable_from_the_wrong_end(ontology):
    """`placedBy` runs Order -> Customer; from Customer the name is `orders`."""
    with pytest.raises(ResolverError, match="'Customer' has no link 'placedBy'"):
        _resolver(ontology).traverse("Customer", "c1", "placedBy")


def test_links_of_reports_both_directions(ontology):
    r = _resolver(ontology)
    assert [(d.name, d.target_object_type, d.forward) for d in r.links_of("Order")] == [
        ("placedBy", "Customer", True)
    ]
    assert [(d.name, d.target_object_type, d.forward) for d in r.links_of("Customer")] == [
        ("orders", "Order", False)
    ]
