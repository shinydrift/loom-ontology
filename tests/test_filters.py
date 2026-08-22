"""The filter grammar — what a caller may say, and what it does with a null.

`test_predicate.py`'s twin. That one holds a deployment's grammar to agreeing with itself across two
planes; this one holds a caller's to agreeing with the schema that advertises it, which is the
failure mode a generated surface has: an argument the schema offers and the resolver refuses, or one
the resolver takes and the schema never mentioned.
"""

from pathlib import Path

import pytest

from loom import build
from loom.filters import (
    CONTAINS,
    FILTER_OPS,
    MEMBERSHIP,
    FilterError,
    lower,
    operators,
    property_schema,
)
from loom.query.engine import Capabilities, CompiledQuery
from loom.query.ir import ColumnRef, Compare, Const, Contains, In, pushdown_hints
from loom.resolver import Resolver

VALID = Path(__file__).parent / "fixtures" / "valid"


@pytest.fixture(scope="module")
def ontology():
    ont, _ = build(VALID)
    return ont


@pytest.fixture(scope="module")
def customer(ontology):
    return ontology.object_types["Customer"]


def _lower(ontology, obj, name, value):
    return lower(obj, obj.properties[name], value, "t0", ontology.object_types)


# ---- the two spellings ---------------------------------------------------------


def test_a_bare_value_keeps_its_v0_meaning(ontology, customer):
    """Type-directed sugar, unchanged: substring for a searchable string, exact for the rest.

    Making the bare spelling uniformly `eq` would return *fewer* rows to every filter already
    written against `name`, with nothing raising — the silent narrowing this codebase refuses."""
    assert _lower(ontology, customer, "name", "ada") == (Contains("t0", "full_name", "ada"),)
    assert _lower(ontology, customer, "tier", "gold") == (
        Compare("==", ColumnRef("t0", "tier"), Const("gold")),
    )


def test_operators_on_one_property_are_a_conjunction(ontology, customer):
    assert _lower(ontology, customer, "ltv", {"gte": 100, "lt": 500}) == (
        Compare(">=", ColumnRef("t0", "lifetime_value"), Const(100.0)),
        Compare("<", ColumnRef("t0", "lifetime_value"), Const(500.0)),
    )


def test_comparisons_come_back_in_a_declared_order_not_the_callers(ontology, customer):
    """So the SQL a filter compiles to does not depend on how a JSON object was serialized."""
    one = _lower(ontology, customer, "ltv", {"lt": 500, "gte": 100})
    two = _lower(ontology, customer, "ltv", {"gte": 100, "lt": 500})
    assert one == two


def test_an_empty_operator_object_is_refused(ontology, customer):
    with pytest.raises(FilterError, match="names no comparison"):
        _lower(ontology, customer, "ltv", {})


# ---- null ----------------------------------------------------------------------


def test_a_bare_null_is_refused_and_the_refusal_names_the_spelling(ontology, customer):
    with pytest.raises(FilterError) as e:
        _lower(ontology, customer, "ltv", None)
    assert "a bare null is not a filter value" in str(e.value)
    assert '{"eq": null}' in str(e.value)


def test_null_is_a_value_under_eq_and_ne(ontology, customer):
    assert _lower(ontology, customer, "ltv", {"eq": None}) == (
        Compare("==", ColumnRef("t0", "lifetime_value"), Const(None)),
    )
    assert _lower(ontology, customer, "ltv", {"ne": None}) == (
        Compare("!=", ColumnRef("t0", "lifetime_value"), Const(None)),
    )


@pytest.mark.parametrize("op", ["gt", "gte", "lt", "lte"])
def test_ordering_against_null_is_refused(ontology, customer, op):
    """The refusal `predicate._compare` makes at load time for `object.ltv > null`, made at the
    only time a filter has — undecided for every row is a filter that withholds the object type
    while reading like one that selects."""
    with pytest.raises(FilterError, match="is undecided for every row"):
        _lower(ontology, customer, "ltv", {op: None})


def test_the_schema_admits_a_null_exactly_where_the_grammar_does(ontology, customer):
    """One claim, two readers: whatever the resolver accepts, the schema advertises."""
    schema = property_schema(customer.properties["ltv"], searchable=False)
    for op, sub in schema["anyOf"][1]["properties"].items():
        admits_null = {"type": "null"} in sub.get("anyOf", [])
        try:
            _lower(ontology, customer, "ltv", {op: None})
        except FilterError:
            assert not admits_null, f"schema says '{op}' takes a null and the grammar refuses one"
        else:
            assert admits_null, f"grammar takes a null for '{op}' and the schema does not say so"


# ---- membership ----------------------------------------------------------------


def test_membership_lowers_to_one_node_in_the_conjunction(ontology, customer):
    """The reason `in` shipped before `or`: it disjoins values, not predicates, so the flat
    ANDed tuple `ir.Search.filters` already is holds it without becoming a tree."""
    assert _lower(ontology, customer, "tier", {"in": ["gold", "silver"]}) == (
        In("t0", "tier", ("gold", "silver")),
    )


