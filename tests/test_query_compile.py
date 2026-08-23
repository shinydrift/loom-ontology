"""IR -> DuckDB SQL. `Engine.compile()` is pure, so the generated SQL is asserted directly."""

from dataclasses import replace

import pytest

from loom.query.engine import EngineError
from loom.query.engines.duckdb import DuckDBEngine
from loom.query.ir import (
    Column,
    ColumnRef,
    Compare,
    Const,
    Contains,
    Eq,
    GetByKey,
    In,
    Match,
    Project,
    Search,
    TableRef,
    ThroughRef,
    Traverse,
    VectorRef,
)

CUST = TableRef(catalog="main", table="crm.customers", alias="t0")
ORDERS = TableRef(catalog="main", table="sales.orders", alias="t1")

COLUMNS = (
    Column(alias="t0", column="id", output="customerId"),
    Column(alias="t0", column="full_name", output="name"),
)

VECTORS = VectorRef(
    table=TableRef(catalog="main", table="_loom_meta.vectors__Customer", alias="v0"),
    key_column="key",
    vector_column="vector",
    model_column="model",
    dims_column="dims",
    property_column="property",
    model="stub-v1",
    property="name",
)


def _match(**kwargs):
    """A ranked read over the customers table, with the sidecar joined on the primary key."""
    fields = dict(
        table=CUST,
        vectors=VECTORS,
        key_column="id",
        query=(0.5, -0.5),
        score_as="_loom_score",
        order_by=("id",),
        limit=10,
    )
    fields.update(kwargs)
    columns = fields.pop("columns", COLUMNS)
    return Project(source=Match(**fields), columns=columns)


@pytest.fixture
def engine():
    return DuckDBEngine(catalogs={})


def test_get_by_key(engine):
    plan = Project(source=GetByKey(table=CUST, key_column="id", key_value="c1"), columns=COLUMNS)
    q = engine.compile(plan)
    assert q.sql == (
        'SELECT "t0"."id" AS "customerId", "t0"."full_name" AS "name" '
        'FROM "t0" WHERE "t0"."id" = ? LIMIT 2'
    )
    assert q.params == ("c1",)


def test_get_by_key_pushes_the_key_down_and_limits_the_scan(engine):
    """The whole point: fetching one object must not read the table."""
    plan = Project(source=GetByKey(table=CUST, key_column="id", key_value="c1"), columns=COLUMNS)
    (scan,) = engine.compile(plan).scans
    assert scan.alias == "t0" and scan.table == "crm.customers"
    assert scan.predicates == (("id", "c1"),)
    assert scan.limit == 2
    assert scan.columns == ("full_name", "id")


def test_get_by_key_limits_to_two_so_duplicates_are_visible(engine):
    """LIMIT 1 would hide a primary key the backing table doesn't actually enforce."""
    plan = Project(source=GetByKey(table=CUST, key_column="id", key_value="c1"), columns=COLUMNS)
    assert engine.compile(plan).sql.endswith("LIMIT 2")


def test_search_with_no_filters_is_a_paged_ordered_scan(engine):
    plan = Project(
        source=Search(table=CUST, order_by=("id",), limit=10, offset=20), columns=COLUMNS
    )
    q = engine.compile(plan)
    assert q.sql == (
        'SELECT "t0"."id" AS "customerId", "t0"."full_name" AS "name" '
        'FROM "t0" ORDER BY "t0"."id" LIMIT ? OFFSET ?'
    )
    assert q.params == (10, 20)


def test_search_orders_by_key_so_pages_are_stable(engine):
    """Without ORDER BY, page 2 of a paginated tool call is unrelated to page 1."""
    plan = Project(source=Search(table=CUST, order_by=("id",), limit=5), columns=COLUMNS)
    assert 'ORDER BY "t0"."id"' in engine.compile(plan).sql


def _eq(column, value):
    return Compare("==", ColumnRef("t0", column), Const(value))


