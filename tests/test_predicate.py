"""A row predicate — the subset that lowers, and the agreement of the two lowerings.

The claim this file exists to hold down is the one M5's second slice is built on: **a `rows:`
predicate admits the same rows on the read path, where it is compiled into SQL, as on the write
path, where it is evaluated in process over one row.** A governance filter that admits a row on one
plane and drops it on the other is the worst failure available to it, so the agreement is asserted
differentially — every predicate against every row, through real DuckDB and through
`predicate.admits`, with the two admitted sets compared — rather than argued for in a docstring.

The fixture rows are chosen to be hostile in exactly the way that matters: a null in a nullable
column, a null in a column the spec declares **non-nullable** (tables do contradict specs — that is
why `ambiguous_key` exists), and the predicates that pit §5's two-valued equality against SQL's
three-valued logic under a negation.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from loom import build
from loom.auth import ClaimType, Principal, readable_claims
from loom.expr import parse as parse_expr
from loom.governance import Policy, PolicyError, bind_policies
from loom.predicate import (
    GRAMMAR,
    LOWERABLE,
    NOT_LOWERABLE,
    UNDECIDED,
    admits,
    check,
    check_guard,
    fold,
    lower,
    truth,
)
from loom.query.engines.duckdb import DuckDBEngine
from loom.query.ir import ColumnRef, Compare, Const, Not, Or
from loom.resolver import Resolver

VALID = Path(__file__).parent / "fixtures" / "valid"


@pytest.fixture
def customer():
    ont, _ = build(VALID)
    return ont, ont.object_types["Customer"]


def _expr(text: str):
    return parse_expr(text)


# ---- the subset ------------------------------------------------------------------


def test_the_grammar_is_partitioned_into_what_lowers_and_what_is_refused():
    """`ENFORCED_KEYS`/`RESERVED_KEYS`' device, applied to an expression language.

    A node that is in neither set is the third kind: silently accepted, unenforced, and unmentioned
    — which for a governance predicate means a policy that filters differently on the two planes.
    Adding an operator or a function to `expr` fails here until somebody says which it is."""
    assert LOWERABLE.isdisjoint(NOT_LOWERABLE)
    assert LOWERABLE | set(NOT_LOWERABLE) == GRAMMAR


@pytest.mark.parametrize(
    "text, expected",
    [
        ("object.ltv + 1 > 2", "Arithmetic"),
        ("object.ltv - 1 > 2", "Arithmetic"),
        ("object.ltv * 2 > 2", "Arithmetic"),
        ("object.ltv / 2 > 2", "Arithmetic"),
        ("lower(object.name) == 'ada'", "Case folding"),
        ("upper(object.name) == 'ADA'", "Case folding"),
        ("len(object.name) > 3", "Length in characters or in bytes"),
        ("coalesce(object.ltv, 0) > 3", "the null tool"),
        ("object.name != null && -object.ltv > 3", "Arithmetic"),
    ],
)
def test_a_node_the_engine_would_compute_is_refused_naming_it(customer, text, expected):
    """The line is not "what was easy to implement": *a predicate is lowerable when Loom, not the
    engine, decides what every operator means*. Loom emits the comparison and binds the constant, so
    there is nothing for an engine to compute differently — and each refusal says which engine
    freedom it is declining to inherit."""
    ont, obj = customer
    (problem,) = check(_expr(text), obj, ont.object_types)
    assert expected in problem
    assert "row predicate" in problem


def test_now_is_refused_for_a_reason_that_is_not_about_engines(customer):
    """The one refusal in the set that would lower perfectly well — it never reaches the engine, it
    would bind as a parameter. It is refused because *which instant, the read's or the run's* is a
    decision, and a subset that answers it by accident is how a decision gets made by nobody."""
    ont, obj = customer
    (problem,) = check(_expr("object.name != null && now() > now()"), obj, ont.object_types)
    assert "which instant" in problem
    assert "stamps one instant per call" in problem


def test_a_refused_node_is_refused_the_same_way_wherever_it_stands(customer):
    """A `coalesce()` standing where a condition belongs and one standing where an operand belongs
    are the same thing the deployment cannot compile — so they get the same sentence, and an author
    who moves it has not fixed it. The message names the sub-expression, which is what an author can
    find in the file."""
    ont, obj = customer
    (as_condition,) = check(_expr("coalesce(object.ltv, 1)"), obj, ont.object_types)
    (as_operand,) = check(_expr("coalesce(object.ltv, 1) > 2"), obj, ont.object_types)
    assert as_condition == as_operand
    assert as_condition.startswith("'coalesce(object.ltv, 1)' cannot be used in a row predicate")


def test_a_bare_name_is_a_parameter_and_a_policy_has_none(customer):
    """One language keeps one meaning for each reference form. A bare identifier is a *parameter*
    reference in §5; growing a second meaning for it inside a policy would be a dialect."""
    ont, obj = customer
    (problem,) = check(_expr("tier == 'gold'"), obj, ont.object_types)
    assert "a bare name is a parameter and a policy has none" in problem
    assert "Write 'object.tier'" in problem


def test_a_misspelled_property_is_refused_with_a_suggestion(customer):
    """A policy protecting a misspelling protects nothing and looks exactly like one that works —
    the argument that already refuses a misspelled `mask`."""
    ont, obj = customer
    (problem,) = check(_expr("object.teir == 'gold'"), obj, ont.object_types)
    assert "'object.teir' is not a property of 'Customer'" in problem and "tier" in problem


def test_a_predicate_that_is_not_a_condition_is_refused(customer):
    ont, obj = customer
    (problem,) = check(_expr("object.tier"), obj, ont.object_types)
    assert "'object.tier' is not a condition" in problem


def test_ordering_against_null_is_refused_rather_than_admitting_nothing(customer):
    """`object.ltv > null` is undecided for every row, so it withholds the whole object type while
    reading like a filter. §5 already refuses to order a null; this is that refusal moved to load
    time, where it can name the expression instead of silently emptying a table."""
    ont, obj = customer
    (problem,) = check(_expr("object.ltv > null"), obj, ont.object_types)
    assert "orders against null" in problem and "object.ltv > null" in problem


def test_incomparable_operands_are_refused(customer):
    ont, obj = customer
    (problem,) = check(_expr("object.ltv == 'gold'"), obj, ont.object_types)
    assert "compares 'double' with 'string'" in problem


def test_a_predicate_that_reads_no_property_is_refused(customer):
    """The same answer for every row: it either admits everything, which reads like protection and
    is none, or admits nothing, which withholds the object type by the back door. Not declaring it
    is the honest spelling of either — the argument that refuses a mask withholding nothing."""
    ont, obj = customer
    (problem,) = check(_expr("1 == 1"), obj, ont.object_types)
    assert "names no property of 'Customer'" in problem


def test_an_enum_compared_against_a_value_it_cannot_hold_is_refused(customer):
    """The same offence as the test above, reached with a constant that *looks* like data.

    `object.tier != 'closed'` reads like a filter, type-checks — an enum compares as its string
    storage — and is true for every row, so the deployment withholds nothing. The shipped dashboard
    example made exactly this mistake and nothing anywhere said so: the policy loaded, the banner
    reported it, and every row came back."""
    ont, obj = customer
    (problem,) = check(_expr("object.tier != 'closed'"), obj, ont.object_types)
    assert "a value it cannot hold" in problem
    assert "bronze, silver, gold" in problem
    # Which way the accident falls, because `!=` and `==` fail in opposite directions and an author
    # needs to know which one they have.
    assert "true for every row" in problem and "withholds nothing at all" in problem
    (flipped,) = check(_expr("object.tier == 'closed'"), obj, ont.object_types)
    assert "false for every row" in flipped and "withholds every row" in flipped


def test_a_misspelled_enum_value_is_refused_with_a_suggestion(customer):
    ont, obj = customer
    (problem,) = check(_expr("object.tier == 'glod'"), obj, ont.object_types)
    assert "did you mean 'gold'?" in problem


def test_a_declared_enum_value_is_the_whole_point_and_stays_accepted(customer):
    ont, obj = customer
    assert check(_expr("object.tier == 'gold'"), obj, ont.object_types) == []
    # Null is a value you may test an enum against — `==` is null-safe here — and it is not a member
    # of anything, so the membership check must not reach for it.
    assert check(_expr("object.tier != null"), obj, ont.object_types) == []
    # Ordering is a different answer per row whether or not the string is a member, so there is
    # nothing constant to refuse and this check stays out of it.
    assert check(_expr("object.tier > 'closed'"), obj, ont.object_types) == []


def test_every_problem_at_once(customer):
    """`check_capabilities`' bargain: somebody reconciling a policy with a spec learns the whole of
    what disagrees in one reading."""
    ont, obj = customer
    problems = check(_expr("object.teir == 'gold' && lower(object.name) == 'a'"), obj, ont.object_types)
    assert len(problems) == 2


def test_a_property_a_link_joins_on_may_be_filtered_even_though_it_may_not_be_masked(customer):
    """The four refusals a *mask* meets do not carry over, and that is a consequence rather than an
    oversight: each of them is a surface still trying to **use** a value it cannot read. A predicate
    uses the value and shows nobody, so it may filter on a primary key, on a link's join property,
    or on a property an action reads."""
    ont, obj = customer
    assert check(_expr("object.customerId != 'c1'"), obj, ont.object_types) == []
    # upgradeTier reads and writes tier, which a mask may not touch and a predicate may.
    assert check(_expr("object.tier != 'bronze'"), obj, ont.object_types) == []
    bound = bind_policies(
        ont, [Policy(name="p", object_type="Customer", rows=_expr("object.customerId != 'c1'"))]
    ).select(None)
    assert bound.filtered_by("Customer") == ("p",)


def test_an_object_ref_property_compares_as_the_key_it_travels_as(customer):
    """An `objectRef` is a string on the wire — the referenced object's primary key — so
    `object.owner == 'c1'` has to be a comparison of two strings.

    Without the resolution it would be refused as 'objectRef' against 'string', which is a type
    nothing on the wire ever has. It is the one thing this checker borrows from the validator's,
    and it is borrowed rather than re-derived because `coerce_value` already answers the same
    question on the way in."""
    from dataclasses import replace as replace_field

    from loom.model import Property
    from loom.types import PropType

    ont, obj = customer
    owner = Property(name="owner", type=PropType("objectRef", object_type="Customer"), column="owner_id")
    with_ref = replace_field(obj, properties={**obj.properties, "owner": owner})

    assert check(_expr("object.owner == 'c1'"), with_ref, ont.object_types) == []
    (problem,) = check(_expr("object.owner > 1"), with_ref, ont.object_types)
    assert "compares 'string' with 'long'" in problem


def test_a_refused_predicate_names_the_policy_and_the_expression(customer):
    ont, _ = customer
    with pytest.raises(PolicyError) as e:
        bind_policies(
            ont, [Policy(name="eu-only", object_type="Customer", rows=_expr("lower(object.name) == 'x'"))]
        )
    assert "policy 'eu-only' filters rows of 'Customer'" in str(e.value)
    assert "lower(object.name) == 'x'" in str(e.value)


# ---- the read plane --------------------------------------------------------------


def test_equality_lowers_null_safe_and_ordering_does_not(customer):
    """The one operator where §5 and SQL genuinely disagree, and §5 wins on both planes.

    `=` would answer *unknown* for a null and, under a `NOT`, flip a row from excluded to admitted —
    so the fix is at the node rather than in each adapter's head. Ordering is deliberately *not*
    lifted: SQL yields unknown for `NULL > 100` and §5 refuses to order a null, so both planes call
    it undecided and neither admits."""
    _, obj = customer
    engine = DuckDBEngine(catalogs={})
    params: list = []
    sql = engine._predicate(lower(_expr("object.tier == 'gold'"), obj, "t0"), params)
    assert sql == '"t0"."tier" IS NOT DISTINCT FROM ?' and params == ["gold"]

    params = []
    sql = engine._predicate(lower(_expr("object.ltv > 100"), obj, "t0"), params)
    assert sql == '"t0"."lifetime_value" > ?' and params == [100]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("object.ltv == null", '"t0"."lifetime_value" IS NULL'),
        ("object.ltv != null", '"t0"."lifetime_value" IS NOT NULL'),
        ("null == object.ltv", '"t0"."lifetime_value" IS NULL'),
        ("object.tier != 'gold'", '"t0"."tier" IS DISTINCT FROM ?'),
        ("!(object.tier == 'gold')", '(NOT "t0"."tier" IS NOT DISTINCT FROM ?)'),
        (
            "object.ltv > 1 && object.tier == 'gold'",
            '("t0"."lifetime_value" > ? AND "t0"."tier" IS NOT DISTINCT FROM ?)',
        ),
        (
            "object.ltv > 1 || object.tier == 'gold'",
            '("t0"."lifetime_value" > ? OR "t0"."tier" IS NOT DISTINCT FROM ?)',
        ),
        ("object.tier == object.name", '"t0"."tier" IS NOT DISTINCT FROM "t0"."full_name"'),
    ],
)
def test_lowered_sql(customer, text, expected):
    """A null literal takes the shorter spelling: `IS NULL` says exactly what `IS NOT DISTINCT FROM
    NULL` says and needs no typed parameter for a value that has no type."""
    _, obj = customer
    assert DuckDBEngine(catalogs={})._predicate(lower(_expr(text), obj, "t0"), []) == expected


def test_lowering_is_ir_not_sql(customer):
    """The predicate reaches an adapter as nodes, not as a string — which is what keeps *the LLM
    never receives raw SQL* structural, and what lets a second adapter lower it differently."""
    _, obj = customer
    pred = lower(_expr("!(object.ltv > 1) || object.tier == 'gold'"), obj, "t0")
    assert pred == Or(
        Not(Compare(">", ColumnRef("t0", "lifetime_value"), Const(1))),
        Compare("==", ColumnRef("t0", "tier"), Const("gold")),
    )


# ---- the write plane -------------------------------------------------------------


@pytest.mark.parametrize(
    "text, row, expected",
    [
        # §5's two-valued equality, preserved exactly: null is a value you can test.
        ("object.ltv == null", {"ltv": None}, True),
        ("object.ltv == null", {"ltv": 1.0}, False),
        ("object.ltv != null", {"ltv": None}, False),
        ("object.tier != 'gold'", {"tier": None}, True),
        # Ordering a null is undecided, where a validation rule would be an expression_error.
        ("object.ltv > 100", {"ltv": None}, UNDECIDED),
        ("!(object.ltv > 100)", {"ltv": None}, UNDECIDED),
        # Kleene, which is what makes the two planes agree: a decided operand decides, whichever
        # side it is written on, and that is exactly what short-circuiting would break.
        ("object.ltv > 100 || object.tier == 'gold'", {"ltv": None, "tier": "gold"}, True),
        ("object.tier == 'gold' || object.ltv > 100", {"ltv": None, "tier": "gold"}, True),
        ("object.ltv > 100 || object.tier == 'gold'", {"ltv": None, "tier": "bronze"}, UNDECIDED),
        ("object.ltv > 100 && object.tier == 'gold'", {"ltv": None, "tier": "bronze"}, False),
        ("object.ltv > 100 && object.tier == 'gold'", {"ltv": None, "tier": "gold"}, UNDECIDED),
    ],
)
def test_three_answers(customer, text, row, expected):
    assert truth(_expr(text), row) is expected


def test_a_row_is_admitted_only_on_true(customer):
    """The admission rule, and the reason negation stays fail-closed: `NOT undecided` is undecided,
    so a predicate written to exclude cannot be made to admit by a missing value.

    The rejected alternative — totalize every leaf to true-or-false on both planes — turns
    `!(object.ltv > 100)` into `!false` for a null `ltv` and admits the row."""
    assert admits(_expr("object.ltv > 100"), {"ltv": 200.0}) is True
    assert admits(_expr("object.ltv > 100"), {"ltv": 1.0}) is False
    assert admits(_expr("object.ltv > 100"), {"ltv": None}) is False
    assert admits(_expr("!(object.ltv > 100)"), {"ltv": None}) is False


def test_undecided_has_no_truth_value():
    """A sentinel that is falsy would let `if _truth(...)` read as the admission rule and be wrong
    for exactly the rows this slice exists for."""
    with pytest.raises(TypeError):
        bool(UNDECIDED)


# ---- the two planes, differentially ---------------------------------------------

# Physical rows. `full_name` is declared non-nullable and one row holds a null anyway: a table can
# contradict its spec — that is why `ambiguous_key` exists — and the runtime meets the undecidable
# leaf either way, which is half the reason "refuse any predicate that touches null" was not a way
# out of the null question.
ROWS = [
    {"id": "c1", "full_name": "Ada", "tier": "gold", "lifetime_value": 48210.5},
    {"id": "c2", "full_name": "Grace", "tier": "silver", "lifetime_value": None},
    {"id": "c3", "full_name": "Katherine", "tier": None, "lifetime_value": 100.0},
    {"id": "c4", "full_name": None, "tier": "bronze", "lifetime_value": None},
    {"id": "c5", "full_name": "gold", "tier": "gold", "lifetime_value": 1.0},
]

SCHEMA = pa.schema(
    [
        pa.field("id", pa.string()),
        pa.field("full_name", pa.string()),
        pa.field("tier", pa.string()),
        pa.field("lifetime_value", pa.float64()),
    ]
)

PREDICATES = [
    "object.ltv == null",
    "object.ltv != null",
    "object.tier == 'gold'",
    "object.tier != 'gold'",
    "!(object.tier == 'gold')",
    "object.ltv > 100",
    "object.ltv <= 100",
    "!(object.ltv > 100)",
    "object.ltv > 100 || object.tier == 'gold'",
    "object.tier == 'gold' || object.ltv > 100",
    "object.ltv > 100 && object.tier == 'gold'",
    "!(object.ltv > 100) || object.tier == 'gold'",
    "!(object.ltv > 100 && object.tier == 'gold')",
    "!(object.ltv > 100 || object.tier == 'gold')",
    "object.name == null",
    "object.name != null && object.ltv >= 100",
    "object.ltv == 100.0",
    "object.tier == object.name",
    "object.tier != object.name",
    "object.customerId != 'c1' && object.ltv != null",
    "!(object.name == null) || !(object.ltv == null)",
]


class ArrowCatalog:
    """Just enough catalog to hand DuckDB a real Arrow table with real nulls in it."""

    def __init__(self, rows):
        self.rows = rows

    def scan(self, table, columns=None, predicates=(), limit=None):
        names = list(columns) if columns else SCHEMA.names
        return pa.table(
            {name: [row[name] for row in self.rows] for name in names},
            schema=pa.schema([SCHEMA.field(name) for name in names]),
        )


def _rows_through_sql(ont, policies) -> set[str]:
    """The rows a real DuckDB returns under an already-decided `PolicySet`."""
    resolver = Resolver(
        ontology=ont,
        engine=DuckDBEngine(catalogs={"rest_main": ArrowCatalog(ROWS)}),
        policies=policies,
    )
    return {row["customerId"] for row in resolver.list("Customer", limit=500)}


def _through_sql(ont, text: str) -> set[str]:
    policies = bind_policies(ont, [Policy(name="p", object_type="Customer", rows=_expr(text))]).select(None)
    resolver = Resolver(
        ontology=ont,
        engine=DuckDBEngine(catalogs={"rest_main": ArrowCatalog(ROWS)}),
        policies=policies,
    )
    return {row["customerId"] for row in resolver.list("Customer", limit=500)}


def _in_process(obj, text: str) -> set[str]:
    expr = _expr(text)
    return {
        row["id"]
        for row in ROWS
        if admits(expr, {name: row[prop.column] for name, prop in obj.properties.items()})
    }


@pytest.mark.parametrize("text", PREDICATES)
def test_the_two_lowerings_admit_the_same_rows(customer, text):
    """The claim the whole slice rests on, asserted rather than argued.

    A predicate is compiled into the query on the read path and evaluated in process on the write
    path, because `ActionRuntime` reads through the `Catalog` port and never through the resolver.
    Two evaluators are two chances to mean different things, and the difference nulls cause is
    silent on both sides: SQL drops a row it could not decide about, and an in-process evaluator
    would have raised. This runs every predicate against every row through both and compares the
    sets, so a new node lowered one way and evaluated the other fails here rather than in
    somebody's lake."""
    ont, obj = customer
    assert _through_sql(ont, text) == _in_process(obj, text), text


