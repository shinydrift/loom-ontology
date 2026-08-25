"""The migration planner — classified against a fake catalog rather than a live one.

Same bargain as test_physical.py: the `Catalog` port means the whole diff is testable with no
Iceberg stack, no disk, and no pyiceberg import. What's asserted here is almost entirely the
*classification* — that a change lands as safe, physical-safe, or breaking — because that
judgement is the only thing `plan` produces that a human is going to act on.
"""

from pathlib import Path

import pytest

from loom.catalog.base import Column, TableSchema
from loom.errors import Diagnostics
from loom.migrate import Severity, diff_ontology, render_plan
from loom.migrate.schema import desired_tables
from loom.model import Ontology
from loom.ontology import build

VALID = Path(__file__).parent / "fixtures" / "valid"

# The physical shape the worked-example fixture already matches — planning against this is a no-op.
CUSTOMERS = {
    "id": Column("id", "string", required=True, field_id=1),
    "full_name": Column("full_name", "string", required=True, field_id=2),
    "tier": Column("tier", "string", required=True, field_id=3),
    "lifetime_value": Column("lifetime_value", "double", required=False, field_id=4),
}
ORDERS = {
    "id": Column("id", "string", required=True, field_id=1),
    "customer_id": Column("customer_id", "string", required=True, field_id=2),
    "total_amount": Column("total_amount", "decimal(12,2)", required=True, field_id=3),
    "created_at": Column("created_at", "timestamptz", required=True, field_id=4),
}


class FakeCatalog:
    """An in-memory `Catalog` — introspection only, which is all the planner needs."""

    def __init__(self, name="rest_main", tables=None):
        self.name = name
        self.tables = tables if tables is not None else {"crm.customers": CUSTOMERS, "sales.orders": ORDERS}

    def table_exists(self, table: str) -> bool:
        return table in self.tables

    def describe(self, table: str) -> TableSchema:
        if table not in self.tables:
            raise RuntimeError(f"no such table {table}")
        return TableSchema(table=table, columns=self.tables[table])

    def scan(self, table, columns=None, predicates=(), limit=None):  # pragma: no cover - unused here
        raise NotImplementedError


@pytest.fixture(scope="module")
def ontology() -> Ontology:
    built, _ = build(VALID)
    return built


def _plan(ontology, tables=None, catalog_name="rest_main"):
    diag = Diagnostics()
    plan = diff_ontology(ontology, {catalog_name: FakeCatalog(catalog_name, tables)}, diag)
    assert diag.errors == [], f"unexpected planning errors: {[e.message for e in diag.errors]}"
    return plan


def _people(tmp_path: Path, person_prop: str, manager_prop: str) -> None:
    """Two objectTypes over one `hr.people` table, differing only in the extra property each maps.

    One declaration per file: the loader reads a directory of single-document YAML, so a
    multi-document file is a load error rather than two declarations."""
    for name, api_name, prop in (("person", "Person", person_prop), ("manager", "Manager", manager_prop)):
        (tmp_path / f"{name}.yaml").write_text(
            f"""
objectType:
  apiName: {api_name}
  primaryKey: id
  title: id
  backing: {{ catalog: main, table: hr.people }}
  properties:
    - {{ name: id, type: string, column: id, unique: true }}
    - {{ name: {prop} }}
"""
        )


def _column(plan, table: str, column: str):
    """The single change planned for one column — the shape most assertions below want."""
    matches = [c for t in plan.changes if t.table == table for c in t.columns if c.column == column]
    assert len(matches) == 1, f"expected exactly one change for {table}.{column}, got {matches}"
    return matches[0]


# --- the desired-state half -------------------------------------------------------------------


def test_desired_tables_cover_every_backing_table(ontology):
    diag = Diagnostics()
    tables = desired_tables(ontology, diag)
    assert diag.errors == []
    assert set(tables) == {("rest_main", "crm.customers"), ("rest_main", "sales.orders")}


def test_a_property_column_carries_its_declaring_source(ontology):
    tables = desired_tables(ontology, Diagnostics())
    ltv = tables[("rest_main", "crm.customers")].columns["lifetime_value"]
    assert ltv.iceberg_type == "double"
    assert ltv.required is False  # declared nullable
    assert ltv.source == "Customer.ltv"


def test_two_object_types_over_one_table_merge_rather_than_overwrite(tmp_path):
    """Modelling a subtype as a second objectType over the same table is normal — the planner has
    to want the union of their columns, not whichever one it saw last."""
    _people(tmp_path, "name, type: string, column: name", "reports, type: int, column: report_count")
    built, _ = build(tmp_path)
    tables = desired_tables(built, Diagnostics())
    table = tables[("main", "hr.people")]
    assert set(table.columns) == {"id", "name", "report_count"}
    # Declaration order, which for a directory of specs is sorted filename order — the plan is
    # printed for a human and diffed by CI, so it has to be the same on every run.
    assert table.sources == ("Manager", "Person")