def test_equality_filter_is_both_sql_and_pushdown(engine):
    """`IS NOT DISTINCT FROM`, not `=`, since a caller's `eq` is the node a policy's is.

    For a bound non-null parameter the two select the same rows; the spelling is what makes
    `{"eq": null}` and `object.x == null` one answer instead of two."""
    plan = Project(
        source=Search(table=CUST, filters=(_eq("tier", "gold"),), order_by=("id",), limit=5),
        columns=COLUMNS,
    )
    q = engine.compile(plan)
    assert '"t0"."tier" IS NOT DISTINCT FROM ?' in q.sql
    assert q.params == ("gold", 5)
    assert q.scans[0].predicates == (("tier", "gold"),)


def test_range_filters_and_between_compile_to_a_conjunction(engine):
    """The acceptance shape: two comparisons on one column, ANDed, neither pushed down."""
    plan = Project(
        source=Search(
            table=CUST,
            filters=(
                Compare(">=", ColumnRef("t0", "lifetime_value"), Const(100.0)),
                Compare("<", ColumnRef("t0", "lifetime_value"), Const(500.0)),
            ),
            order_by=("id",),
            limit=5,
        ),
        columns=COLUMNS,
    )
    q = engine.compile(plan)
    assert '"t0"."lifetime_value" >= ? AND "t0"."lifetime_value" < ?' in q.sql
    assert q.params == (100.0, 500.0, 5)
    # The pushdown channel is a (column, value) pair by shape, so a range has no spelling in it.
    assert q.scans[0].predicates == ()


def test_a_filtered_column_is_scanned_even_when_it_is_not_projected(engine):
    """A range on a column outside the projection still has to be read to be filtered on."""
    plan = Project(
        source=Search(
            table=CUST,
            filters=(Compare(">", ColumnRef("t0", "lifetime_value"), Const(1.0)),),
            order_by=("id",),
            limit=5,
        ),
        columns=(Column("t0", "id", "customerId"),),
    )
    assert "lifetime_value" in engine.compile(plan).scans[0].columns


def test_contains_filter_uses_ilike_and_is_not_pushed_down(engine):
    plan = Project(
        source=Search(table=CUST, filters=(Contains("t0", "full_name", "ada"),), order_by=("id",), limit=5),
        columns=COLUMNS,
    )
    q = engine.compile(plan)
    assert '"t0"."full_name" ILIKE ? ESCAPE \'\\\'' in q.sql
    assert q.params == ("%ada%", 5)
    assert q.scans[0].predicates == ()  # substring match is not an Iceberg predicate


@pytest.mark.parametrize(
    "value,expected",
    [("50%", "%50\\%%"), ("a_b", "%a\\_b%"), ("back\\slash", "%back\\\\slash%")],
)
def test_like_metacharacters_in_user_input_are_escaped(engine, value, expected):
    """A search for "50%" means those two characters, not "starts with 50"."""
    plan = Project(
        source=Search(table=CUST, filters=(Contains("t0", "full_name", value),), order_by=("id",), limit=5),
        columns=COLUMNS,
    )
    assert engine.compile(plan).params[0] == expected


def test_null_equality_becomes_is_null(engine):
    """`= NULL` never matches, so an explicit null filter has to compile differently."""
    plan = Project(
        source=Search(table=CUST, filters=(_eq("lifetime_value", None),), order_by=("id",), limit=5),
        columns=COLUMNS,
    )
    q = engine.compile(plan)
    assert '"t0"."lifetime_value" IS NULL' in q.sql
    assert q.params == (5,)  # no bind param for the null


def _search(*filters, columns=COLUMNS):
    return Project(
        source=Search(table=CUST, filters=filters, order_by=("id",), limit=5), columns=columns
    )


def test_membership_binds_every_value(engine):
    q = engine.compile(_search(In("t0", "tier", ("gold", "platinum"))))
    assert '"t0"."tier" IN (?, ?)' in q.sql
    assert q.params == ("gold", "platinum", 5)