def test_the_differential_corpus_is_actually_discriminating(customer):
    """A corpus every predicate admits everything from would pass the test above and prove nothing.
    At least one predicate must disagree with at least one other, and the ones that hinge on a null
    must not all be empty."""
    ont, _ = customer
    admitted = {text: _through_sql(ont, text) for text in PREDICATES}
    assert len({frozenset(v) for v in admitted.values()}) > 5
    # The case that kills the "totalize every leaf" alternative: `ltv` is null for c2 and c4, and a
    # two-valued emulation would admit both here.
    assert admitted["!(object.ltv > 100)"] == {"c3", "c5"}


def test_a_masked_property_can_still_be_filtered_on(customer):
    """Loom filtering, not the caller. §6.1 refuses a *caller's* filter on a masked property because
    an empty result is an oracle for its value; a policy filtering on what it also withholds hands
    the caller nothing but an absent row, and is the natural way to write "hide the column and the
    rows it identifies"."""
    ont, _ = customer
    policies = bind_policies(
        ont,
        [Policy(name="p", object_type="Customer", mask=("ltv",), rows=_expr("object.ltv != null"))],
    ).select(None)
    resolver = Resolver(
        ontology=ont,
        engine=DuckDBEngine(catalogs={"rest_main": ArrowCatalog(ROWS)}),
        policies=policies,
    )
    rows = resolver.list("Customer", limit=500)
    assert {r["customerId"] for r in rows} == {"c1", "c3", "c5"}
    assert all("ltv" not in r for r in rows)