def test_a_shared_column_is_required_only_if_every_declaration_requires_it(tmp_path):
    """One nullable mapping is enough to make the column optional — a required column would break
    the declaration that expects to write nulls into it."""
    _people(tmp_path, "note, type: string, column: note", "note, type: string, column: note, nullable: true")
    built, _ = build(tmp_path)
    tables = desired_tables(built, Diagnostics())
    assert tables[("main", "hr.people")].columns["note"].required is False


def test_a_shared_column_with_two_types_is_an_error_not_a_silent_pick(tmp_path):
    _people(tmp_path, "code, type: string, column: code", "code, type: int, column: code")
    built, _ = build(tmp_path)
    diag = Diagnostics()
    desired_tables(built, diag)
    messages = " | ".join(e.message for e in diag.errors)
    assert "both map column 'code' of 'hr.people'" in messages
    assert "disagree on its type (int vs string)" in messages


def test_a_through_table_wants_one_required_column_per_side(tmp_path):
    (tmp_path / "student.yaml").write_text(
        """
objectType:
  apiName: Student
  primaryKey: id
  title: id
  backing: { catalog: main, table: edu.students }
  properties:
    - { name: id, type: int, column: id, unique: true }
"""
    )
    (tmp_path / "course.yaml").write_text(
        """
objectType:
  apiName: Course
  primaryKey: code
  title: code
  backing: { catalog: main, table: edu.courses }
  properties:
    - { name: code, type: long, column: code, unique: true }
"""
    )
    (tmp_path / "enrolments.yaml").write_text(
        """
linkType:
  apiName: enrolments
  cardinality: many_to_many
  from: { objectType: Student, property: id }
  to: { objectType: Course, property: code }
  through: { catalog: main, table: edu.enrolments, fromColumn: student_id, toColumn: course_code }
"""
    )
    built, _ = build(tmp_path)
    table = desired_tables(built, Diagnostics())[("main", "edu.enrolments")]
    # Each side takes the type of the property it joins to, not a shared one — the ends here are
    # deliberately different (int/long, comparable by promotion) so a swap would show up.
    assert table.columns["student_id"].iceberg_type == "int"
    assert table.columns["course_code"].iceberg_type == "long"
    # A mapping row with a null end joins to nothing, so neither side is ever optional.
    assert all(c.required for c in table.columns.values())


# --- classification ---------------------------------------------------------------------------


def test_a_matching_catalog_plans_nothing(ontology):
    plan = _plan(ontology)
    assert plan.is_empty
    assert render_plan(plan) == "No changes — the catalog already matches the ontology."


def test_a_missing_table_is_a_creation_not_an_error(ontology):
    """The reason `plan` can't reuse `validate --physical`: that pass calls this an error."""
    plan = _plan(ontology, tables={"sales.orders": ORDERS})
    assert len(plan.changes) == 1
    created = plan.changes[0]
    assert (created.action, created.table) == ("create", "crm.customers")
    assert [c.column for c in created.columns] == ["id", "full_name", "tier", "lifetime_value"]
    assert created.severity is Severity.SAFE


def test_a_required_column_on_a_new_table_is_still_safe(ontology):
    """No existing rows means nothing a required column could invalidate."""
    plan = _plan(ontology, tables={"crm.customers": CUSTOMERS})
    change = _column(plan, "sales.orders", "created_at")
    assert (change.kind, change.severity) == ("add", Severity.SAFE)


def test_adding_an_optional_column_is_safe(ontology):
    live = {k: v for k, v in CUSTOMERS.items() if k != "lifetime_value"}
    plan = _plan(ontology, {"crm.customers": live, "sales.orders": ORDERS})
    change = _column(plan, "crm.customers", "lifetime_value")
    assert (change.kind, change.severity) == ("add", Severity.SAFE)
    assert change.detail == "double optional"


def test_adding_a_required_column_to_an_existing_table_is_breaking(ontology):
    live = {k: v for k, v in CUSTOMERS.items() if k != "tier"}
    plan = _plan(ontology, {"crm.customers": live, "sales.orders": ORDERS})
    change = _column(plan, "crm.customers", "tier")
    assert (change.kind, change.severity) == ("add", Severity.BREAKING)
    assert "add it nullable, backfill" in change.reason


def test_a_widening_type_change_is_physical_safe(ontology):
    """`Customer.ltv` is declared double, and Iceberg widens a float into it by field id."""
    live = dict(CUSTOMERS, lifetime_value=Column("lifetime_value", "float", required=False, field_id=4))
    plan = _plan(ontology, {"crm.customers": live, "sales.orders": ORDERS})
    change = _column(plan, "crm.customers", "lifetime_value")
    assert (change.kind, change.severity) == ("promote", Severity.PHYSICAL_SAFE)
    assert change.detail == "float -> double"
    assert "field id 4" in change.reason