def test_membership_of_only_a_null_is_the_same_sql_as_an_equality_null(engine):
    """`{"in": [null]}` abbreviates `{"eq": null}`, so it had better compile to what that does.

    SQL's `IN` would never match here — the list's null is compared with `=` — which is the whole
    reason `_in` lifts a null out rather than binding it as a parameter."""
    membership = engine.compile(_search(In("t0", "lifetime_value", (None,))))
    equality = engine.compile(_search(_eq("lifetime_value", None)))
    assert '"t0"."lifetime_value" IS NULL' in membership.sql
    assert membership.sql == equality.sql
    assert membership.params == equality.params == (5,)


def test_membership_with_a_null_among_values_is_a_disjunction(engine):
    """The null cannot ride in the `IN` list, so the clause is the two halves ORed."""
    q = engine.compile(_search(In("t0", "tier", ("gold", None))))
    assert '("t0"."tier" IN (?) OR "t0"."tier" IS NULL)' in q.sql
    assert q.params == ("gold", 5)


def test_membership_is_not_pushed_down(engine):
    """Not a channel too narrow to carry it — `ScanRequest.predicates` is ANDed, so one hint per
    value would prune to the rows matching *every* value: an empty scan, silently."""
    q = engine.compile(_search(In("t0", "tier", ("gold", "platinum"))))
    assert q.scans[0].predicates == ()
    assert "tier" in q.scans[0].columns  # still scanned, since the WHERE reads it


def test_membership_on_an_unprojected_column_is_still_scanned(engine):
    q = engine.compile(
        _search(In("t0", "lifetime_value", (1.0,)), columns=(Column("t0", "id", "customerId"),))
    )
    assert "lifetime_value" in q.scans[0].columns


def test_traverse_joins_and_anchors_on_the_source_key(engine):
    plan = Project(
        source=Traverse(
            from_table=TableRef("main", "crm.customers", "t1"),
            to_table=TableRef("main", "sales.orders", "t0"),
            from_column="id",
            to_column="customer_id",
            anchor=Eq("t1", "id", "c2"),
            order_by=("id",),
            limit=50,
        ),
        columns=(Column("t0", "id", "orderId"),),
    )
    q = engine.compile(plan)
    assert q.sql == (
        'SELECT "t0"."id" AS "orderId" FROM "t0" '
        'JOIN "t1" ON "t0"."customer_id" = "t1"."id" '
        'WHERE "t1"."id" = ? ORDER BY "t0"."id" LIMIT ?'
    )
    assert q.params == ("c2", 50)


def test_traverse_pushes_the_anchor_into_the_source_scan(engine):
    """The join stays cheap because the anchored side is pruned to one row by the catalog."""
    plan = Project(
        source=Traverse(
            from_table=TableRef("main", "crm.customers", "t1"),
            to_table=TableRef("main", "sales.orders", "t0"),
            from_column="id",
            to_column="customer_id",
            anchor=Eq("t1", "id", "c2"),
            order_by=("id",),
            limit=50,
        ),
        columns=(Column("t0", "id", "orderId"),),
    )
    scans = {s.alias: s for s in engine.compile(plan).scans}
    assert scans["t1"].predicates == (("id", "c2"),)
    assert scans["t0"].predicates == ()
    assert scans["t0"].columns == ("customer_id", "id")


def test_many_to_many_traverse_goes_through_the_mapping_table(engine):
    plan = Project(
        source=Traverse(
            from_table=TableRef("main", "crm.customers", "t1"),
            to_table=TableRef("main", "catalog.products", "t0"),
            from_column="id",
            to_column="sku",
            anchor=Eq("t1", "id", "c1"),
            through=ThroughRef(
                table=TableRef("main", "crm.customer_products", "m0"),
                from_column="customer_id",
                to_column="product_sku",
            ),
            order_by=("sku",),
            limit=50,
        ),
        columns=(Column("t0", "sku", "sku"),),
    )
    q = engine.compile(plan)
    assert (
        'FROM "t0" '
        'JOIN "m0" ON "t0"."sku" = "m0"."product_sku" '
        'JOIN "t1" ON "m0"."customer_id" = "t1"."id"'
    ) in q.sql
    assert {s.alias for s in q.scans} == {"t0", "t1", "m0"}