def test_the_predicate_filters_before_the_page(customer):
    """Post-filtering in the resolver was never available: a page of 50 thinned to 31 by governance
    would report `hasMore: false` on a full table, and `offset` would step over rows the caller was
    never shown. In the `WHERE` clause it is `ORDER BY`/`LIMIT`/`OFFSET` that see the governed set."""
    ont, obj = customer
    policies = bind_policies(ont, [Policy(name="p", object_type="Customer", rows=_expr("object.ltv != null"))]).select(None)
    resolver = Resolver(
        ontology=ont,
        engine=DuckDBEngine(catalogs={"rest_main": ArrowCatalog(ROWS)}),
        policies=policies,
    )
    first = resolver.list("Customer", limit=2)
    second = resolver.list("Customer", limit=2, offset=2)
    # c2 and c4 are the withheld rows; page 1 and page 2 step over the governed set, not the table.
    assert [r["customerId"] for r in first] == ["c1", "c3"]
    assert [r["customerId"] for r in second] == ["c5"]

    compiled = resolver.engine.compile(_plan_for(resolver, obj))
    assert compiled.sql.index("WHERE") < compiled.sql.index("LIMIT")


def _plan_for(resolver, obj):
    from loom.query.ir import Project, Search

    return Project(
        source=Search(
            table=resolver._table(obj, "t0"),
            order_by=(obj.pk_property.column,),
            limit=2,
        ),
        columns=resolver._projection(obj, "t0"),
    )