@pytest.mark.parametrize("current", ["int", "long"])
def test_a_change_iceberg_cannot_alter_is_breaking_however_readable_it_is(ontology, current):
    """The one a probe found by running `loom apply` into a real catalog.

    An `int` column under a `double` property *reads* fine — `promotable` says so and
    `loom validate --physical` accepts it on that basis. Iceberg still will not alter the stored
    type: its promotion set is `int -> long` and `float -> double`, and nothing else. Classifying
    this as physical-safe produced a plan `apply` could not execute, and the operator met the
    difference as pyiceberg's `Cannot change column type` half way through the run."""
    live = dict(CUSTOMERS, lifetime_value=Column("lifetime_value", current, required=False, field_id=4))
    plan = _plan(ontology, {"crm.customers": live, "sales.orders": ORDERS})
    change = _column(plan, "crm.customers", "lifetime_value")
    assert (change.kind, change.severity) == ("retype", Severity.BREAKING)
    assert change.detail == f"{current} -> double"
    assert "does not promote" in change.reason


def test_a_narrowing_type_change_is_breaking(ontology):
    live = dict(ORDERS, customer_id=Column("customer_id", "double", required=True, field_id=2))
    plan = _plan(ontology, {"crm.customers": CUSTOMERS, "sales.orders": live})
    change = _column(plan, "sales.orders", "customer_id")
    assert (change.kind, change.severity) == ("retype", Severity.BREAKING)
    assert "does not promote" in change.reason


def test_loosening_a_constraint_is_safe(ontology):
    """Every existing row already satisfies a constraint being dropped."""
    live = dict(CUSTOMERS, lifetime_value=Column("lifetime_value", "double", required=True, field_id=4))
    plan = _plan(ontology, {"crm.customers": live, "sales.orders": ORDERS})
    change = _column(plan, "crm.customers", "lifetime_value")
    assert (change.kind, change.severity) == ("loosen", Severity.SAFE)
    assert change.detail == "required -> optional"


def test_tightening_a_constraint_is_breaking(ontology):
    live = dict(CUSTOMERS, full_name=Column("full_name", "string", required=False, field_id=2))
    plan = _plan(ontology, {"crm.customers": live, "sales.orders": ORDERS})
    change = _column(plan, "crm.customers", "full_name")
    assert (change.kind, change.severity) == ("tighten", Severity.BREAKING)
    assert "may already hold nulls" in change.reason


def test_a_type_and_a_nullability_change_on_one_column_are_reported_separately(ontology):
    """They are two distinct operations with two distinct severities — collapsing them would hide
    the breaking half behind the safe one."""
    live = dict(CUSTOMERS, lifetime_value=Column("lifetime_value", "float", required=True, field_id=4))
    plan = _plan(ontology, {"crm.customers": live, "sales.orders": ORDERS})
    kinds = {c.kind: c.severity for t in plan.changes for c in t.columns}
    assert kinds == {"promote": Severity.PHYSICAL_SAFE, "loosen": Severity.SAFE}


def test_decimal_precision_is_not_a_promotion(ontology):
    live = dict(ORDERS, total_amount=Column("total_amount", "decimal(10,2)", required=True, field_id=3))
    plan = _plan(ontology, {"crm.customers": CUSTOMERS, "sales.orders": live})
    change = _column(plan, "sales.orders", "total_amount")
    assert change.severity is Severity.BREAKING
    assert change.detail == "decimal(10,2) -> decimal(12,2)"


# --- the no-drops rule ------------------------------------------------------------------------


def test_an_unmapped_column_is_reported_but_never_dropped(ontology):
    """An objectType maps a subset of a table's columns. A column no property mentions is someone
    else's data, not a deleted property."""
    live = dict(CUSTOMERS, legacy_notes=Column("legacy_notes", "string", required=False, field_id=9))
    plan = _plan(ontology, {"crm.customers": live, "sales.orders": ORDERS})
    assert [(u.table, u.columns) for u in plan.unmanaged] == [("crm.customers", ("legacy_notes",))]
    assert "drop" not in render_plan(plan)


def test_unmapped_columns_alone_are_not_a_plan(ontology):
    """The case that decides the shape: an existing lake table nearly always carries columns the
    ontology doesn't map. If those counted as changes, `plan` would never once report a clean run
    — and "no changes" is the answer it has to be able to give."""
    live = dict(CUSTOMERS, legacy_notes=Column("legacy_notes", "string", required=False, field_id=9))
    plan = _plan(ontology, {"crm.customers": live, "sales.orders": ORDERS})
    assert plan.changes == ()
    assert plan.is_empty
    out = render_plan(plan)
    assert out.startswith("No changes — the catalog already matches the ontology.")
    assert "Unmanaged — columns no property maps, left untouched:" in out
    assert "  · rest_main.crm.customers: legacy_notes" in out