def test_identifiers_with_quotes_are_escaped(engine):
    weird = TableRef(catalog="main", table='x."y"', alias='a"b')
    plan = Project(
        source=GetByKey(table=weird, key_column='c"d', key_value=1),
        columns=(Column('a"b', 'c"d', 'out"put'),),
    )
    assert '"a""b"."c""d" AS "out""put"' in engine.compile(plan).sql


def test_offset_without_a_limit_is_refused(engine):
    """DuckDB requires LIMIT before OFFSET; failing loudly beats emitting invalid SQL."""
    plan = Project(source=Search(table=CUST, order_by=("id",), limit=None, offset=10), columns=COLUMNS)
    with pytest.raises(EngineError, match="OFFSET requires a LIMIT"):
        engine.compile(plan)


def test_a_plan_must_be_rooted_in_project(engine):
    with pytest.raises(EngineError, match="rooted in Project"):
        engine.compile(GetByKey(table=CUST, key_column="id", key_value="c1"))


def test_capabilities_report_no_native_merge():
    """Writes go through the Iceberg catalog, not DuckDB — M3 depends on knowing that."""
    caps = DuckDBEngine(catalogs={}).capabilities()
    assert caps.name == "duckdb"
    assert caps.joins and caps.offset and caps.case_insensitive_like
    assert caps.native_merge is False


# ---- match ---------------------------------------------------------------------
#
# The fourth source node, and the first whose SELECT list holds something no table has. Three
# things are asserted here that nothing else can see: the parameter *order* across a clause
# boundary (the query vector is bound before the guard, because it appears before it in the SQL),
# the fixed-width cast on both sides of the distance, and that the score sorts before the tie-break
# rather than after it.


def test_match_joins_the_sidecar_and_ranks_by_distance(engine):
    q = engine.compile(_match())
    assert q.sql == (
        'SELECT "t0"."id" AS "customerId", "t0"."full_name" AS "name", '
        'array_cosine_similarity(CAST("v0"."vector" AS FLOAT[2]), CAST(? AS FLOAT[2])) AS "_loom_score" '
        'FROM "t0" JOIN "v0" ON "t0"."id" = "v0"."key" '
        'WHERE "v0"."vector" IS NOT NULL AND "v0"."model" = ? AND "v0"."dims" = ? '
        'AND "v0"."property" = ? '
        'ORDER BY "_loom_score" DESC, "t0"."id" LIMIT ?'
    )
    assert q.params == ([0.5, -0.5], "stub-v1", 2, "name", 10)


def test_the_score_sorts_before_the_tie_break(engine):
    """The tie-break is what makes the order total, so page 2 is the continuation of page 1 rather
    than an unrelated draw from the same set."""
    sql = engine.compile(_match(offset=10)).sql
    assert 'ORDER BY "_loom_score" DESC, "t0"."id" LIMIT ? OFFSET ?' in sql


def test_the_width_in_the_cast_comes_from_the_query_vector(engine):
    """`dims` is never declared anywhere — the width the ranking happens at is the width of the
    vector the provider just returned, stated in the SQL rather than inferred from a row."""
    sql = engine.compile(_match(query=(1.0, 2.0, 3.0, 4.0))).sql
    assert sql.count("FLOAT[4]") == 2
    assert "FLOAT[2]" not in sql


