"""Resolver behaviour, checked against a recording engine.

No catalog, no DuckDB, no storage: what matters here is *which plan* an ontology operation
produces, since that's where the semantic guarantees live. Whether the plan then returns the right
rows is test_e2e_iceberg.py's job.
"""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from loom import build
from loom.governance import Policy, bind_policies
from loom.query.engine import Capabilities, CompiledQuery
from loom.query.ir import ColumnRef, Compare, Const, Contains, Eq, GetByKey, Match, Search, Traverse
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


# ---- match ---------------------------------------------------------------------


def _semantic(ontology, name="Customer", prop="name"):
    """The fixture with a `semantic:` property injected.

    Injected rather than declared, for `test_embed.py`'s reason: this ontology is read by half the
    suite, and a property declared for one module's benefit is one every other module routes
    around."""
    obj = replace(ontology.object_types[name], semantic=prop)
    return replace(ontology, object_types={**ontology.object_types, name: obj})


def test_match_builds_a_match_over_the_table_and_its_sidecar(ontology):
    r = _resolver(_semantic(ontology))
    r.match("Customer", [0.1, 0.2], "stub-v1")
    src = r.engine.source
    assert isinstance(src, Match)
    assert src.table.table == "crm.customers"
    assert src.key_column == "id"  # the physical column, as everywhere else in this layer
    assert src.vectors.table.table == "_loom_meta.vectors__Customer"
    # The object's own catalog: a vector is derived from the rows of one table and written beside
    # them, which is `EmbedRuntime._store`'s choice seen from the read side.
    assert src.vectors.table.catalog == "rest_main"
    assert src.query == (0.1, 0.2)


def test_the_sidecar_can_never_carry_a_policy(ontology):
    """It stands for no object type, so no policy names it — `ThroughRef`'s answer, and there is no
    field here to put a different one in."""
    r = _resolver(_semantic(ontology))
    r.match("Customer", [0.1], "stub-v1")
    assert not hasattr(r.engine.source.vectors, "predicate")
    assert r.engine.source.vectors.table.predicate is None


def test_match_ties_break_on_the_primary_key(ontology):
    r = _resolver(_semantic(ontology))
    r.match("Customer", [0.1], "stub-v1")
    assert r.engine.source.order_by == ("id",)


def test_match_pages_like_every_other_read(ontology):
    """Including the bound: a ranked read reaches `_page_size` by the same door, so asking for more
    than the maximum is the refusal it is everywhere else rather than a clamp only this surface
    would have to explain."""
    r = _resolver(_semantic(ontology))
    r.match("Customer", [0.1], "stub-v1")
    assert r.engine.source.limit == DEFAULT_PAGE_SIZE
    r.match("Customer", [0.1], "stub-v1", limit=MAX_PAGE_SIZE, offset=3)
    assert r.engine.source.limit == MAX_PAGE_SIZE and r.engine.source.offset == 3
    with pytest.raises(ResolverError, match=f"limit must be <= {MAX_PAGE_SIZE}"):
        r.match("Customer", [0.1], "stub-v1", limit=9_000)


def test_match_takes_the_same_filters_search_takes(ontology):
    r = _resolver(_semantic(ontology))
    r.match("Customer", [0.1], "stub-v1", {"tier": "gold"})
    assert r.engine.source.filters == (_eq("tier", "gold"),)


def test_match_refuses_a_filter_on_a_masked_property(typed):
    """The same refusal `search` gets, from the same code: a ranking over a withheld value would be
    an even better oracle for it than a filter, since it answers with a gradient.

    The masked property has to be a *searchable* one for this to be the refusal that fires, because
    `_filters` checks the spec's list before the deployment's policies — a property that was never
    filterable is told so without a policy name being spoken. Hence `typed`, where `ltv` is declared
    the way an author would declare it: the two other searchable properties cannot be masked at all
    here, `name` because this variant declares it semantic and `tier` because an action writes it."""
    ont = _semantic(typed)
    policies = bind_policies(
        ont, (Policy(name="hide-ltv", object_type="Customer", mask=("ltv",)),)
    ).select(None)
    r = _resolver(ont).governed_by(policies)
    with pytest.raises(ResolverError, match="withheld by governance policy 'hide-ltv'"):
        r.match("Customer", [0.1], "stub-v1", {"ltv": 5.0})


def test_match_refuses_a_type_that_declares_nothing(ontology):
    with pytest.raises(ResolverError, match="declares no 'semantic:' property"):
        _resolver(ontology).match("Customer", [0.1], "stub-v1")


def test_match_refuses_an_empty_query_vector(ontology):
    """`{"in": []}`'s argument one plane over: a zero-width query has no distance to anything, so
    every row would come back in key order wearing a score — an answer nobody could tell from one."""
    with pytest.raises(ResolverError, match="empty, so it ranks nothing"):
        _resolver(_semantic(ontology)).match("Customer", [], "stub-v1")


def test_match_splits_the_score_from_the_object(ontology):
    """§7's namespace rule reaching the result: the object is the spec's vocabulary and the score is
    Loom's, so one is never inside the other."""
    rows = [
        {"customerId": "c1", "name": "Ada", "_loom_score": 0.9, "_loom_embedded_at": None},
        {"customerId": "c2", "name": "Grace", "_loom_score": 0.4, "_loom_embedded_at": None},
    ]
    result = _resolver(_semantic(ontology), rows).match("Customer", [0.1], "stub-v1")
    assert result.object_type == "Customer" and result.property == "name"
    assert result.model == "stub-v1"
    assert [m.score for m in result.matches] == [0.9, 0.4]
    assert result.matches[0].object == {"customerId": "c1", "name": "Ada"}


def test_embedded_as_of_is_the_oldest_stamp_among_the_rows_returned(ontology):
    """The claim an envelope can honestly make about the page it is attached to: every object here
    was embedded at least this recently. `loom embed` reports the sidecar-wide reading, which is the
    operator's question rather than the caller's."""
    old = datetime(2026, 1, 1, tzinfo=UTC)
    new = datetime(2026, 8, 1, tzinfo=UTC)
    rows = [
        {"customerId": "c1", "_loom_score": 0.9, "_loom_embedded_at": new},
        {"customerId": "c2", "_loom_score": 0.4, "_loom_embedded_at": old},
    ]
    result = _resolver(_semantic(ontology), rows).match("Customer", [0.1], "stub-v1")
    assert result.embedded_as_of == old


def test_a_property_named_like_the_score_does_not_collide_with_it(ontology):
    """Nothing stops a spec declaring `_loom_score`, and the engine hands rows back keyed by output
    name — so a collision would not be an error, it would be two columns silently becoming one."""
    obj = ontology.object_types["Customer"]
    renamed = replace(
        obj,
        properties={
            **{k: v for k, v in obj.properties.items() if k != "name"},
            "_loom_score": replace(obj.properties["name"], name="_loom_score"),
        },
    )
    ont = replace(ontology, object_types={**ontology.object_types, "Customer": renamed})
    r = _resolver(_semantic(ont, prop="_loom_score"))
    r.match("Customer", [0.1], "stub-v1")
    outputs = [c.output for c in r.engine.plans[-1].columns]
    assert r.engine.source.score_as not in {"_loom_score"}
    assert len(outputs) == len(set(outputs))