def test_unmapped_columns_are_listed_apart_from_real_changes(ontology):
    """Under the table they'd read as queued up to be dropped, which is the one thing `apply`
    will never do to them."""
    live = dict(
        CUSTOMERS,
        full_name=Column("full_name", "string", required=False, field_id=2),
        legacy_notes=Column("legacy_notes", "string", required=False, field_id=9),
    )
    plan = _plan(ontology, {"crm.customers": live, "sales.orders": ORDERS})
    out = render_plan(plan)
    assert [c.column for c in plan.changes[0].columns] == ["full_name"]
    assert out.index("! full_name") < out.index("Plan: ") < out.index("legacy_notes")


# --- failure reporting ------------------------------------------------------------------------


def test_an_undeclared_catalog_is_an_error_that_names_its_declarations(ontology):
    diag = Diagnostics()
    diff_ontology(ontology, {"other": FakeCatalog("other")}, diag)
    messages = " | ".join(e.message for e in diag.errors)
    assert "catalog 'rest_main', which is not declared in loom.yaml" in messages
    assert any(e.hint and "Customer" in e.hint for e in diag.errors)


def test_an_un_introspectable_table_is_one_error_and_the_rest_still_plans(ontology):
    class Broken(FakeCatalog):
        def describe(self, table):
            if table == "crm.customers":
                raise RuntimeError("metastore said no")
            return super().describe(table)

    diag = Diagnostics()
    live = {"crm.customers": CUSTOMERS, "sales.orders": {k: v for k, v in ORDERS.items() if k != "created_at"}}
    plan = diff_ontology(ontology, {"rest_main": Broken("rest_main", live)}, diag)
    assert "could not introspect 'crm.customers': metastore said no" in " | ".join(e.message for e in diag.errors)
    # The other table is still planned — the pass accumulates rather than aborting.
    assert [c.table for c in plan.changes] == ["sales.orders"]


# --- rendering --------------------------------------------------------------------------------


def test_the_render_marks_severity_in_the_left_margin(ontology):
    live = dict(CUSTOMERS, full_name=Column("full_name", "string", required=False, field_id=2))
    plan = _plan(ontology, {"crm.customers": live, "sales.orders": ORDERS})
    out = render_plan(plan, title="ontology")

    assert out.startswith("Loom plan — ontology")
    # A breaking column drags its table's marker to `!`, so severity is visible before the text.
    assert "  ! rest_main.crm.customers" in out
    assert "      ! full_name" in out
    assert "breaking" in out
    assert "Plan: 0 to create, 1 to change · 1 breaking" in out
    assert "`loom apply` will refuse this plan" in out


def test_a_breaking_add_is_marked_by_severity_not_by_kind(ontology):
    """`+` is the marker that means "free". A required column added to a populated table is an
    add, but it is the one change on the page that isn't free."""
    live = {k: v for k, v in CUSTOMERS.items() if k != "tier"}
    plan = _plan(ontology, {"crm.customers": live, "sales.orders": ORDERS})
    out = render_plan(plan)
    assert "      ! tier" in out
    assert "      + tier" not in out


def test_a_promotion_renders_its_field_id_reasoning(ontology):
    live = dict(CUSTOMERS, lifetime_value=Column("lifetime_value", "float", required=False, field_id=4))
    plan = _plan(ontology, {"crm.customers": live, "sales.orders": ORDERS})
    out = render_plan(plan)
    assert "      ~ lifetime_value  float -> double  physical-safe" in out
    assert "existing data files are not rewritten" in out


def test_unmanaged_columns_render_in_table_order(ontology):
    live = dict(
        CUSTOMERS,
        legacy_notes=Column("legacy_notes", "string", required=False, field_id=9),
        etl_batch=Column("etl_batch", "string", required=False, field_id=10),
    )
    plan = _plan(ontology, {"crm.customers": live, "sales.orders": ORDERS})
    assert "  · rest_main.crm.customers: legacy_notes, etl_batch" in render_plan(plan)


def test_a_creation_renders_every_column_without_per_column_labels(ontology):
    plan = _plan(ontology, tables={"sales.orders": ORDERS})
    out = render_plan(plan)
    assert "  + rest_main.crm.customers — create table · Customer" in out
    assert "      + id" in out
    # All trivially safe on a fresh table — labelling each one is a column of noise.
    assert "safe" not in out.split("Plan:")[0]
    assert "Plan: 1 to create, 0 to change · 4 safe" in out