# ---- the caller, in a predicate and in a guard -----------------------------------


CLAIMS = {"dept": ClaimType("string"), "groups": ClaimType("string", array=True)}


def _guard(text: str, claims=None):
    return check_guard(_expr(text), CLAIMS if claims is None else claims)


def test_a_claim_is_declared_or_it_is_refused(customer):
    """The check the declaration buys, and the reason `mcp.auth.claims` exists at all.

    Every other reference form in this language is checked against a declaration. Without one, a
    typo'd claim would fail *closed* — the guard undecided, the policy applied to everybody, the
    deployment withholding more than it was asked to with nothing anywhere saying why."""
    (problem,) = _guard("principal.dpet == 'hr'")
    assert "not a declared claim" in problem and "did you mean 'dept'" in problem

    # And a deployment that declared none at all gets a different sentence, because that is a
    # different mistake: not a misspelling, but a policy naming a caller nobody can name.
    (problem,) = _guard("principal.dept == 'hr'", claims={})
    assert "does not declare" in problem and "transport: http" in problem


def test_the_two_claims_every_believable_token_carries_need_no_declaration(customer):
    """`sub` and `iss` are `require`d by the verifier, so a policy naming either reads something
    every token this deployment believes already carries — and they are the only claims a guard can
    name that can never be undecided."""
    ont, obj = customer
    assert _guard("principal.sub == 'alice'", claims={}) == []
    assert check(_expr("object.customerId == principal.sub"), obj, ont.object_types, {}) == []