def test_every_element_is_coerced_like_the_eq_it_stands_for(ontology, customer):
    """An agent sending `"100"` for a double gets the same value it would from `{"eq": "100"}` —
    the coercion `model.coerce_value` exists to make one answer instead of two."""
    assert _lower(ontology, customer, "ltv", {"in": ["100", 200]}) == (
        In("t0", "lifetime_value", (100.0, 200.0)),
    )


def test_a_one_element_membership_means_what_the_eq_it_abbreviates_means(ontology, customer):
    """Asserted here as a node and in `test_query_compile` as SQL, because the trap is that the
    two agree on every table with no nulls in the filtered column."""
    (member,) = _lower(ontology, customer, "ltv", {"in": [None]})
    (equal,) = _lower(ontology, customer, "ltv", {"eq": None})
    assert member == In("t0", "lifetime_value", (None,))
    assert equal == Compare("==", ColumnRef("t0", "lifetime_value"), Const(None))


def test_a_null_may_be_an_element_even_though_the_list_may_not_be_null(ontology, customer):
    assert _lower(ontology, customer, "ltv", {"in": [1.0, None]}) == (
        In("t0", "lifetime_value", (1.0, None)),
    )
    with pytest.raises(FilterError, match="'in' takes a list of values, got null"):
        _lower(ontology, customer, "ltv", {"in": None})


def test_an_empty_membership_list_is_refused(ontology, customer):
    """It has an honest answer — no rows — and that is what makes it a refusal: a caller cannot
    tell that answer from a search that found nothing."""
    with pytest.raises(FilterError, match="matches no row"):
        _lower(ontology, customer, "tier", {"in": []})


def test_the_schema_refuses_an_empty_list_in_the_same_place_the_grammar_does(customer):
    """Announcement and enforcement, which is what this module exists to keep together."""
    schema = property_schema(customer.properties["tier"], searchable=False)
    assert schema["anyOf"][1]["properties"][MEMBERSHIP]["minItems"] == 1


@pytest.mark.parametrize("value", ["gold", {"eq": "gold"}, 3])
def test_a_membership_value_that_is_not_a_list_is_refused(ontology, customer, value):
    with pytest.raises(FilterError, match="'in' takes a list of values"):
        _lower(ontology, customer, "tier", {"in": value})


def test_membership_is_offered_wherever_equality_is(ontology):
    """It *is* equality, so nothing gates it — no type test, and no negotiated capability, since
    no dialect that can say `WHERE c = ?` cannot say `WHERE c IN (?, ?)`."""
    from loom.negotiate import requirements

    for obj in ontology.object_types.values():
        for prop in obj.properties.values():
            ops = operators(prop, searchable=prop.name in obj.searchable)
            assert ("eq" in ops) == (MEMBERSHIP in ops)
    assert all(r.capability != MEMBERSHIP for r in requirements(ontology))


# ---- what a type admits --------------------------------------------------------


def test_an_enum_is_testable_and_not_orderable(customer):
    """`in` is here and the orderings are not: membership is equality, which an enum has."""
    assert operators(customer.properties["tier"], searchable=True) == ("eq", "ne", MEMBERSHIP)


def test_a_string_is_orderable_and_substring_matchable(customer):
    assert operators(customer.properties["name"], searchable=True) == (
        "eq", "ne", MEMBERSHIP, "gt", "gte", "lt", "lte", CONTAINS,
    )


def test_substring_needs_the_declaration_that_negotiates_it(ontology, customer):
    """`negotiate.py` demands `case_insensitive_like` for searchable *string* properties, so a
    `Contains` for any other property would ask an engine for something nothing checked."""
    assert CONTAINS not in operators(customer.properties["ltv"], searchable=False)
    # `Order.customerId` is a string the spec never declared searchable.
    with pytest.raises(FilterError, match="is not declared searchable"):
        _lower(ontology, ontology.object_types["Order"], "customerId", {"contains": "c"})


def test_an_operator_a_type_does_not_have_names_the_ones_it_does(ontology, customer):
    with pytest.raises(FilterError, match=r"'gte' does not apply .*available: eq, ne"):
        _lower(ontology, customer, "tier", {"gte": "gold"})


def test_a_misspelled_operator_is_refused_with_a_suggestion(ontology, customer):
    with pytest.raises(FilterError, match="'gt'"):
        _lower(ontology, customer, "ltv", {"gtt": 1})


def test_every_operator_this_grammar_spells_lowers_to_a_comparison_the_ir_has(ontology, customer):
    """`FILTER_OPS` is the whole of the mapping between two spellings for one meaning, so a
    seventh operator has to be given a §5 spelling rather than arriving without one."""
    from loom.predicate import COMPARISONS

    assert set(FILTER_OPS.values()) == COMPARISONS
    for op in FILTER_OPS:
        (node,) = _lower(ontology, customer, "ltv", {op: 1})
        assert node.op == FILTER_OPS[op]


def test_a_substring_value_is_coerced_like_every_other_value(ontology, customer):
    """`contains` is only offered on a string property, where coercion is `str(value)` — which is
    what the bare spelling did in v0, so an agent sending a number keeps the answer it had."""
    assert _lower(ontology, customer, "name", {"contains": 3}) == (Contains("t0", "full_name", "3"),)
    assert _lower(ontology, customer, "name", 3) == (Contains("t0", "full_name", "3"),)