def test_the_comparability_guard_is_both_a_clause_and_a_pushdown(engine):
    """In the `WHERE` for correctness — the distance function raises on two widths rather than
    answering — and in the scan as the equality pair that channel can actually carry."""
    q = engine.compile(_match())
    scans = {s.alias: s for s in q.scans}
    assert scans["v0"].predicates == (("model", "stub-v1"), ("dims", 2), ("property", "name"))
    assert scans["v0"].table == "_loom_meta.vectors__Customer"
    assert scans["v0"].columns == ("dims", "key", "model", "property", "vector")
    for clause in ('"v0"."model" = ?', '"v0"."dims" = ?', '"v0"."property" = ?'):
        assert clause in q.sql


def test_the_guard_covers_the_property_the_vector_was_made_from(engine):
    """The narrowest window in the milestone and the least visible: re-point `semantic:` from one
    column to another and every `source_hash` changes, so a reconcile fixes it — but between the
    deploy and the reconcile the sidecar holds vectors of the *old* text, and without this clause
    they would be ranked under an envelope naming the new property."""
    q = engine.compile(_match(vectors=replace(VECTORS, property="bio")))
    assert '"v0"."property" = ?' in q.sql
    assert q.params[3] == "bio"
    assert ("property", "bio") in {s.alias: s for s in q.scans}["v0"].predicates


def test_a_projected_sidecar_column_is_scanned(engine):
    """The stamp the envelope reports is an ordinary projection off `v0`, so the scan picks it up
    the same way a governed column does."""
    plan = _match(columns=(*COLUMNS, Column("v0", "embedded_at", "_loom_embedded_at")))
    q = engine.compile(plan)
    scans = {s.alias: s for s in q.scans}
    assert "embedded_at" in scans["v0"].columns
    assert '"v0"."embedded_at" AS "_loom_embedded_at"' in q.sql


def test_the_ranked_side_is_never_limited_in_the_scan(engine):
    """`LIMIT k` bounds what comes back, never what has to be measured: every surviving row is a
    candidate until its distance is computed."""
    scans = {s.alias: s for s in engine.compile(_match(limit=1)).scans}
    assert scans["t0"].limit is None and scans["v0"].limit is None


def test_match_filters_narrow_before_the_ranking(engine):
    """The same conjunction `search` takes, in the same `WHERE`, ahead of the ORDER BY — so a
    filtered call ranks fewer rows rather than re-ranking the ones it kept."""
    q = engine.compile(
        _match(filters=(Compare("==", ColumnRef("t0", "tier"), Const("gold")),))
    )
    assert 'AND "t0"."tier" IS NOT DISTINCT FROM ?' in q.sql
    assert q.sql.index('"t0"."tier"') < q.sql.index("ORDER BY")
    # The vector, then the guard, then the filter: the order the clauses appear in.
    assert q.params == ([0.5, -0.5], "stub-v1", 2, "name", "gold", 10)
    scans = {s.alias: s for s in q.scans}
    assert scans["t0"].predicates == (("tier", "gold"),)
    assert "tier" in scans["t0"].columns


def test_match_still_governs_the_object_table(engine):
    """A ranked read is governed on the end that stands for an object type. The sidecar is the
    other end and stands for none, which is `ThroughRef`'s answer rather than a second one."""
    governed = TableRef(
        catalog="main",
        table="crm.customers",
        alias="t0",
        predicate=Compare("==", ColumnRef("t0", "region"), Const("emea")),
    )
    q = engine.compile(_match(table=governed))
    assert 'AND "t0"."region" IS NOT DISTINCT FROM ?' in q.sql
    assert q.params == ([0.5, -0.5], "stub-v1", 2, "name", "emea", 10)
    assert "region" in {s.alias: s for s in q.scans}["t0"].columns
    # Never a pushdown hint — that channel is advisory and a policy is not.
    assert {s.alias: s for s in q.scans}["t0"].predicates == ()


def test_capabilities_claim_vector_search():
    """The flag a spec declaring `semantic:` demands, and the one this adapter can answer for
    because `array_cosine_similarity` is core DuckDB rather than an extension."""
    assert DuckDBEngine(catalogs={}).capabilities().vector_search is True