def test_a_guard_may_not_name_a_row_and_a_predicate_may_not_be_only_a_caller(customer):
    """The two grammars, each refusing the other's business.

    A guard is answered once per call, before any row is read, so `object.` in one is a condition
    with nothing to be a condition about. A predicate that names *only* the caller is the same
    answer for every row, which is the refusal `rows:` already had — and now it names the key that
    was wanted instead."""
    ont, obj = customer
    (problem,) = _guard("object.tier == 'gold'")
    assert "names a row" in problem and "'rows:' is for" in problem

    (problem,) = check(_expr("principal.sub == 'alice'"), obj, ont.object_types, CLAIMS)
    assert "names no property" in problem and "'when:' is for" in problem


def test_contains_is_a_guard_operator_and_not_a_lowering(customer):
    """**The subset rule survives contact with a claim by being restated rather than bent.**

    *Loom, not the engine, decides what every operator means* is a rule about expressions that must
    be answered **twice** — once by SQL and once in process. A guard is answered once, in this
    process, over a list only Loom holds, so the rule does not reach it and `contains` is legal
    there. Inside `rows:` the same operator would have to lower a list into SQL: an IR node this
    codebase does not have and a second evaluator to keep agreeing with it. Refused now, and
    wideable later without changing the meaning of anything already written."""
    ont, obj = customer
    assert _guard("principal.groups contains 'auditors'") == []

    (problem,) = check(_expr("principal.groups contains object.tier"), obj, ont.object_types, CLAIMS)
    assert "'when:' guard operator" in problem and "cannot filter rows" in problem
    assert "contains" in NOT_LOWERABLE and "contains" not in LOWERABLE