# ---- what may become advisory --------------------------------------------------


class _Recording:
    def capabilities(self):
        return Capabilities(name="recording")

    def compile(self, plan):
        self.plan = plan
        return CompiledQuery(sql="<recorded>")

    def execute(self, compiled):
        return []


def test_only_equality_becomes_a_pushdown_hint():
    """The `ScanRequest` channel is a hint an adapter may ignore, so what may enter it is decided
    in one function — and a range has no spelling in a `(column, value)` pair anyway."""
    filters = (
        Compare("==", ColumnRef("t0", "tier"), Const("gold")),
        Compare(">=", ColumnRef("t0", "lifetime_value"), Const(1.0)),
        Compare("!=", ColumnRef("t0", "tier"), Const("bronze")),
        Contains("t0", "full_name", "ada"),
        In("t0", "tier", ("gold", "platinum")),
    )
    assert pushdown_hints(filters) == (("tier", "gold"),)


def test_membership_yields_no_hint_because_the_hints_are_a_conjunction():
    """One hint per value would prune to the rows matching *every* value — an empty scan rather
    than a slow one, which is a wrong answer arriving through a channel documented as advisory."""
    assert pushdown_hints((In("t0", "tier", ("gold", "platinum")),)) == ()


# ---- the same rows, through a real engine ---------------------------------------
#
# `test_predicate.py` runs the deployment grammar's two *lowerings* against each other over a table
# with real nulls in it. This is the caller grammar's one claim of the same kind — `in` abbreviates
# `eq`, so it must select the rows `eq` selects — and it needs real rows for the same reason: the
# two spellings agree on every table where the filtered column has no nulls, so reasoning about
# SQL's `IN` is exactly the thing that can be confidently wrong here.


class _ArrowCatalog:
    """Just enough catalog to hand DuckDB a table with nulls in the filtered columns."""

    ROWS = [
        {"id": "c1", "full_name": "Ada", "tier": "gold", "lifetime_value": 48210.5},
        {"id": "c2", "full_name": "Grace", "tier": "silver", "lifetime_value": None},
        {"id": "c3", "full_name": "Katherine", "tier": None, "lifetime_value": 100.0},
        {"id": "c4", "full_name": "Hopper", "tier": None, "lifetime_value": None},
        {"id": "c5", "full_name": "Mary", "tier": "bronze", "lifetime_value": 1.0},
    ]
    FIELDS = {"id": "string", "full_name": "string", "tier": "string", "lifetime_value": "float64"}

    def scan(self, table, columns=None, predicates=(), limit=None):
        import pyarrow as pa

        names = list(columns) if columns else list(self.FIELDS)
        schema = pa.schema([pa.field(n, getattr(pa, self.FIELDS[n])()) for n in names])
        return pa.table({n: [row[n] for row in self.ROWS] for n in names}, schema=schema)


def _keys(ontology, filters) -> set[str]:
    from loom.query.engines.duckdb import DuckDBEngine

    resolver = Resolver(
        ontology=ontology, engine=DuckDBEngine(catalogs={"rest_main": _ArrowCatalog()})
    )
    return {row["customerId"] for row in resolver.search("Customer", filters, limit=500)}


@pytest.mark.parametrize("value", ["gold", None])
def test_a_one_element_membership_returns_the_rows_its_eq_returns(ontology, value):
    """Including the null, which is the case SQL's own `IN` gets wrong in both directions: it
    never matches a null element, and it answers unknown for a null column."""
    assert _keys(ontology, {"tier": {"in": [value]}}) == _keys(ontology, {"tier": {"eq": value}})


def test_membership_is_the_union_of_the_equalities_it_abbreviates(ontology):
    one = _keys(ontology, {"tier": {"eq": "gold"}})
    two = _keys(ontology, {"tier": {"eq": None}})
    assert _keys(ontology, {"tier": {"in": ["gold", None]}}) == one | two
    assert one and two  # neither half is vacuous, or the union proves nothing


def test_membership_selects_only_what_it_named(ontology):
    assert _keys(ontology, {"tier": {"in": ["gold", "bronze"]}}) == {"c1", "c5"}


def test_a_governance_predicate_cannot_reach_the_hint_channel(ontology):
    """It hangs on `TableRef.predicate`, which `pushdown_hints` is not given — the structural
    version of `ir.TableRef`'s claim that a governance filter is never advisory."""
    from loom.expr import parse as parse_expr
    from loom.governance import Policy, bind_policies

    policy = Policy(
        name="gold-only", object_type="Customer", rows=parse_expr("object.tier == 'gold'")
    )
    program = bind_policies(ontology, [policy])
    engine = _Recording()
    resolver = Resolver(ontology=ontology, engine=engine, policies=program.select(None))
    resolver.search("Customer", {"name": "ada"})

    source = engine.plan.source
    assert source.table.predicate is not None
    assert pushdown_hints(source.filters) == ()
