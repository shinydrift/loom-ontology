"""IR -> DuckDB SQL. `Engine.compile()` is pure, so the generated SQL is asserted directly."""

import pytest

from loom.query.engine import EngineError
from loom.query.engines.duckdb import DuckDBEngine
from loom.query.ir import Column, Contains, Eq, GetByKey, Project, Search, TableRef, ThroughRef, Traverse

CUST = TableRef(catalog="main", table="crm.customers", alias="t0")
ORDERS = TableRef(catalog="main", table="sales.orders", alias="t1")

COLUMNS = (
    Column(alias="t0", column="id", output="customerId"),
    Column(alias="t0", column="full_name", output="name"),
)


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


def test_equality_filter_is_both_sql_and_pushdown(engine):
    plan = Project(
        source=Search(table=CUST, filters=(Eq("t0", "tier", "gold"),), order_by=("id",), limit=5),
        columns=COLUMNS,
    )
    q = engine.compile(plan)
    assert '"t0"."tier" = ?' in q.sql
    assert q.params == ("gold", 5)
    assert q.scans[0].predicates == (("tier", "gold"),)


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
        source=Search(table=CUST, filters=(Eq("t0", "lifetime_value", None),), order_by=("id",), limit=5),
        columns=COLUMNS,
    )
    q = engine.compile(plan)
    assert '"t0"."lifetime_value" IS NULL' in q.sql
    assert q.params == (5,)  # no bind param for the null


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