def test_a_list_claim_is_compared_by_contains_and_by_nothing_else(customer):
    """A list is comparable to nothing in this language: there is no property type it could equal,
    and `==` between a list and a string is a question §5 has no answer for."""
    ont, obj = customer
    (problem,) = _guard("principal.groups == 'auditors'")
    assert "compares against a list claim" in problem and "membership is 'contains'" in problem

    (problem,) = _guard("principal.dept contains 'hr'")
    assert "asks what a non-list contains" in problem

    (problem,) = check(_expr("object.tier == principal.groups"), obj, ont.object_types, CLAIMS)
    assert "compares against a list claim" in problem


def test_a_claim_is_type_checked_against_the_property_it_is_compared_with(customer):
    """The second thing the declaration buys: `comparable_to`, exactly as a property gets."""
    ont, obj = customer
    (problem,) = check(_expr("object.ltv == principal.dept"), obj, ont.object_types, CLAIMS)
    assert "compares 'double' with 'string'" in problem


def test_a_guard_that_names_no_claim_is_refused(customer):
    """A guard that is always true is a policy with no guard; one that is always false is a policy
    that never applies. Both read like a condition and are none — the same refusal a `rows:`
    predicate naming no property already had."""
    (problem,) = _guard("'a' == 'b'")
    assert "names no claim of the caller" in problem


def test_a_folded_predicate_is_one_the_existing_machinery_already_understands(customer):
    """Decision 1's mechanism: a principal is constant for the duration of a call, so what reaches
    either plane is a comparison against a literal. No node here is new."""
    ont, obj = customer
    folded = fold(_expr("object.customerId == principal.sub"), {"sub": "c2"})
    assert lower(folded, obj, "t0") == Compare("==", ColumnRef("t0", "id"), Const("c2"))
    assert admits(folded, {"customerId": "c2"}) and not admits(folded, {"customerId": "c1"})


@pytest.mark.parametrize(
    "text",
    [
        "object.customerId == principal.sub",
        "object.name != principal.sub",
        "!(object.customerId == principal.sub)",
        "object.customerId == principal.sub || object.tier == 'gold'",
        "object.customerId == principal.sub && object.ltv != null",
    ],
)
def test_a_missing_claim_is_undecided_on_both_planes(customer, text):
    """**The differential claim, extended to the one new way a leaf can fail to decide.**

    A token carrying no `sub` leaves a leaf neither plane can answer, and the two must call it
    undecided *by the rules they already had* — SQL's `NULL < NULL` and §5's refusal to order a
    null — so Kleene propagation does the rest and an undecided leaf under `||` beside a true one is
    still true. Substituting `null` instead would be wrong in the dangerous direction: `==` is
    null-safe here, so `object.customerId == null` would **admit** every row with a null key."""
    ont, obj = customer
    # The real path: the policy binds (it names `sub`, which every believable token carries), and
    # the fold happens at selection, for a caller whose claims this deployment could read nothing
    # of. `check()` never sees the generated leaf — it is not something an author can write.
    program = bind_policies(ont, [Policy(name="p", object_type="Customer", rows=_expr(text))], CLAIMS)
    nameless = Principal(subject="alice", issuer="https://issuer.test", client_id="c", claims={})
    decided = program.select(nameless)
    ((_, folded),) = decided.filters["Customer"]
    in_process = {
        row["id"]
        for row in ROWS
        if admits(folded, {name: row[prop.column] for name, prop in obj.properties.items()})
    }
    assert _rows_through_sql(ont, decided) == in_process


def test_a_claim_whose_value_is_not_its_declared_type_is_absent_rather_than_wrong():
    """Anything a policy cannot decide about must be *missing* rather than wrong: absence is already
    defined (undecided, and undecided withholds), while a wrong value would be decided against a
    type nobody checked — and the two planes do not compare a string with a number the same way."""
    principal = Principal(
        subject="alice",
        issuer="https://issuer.test",
        client_id="c",
        claims={
            "sub": "alice",
            "iss": "https://issuer.test",
            "dept": 7,                       # declared string
            "groups": ["ops", 3],            # declared string[]
            "role": "admin",                 # never declared
            "team": None,                    # a claim with no value decides nothing
        },
    )
    assert readable_claims(principal, {**CLAIMS, "team": ClaimType("string")}) == {
        "sub": "alice",
        "iss": "https://issuer.test",
    }
    assert readable_claims(None, CLAIMS) == {}
