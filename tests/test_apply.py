"""The executor — against a fake catalog that records what it was told to do.

Same bargain as test_plan.py: the ports mean the whole of `apply` is testable with no Iceberg
stack. What's asserted here is the *policy* — refuse a breaking plan whole, one transaction per
table, stop at the first failure, never apply twice — because that is the part a real catalog
would only tell us about by breaking someone's table. `test_apply_iceberg.py` proves the same
sequence against real pyiceberg.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loom.catalog.base import CatalogError, Column, TableSchema
from loom.errors import Diagnostics
from loom.migrate import (
    APPLIED,
    FAILED,
    REFUSED,
    UP_TO_DATE,
    MetaStore,
    apply_plan,
    diff_ontology,
    render_apply,
    snapshot_spec,
)
from loom.migrate.meta import META_TABLE, STATUS_APPLIED
from loom.ontology import build

VALID = Path(__file__).parent / "fixtures" / "valid"

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


class FakeWritableCatalog:
    """An in-memory catalog implementing both ports, plus a log of what was asked of it.

    The log is the point: it's how a test asserts that a refused plan issued *no* writes, and that
    a table's edits arrived as one call (one transaction) rather than several."""

    def __init__(self, name="rest_main", tables=None, fail_on: str | None = None):
        self.tables: dict[str, dict[str, Column]] = dict(tables if tables is not None else {})
        self.name = name
        self.rows: dict[str, list[dict]] = {}
        self.properties: dict[str, dict[str, str]] = {}
        self.namespaces: set[str] = {t.rpartition(".")[0] for t in self.tables}
        self.log: list[tuple] = []
        self.fail_on = fail_on  # table whose write raises, to exercise the partial path

    # --- read port
    def table_exists(self, table: str) -> bool:
        return table in self.tables

    def describe(self, table: str) -> TableSchema:
        if table not in self.tables:
            raise CatalogError(f"no such table {table}")
        return TableSchema(table=table, columns=self.tables[table])

    def scan(self, table, columns=None, predicates=(), limit=None):
        return _FakeArrow(self.rows.get(table, []))

    # --- write port
    def ensure_namespace(self, table: str) -> bool:
        namespace = table.rpartition(".")[0]
        if not namespace or namespace in self.namespaces:
            return False
        self.namespaces.add(namespace)
        self.log.append(("namespace", namespace))
        return True

    def create_table(self, table, columns, properties: Mapping[str, str] = {}):
        self._guard(table)
        self.tables[table] = {
            c.name: Column(c.name, c.iceberg_type, c.required, field_id=i)
            for i, c in enumerate(columns, start=1)
        }
        self.properties[table] = dict(properties)
        self.log.append(("create", table, tuple(c.name for c in columns)))

    def alter_table(self, table, edits, properties: Mapping[str, str] = {}):
        self._guard(table)
        live = dict(self.tables[table])
        for edit in edits:
            col = edit.column
            if edit.op == "add":
                live[col.name] = Column(col.name, col.iceberg_type, required=False, field_id=len(live) + 1)
            elif edit.op == "rename":
                # Keyed by name but carrying the field id, which is the whole point of the op:
                # rebuilt in place so a later edit in the same batch finds it under the new name.
                was = live.pop(edit.renamed_from)
                live[col.name] = Column(col.name, was.iceberg_type, was.required, was.field_id)
            elif edit.op == "promote":
                live[col.name] = Column(col.name, col.iceberg_type, live[col.name].required, live[col.name].field_id)
            elif edit.op == "relax":
                live[col.name] = Column(col.name, live[col.name].iceberg_type, False, live[col.name].field_id)
            else:  # pragma: no cover - would be a bug in the executor
                raise AssertionError(f"unexpected op {edit.op}")
        self.tables[table] = live
        self.properties.setdefault(table, {}).update(properties)
        self.log.append(("alter", table, tuple((e.op, e.column.name) for e in edits)))

    def append_rows(self, table, rows):
        self._guard(table)
        self.rows.setdefault(table, []).extend(dict(r) for r in rows)
        self.log.append(("append", table, len(rows)))

    def _guard(self, table: str) -> None:
        if self.fail_on == table:
            raise CatalogError(f"boom: {table}")

    @property
    def writes(self) -> list[tuple]:
        return [entry for entry in self.log if entry[0] != "namespace"]


class _FakeArrow:
    """Just enough of a pyarrow.Table for the meta store, which only ever calls `to_pylist()`."""

    def __init__(self, rows):
        self._rows = rows

    def to_pylist(self):
        return [dict(r) for r in self._rows]


@pytest.fixture(scope="module")
def ontology():
    built, _ = build(VALID)
    return built


@pytest.fixture(scope="module")
def snapshot():
    return snapshot_spec(VALID)


def _plan(ontology, catalog):
    diag = Diagnostics()
    plan = diff_ontology(ontology, {catalog.name: catalog}, diag)
    assert diag.errors == [], [e.message for e in diag.errors]
    return plan


def _matching() -> FakeWritableCatalog:
    return FakeWritableCatalog(tables={"crm.customers": CUSTOMERS, "sales.orders": ORDERS})


# --- creation ------------------------------------------------------------------------------


def test_apply_creates_missing_tables_and_their_namespaces(ontology, snapshot):
    catalog = FakeWritableCatalog(tables={})
    result = apply_plan(_plan(ontology, catalog), {catalog.name: catalog}, snapshot)

    assert result.status == APPLIED
    assert sorted(catalog.tables) == ["_loom_meta.applied", "crm.customers", "sales.orders"]
    assert ("namespace", "crm") in catalog.log and ("namespace", "sales") in catalog.log
    # Column order and nullability survive the round trip through the port.
    assert list(catalog.tables["crm.customers"]) == ["id", "full_name", "tier", "lifetime_value"]
    assert catalog.tables["crm.customers"]["lifetime_value"].required is False
    assert catalog.tables["crm.customers"]["id"].required is True


def test_created_tables_are_stamped_with_the_spec_they_came_from(ontology, snapshot):
    catalog = FakeWritableCatalog(tables={})
    apply_plan(_plan(ontology, catalog), {catalog.name: catalog}, snapshot)

    props = catalog.properties["crm.customers"]
    assert props["loom.managed"] == "true"
    assert props["loom.spec_hash"] == snapshot.content_hash
    assert props["loom.applied_version"] == "1"


# --- alteration ----------------------------------------------------------------------------


def test_every_edit_to_a_table_is_applied_in_a_single_call(ontology, snapshot):
    """One `alter_table` per table, carrying every edit — the port's contract is that this is one
    Iceberg transaction, so two calls would be two commits and a window where half of a column's
    migration had landed. `lifetime_value` here needs both halves: a widening and a loosening."""
    live = dict(CUSTOMERS)
    live["lifetime_value"] = Column("lifetime_value", "int", required=True, field_id=4)
    catalog = FakeWritableCatalog(tables={"crm.customers": live, "sales.orders": ORDERS})

    result = apply_plan(_plan(ontology, catalog), {catalog.name: catalog}, snapshot)

    assert result.status == APPLIED
    alters = [entry for entry in catalog.log if entry[0] == "alter"]
    assert alters == [
        ("alter", "crm.customers", (("promote", "lifetime_value"), ("relax", "lifetime_value"))),
    ]
    assert catalog.tables["crm.customers"]["lifetime_value"].iceberg_type == "double"
    assert catalog.tables["crm.customers"]["lifetime_value"].required is False


def test_a_missing_optional_column_is_added(ontology, snapshot):
    """The only add that is ever safe on a populated table: `ltv` is the one property the fixture
    declares nullable, and a required add is classified breaking precisely because of this."""
    live = {k: v for k, v in CUSTOMERS.items() if k != "lifetime_value"}
    catalog = FakeWritableCatalog(tables={"crm.customers": live, "sales.orders": ORDERS})

    result = apply_plan(_plan(ontology, catalog), {catalog.name: catalog}, snapshot)

    assert result.status == APPLIED
    assert ("alter", "crm.customers", (("add", "lifetime_value"),)) in catalog.log
    assert catalog.tables["crm.customers"]["lifetime_value"].required is False


def test_a_promotion_is_applied_as_a_type_update(ontology, snapshot):
    narrowed = dict(CUSTOMERS)
    narrowed["lifetime_value"] = Column("lifetime_value", "int", required=False, field_id=4)
    catalog = FakeWritableCatalog(tables={"crm.customers": narrowed, "sales.orders": ORDERS})

    apply_plan(_plan(ontology, catalog), {catalog.name: catalog}, snapshot)

    assert ("alter", "crm.customers", (("promote", "lifetime_value"),)) in catalog.log
    assert catalog.tables["crm.customers"]["lifetime_value"].iceberg_type == "double"
    # Promotion is by field id: the id must survive, or existing data files stop matching.
    assert catalog.tables["crm.customers"]["lifetime_value"].field_id == 4


def test_a_rename_reaches_the_port_ahead_of_the_edits_that_depend_on_it(tmp_path, snapshot):
    """The ordering guarantee `alter_table` documents, asserted where it is produced.

    Everything after the rename addresses the column by the name the rename gives it, so an
    implementation whose schema-update API resolves against the pre-transaction schema — pyiceberg's
    does — can only translate them back if it sees the rename first. One call, so one transaction:
    a rename committed apart from the promotion that follows it is a window where the table matches
    neither spec."""
    (tmp_path / "widget.yaml").write_text(
        """
objectType:
  apiName: Widget
  primaryKey: id
  title: id
  backing: { catalog: rest_main, table: demo.widgets }
  properties:
    - { name: id, type: string, column: id, unique: true }
    - { name: score, type: long, column: score, nullable: true, renamedFrom: old_score }
"""
    )
    built, _ = build(tmp_path)
    live = {
        "id": Column("id", "string", required=True, field_id=1),
        "old_score": Column("old_score", "int", required=True, field_id=2),
    }
    catalog = FakeWritableCatalog(tables={"demo.widgets": live})

    result = apply_plan(_plan(built, catalog), {catalog.name: catalog}, snapshot_spec(tmp_path))

    assert result.status == APPLIED, result.error
    assert [entry for entry in catalog.log if entry[0] == "alter"] == [
        ("alter", "demo.widgets", (("rename", "score"), ("promote", "score"), ("relax", "score"))),
    ]
    after = catalog.tables["demo.widgets"]
    assert "old_score" not in after
    # The field id is the whole point: it is what keeps the existing data files readable.
    assert (after["score"].field_id, after["score"].iceberg_type, after["score"].required) == (2, "long", False)


def test_a_rename_loom_cannot_resolve_refuses_the_run_and_writes_nothing(tmp_path, snapshot):
    """Both columns live. Merging them means dropping one, so it goes through the same whole-plan
    refusal as any other breaking change — no `--force`, and the table is left exactly as found."""
    (tmp_path / "widget.yaml").write_text(
        """
objectType:
  apiName: Widget
  primaryKey: id
  title: id
  backing: { catalog: rest_main, table: demo.widgets }
  properties:
    - { name: id, type: string, column: id, unique: true }
    - { name: score, type: double, column: score, nullable: true, renamedFrom: old_score }
"""
    )
    built, _ = build(tmp_path)
    live = {
        "id": Column("id", "string", required=True, field_id=1),
        "old_score": Column("old_score", "double", required=False, field_id=2),
        "score": Column("score", "double", required=False, field_id=3),
    }
    catalog = FakeWritableCatalog(tables={"demo.widgets": dict(live)})

    result = apply_plan(_plan(built, catalog), {catalog.name: catalog}, snapshot_spec(tmp_path))

    assert result.status == REFUSED
    assert catalog.writes == []
    assert catalog.tables["demo.widgets"] == live, "both columns still there, untouched"
    assert not catalog.table_exists(META_TABLE)
    assert "demo.widgets.score: renamed from old_score" in result.error
    assert "cannot merge them" in result.error


# --- refusal -------------------------------------------------------------------------------


def test_a_breaking_change_refuses_the_whole_run(ontology, snapshot):
    """The safe half of a mixed plan is not applied either. A partial apply would leave the lake
    in a state that neither the old spec nor the new one describes."""
    tables = {
        # A safe add, on the table the planner reaches *first*...
        "crm.customers": {k: v for k, v in CUSTOMERS.items() if k != "lifetime_value"},
        # ...and a required column missing from a populated table, which is breaking.
        "sales.orders": {k: v for k, v in ORDERS.items() if k != "created_at"},
    }
    catalog = FakeWritableCatalog(tables=tables)

    result = apply_plan(_plan(ontology, catalog), {catalog.name: catalog}, snapshot)

    assert result.status == REFUSED
    assert catalog.writes == [], "a refused plan must not touch the catalog at all"
    assert "lifetime_value" not in catalog.tables["crm.customers"], "not even the safe half"
    assert not catalog.table_exists(META_TABLE), "a refused run records nothing"
    assert "breaking" in result.error
    assert "sales.orders.created_at" in result.error


def test_a_read_only_catalog_is_refused_before_anything_runs(ontology, snapshot):
    class ReadOnly:
        name = "rest_main"
        tables = {}

        def table_exists(self, table):
            return False

        def describe(self, table):  # pragma: no cover - never reached
            raise CatalogError(table)

        def scan(self, table, columns=None, predicates=(), limit=None):  # pragma: no cover
            raise NotImplementedError

    catalog = ReadOnly()
    result = apply_plan(_plan(ontology, catalog), {"rest_main": catalog}, snapshot)

    assert result.status == REFUSED
    assert "read-only" in result.error


# --- failure partway through ----------------------------------------------------------------


def test_a_failure_stops_the_run_and_records_what_landed(ontology, snapshot):
    catalog = FakeWritableCatalog(tables={}, fail_on="sales.orders")

    result = apply_plan(_plan(ontology, catalog), {catalog.name: catalog}, snapshot)

    assert result.status == FAILED
    assert [o.table for o in result.applied] == ["crm.customers"]
    assert "boom" in result.error
    # The table that did land stays landed — Iceberg commits per table and there is no undo.
    assert "crm.customers" in catalog.tables
    # ...and the history says so, rather than claiming the spec is live.
    record = MetaStore(catalog).latest()
    assert record.status == "partial"
    assert [e["table"] for e in json.loads(record.summary)] == ["rest_main.crm.customers", "rest_main.sales.orders"]
    assert "error" in json.loads(record.summary)[1]


# --- idempotency ---------------------------------------------------------------------------


def test_re_running_the_same_spec_does_nothing(ontology, snapshot):
    catalog = FakeWritableCatalog(tables={})
    first = apply_plan(_plan(ontology, catalog), {catalog.name: catalog}, snapshot)
    writes = len(catalog.writes)

    second = apply_plan(_plan(ontology, catalog), {catalog.name: catalog}, snapshot)

    assert first.status == APPLIED
    assert second.status == UP_TO_DATE
    assert len(catalog.writes) == writes, "a second run must not write anything, not even history"
    assert second.versions == {"rest_main": 1}


def test_an_already_matching_catalog_records_the_spec_once(ontology, snapshot):
    """Nothing to migrate, but nothing recorded either — so the first run still writes history,
    which is what makes the *second* one a no-op."""
    catalog = _matching()
    first = apply_plan(_plan(ontology, catalog), {catalog.name: catalog}, snapshot)
    second = apply_plan(_plan(ontology, catalog), {catalog.name: catalog}, snapshot)

    assert (first.status, second.status) == (APPLIED, UP_TO_DATE)
    assert first.tables == ()
    assert len(catalog.rows[META_TABLE]) == 1


def test_a_spec_edit_that_changes_no_column_still_records_a_version(ontology, tmp_path, snapshot):
    """A comment-only edit migrates nothing — but `_loom_meta` holds the text a rollback restores,
    so a history that skipped it would restore the wrong file."""
    catalog = _matching()
    apply_plan(_plan(ontology, catalog), {catalog.name: catalog}, snapshot)

    edited = snapshot_spec(VALID)
    edited = type(edited)(files={**edited.files, "customer.yaml": edited.files["customer.yaml"] + "\n# note\n"},
                          content_hash="deadbeef")
    result = apply_plan(_plan(ontology, catalog), {catalog.name: catalog}, edited)

    assert result.status == APPLIED
    assert result.tables == (), "no DDL — the physical schema was already right"
    assert result.versions == {"rest_main": 2}
    assert MetaStore(catalog).latest().content_hash == "deadbeef"


# --- more than one catalog ------------------------------------------------------------------


def _two_catalog_ontology(tmp_path: Path):
    """A spec split across two catalogs — one objectType each, one file each (the loader takes a
    single document per file)."""
    for name, api_name, catalog, table in (
        ("customer", "Customer", "warm", "crm.customers"),
        ("event", "Event", "cold", "logs.events"),
    ):
        (tmp_path / f"{name}.yaml").write_text(
            f"""
objectType:
  apiName: {api_name}
  primaryKey: id
  title: id
  backing: {{ catalog: {catalog}, table: {table} }}
  properties:
    - {{ name: id, type: string, column: id, unique: true }}
"""
        )
    built, _ = build(tmp_path)
    return built


def test_both_catalogs_record_the_same_version(tmp_path):
    """The version counts applies of the *spec*. Two lakes, two rows, one number — otherwise
    "version 3" would mean a different thing in each and neither could be quoted."""
    ontology, snapshot = _two_catalog_ontology(tmp_path), snapshot_spec(tmp_path)
    warm, cold = FakeWritableCatalog("warm", {}), FakeWritableCatalog("cold", {})
    catalogs = {"warm": warm, "cold": cold}

    diag = Diagnostics()
    plan = diff_ontology(ontology, catalogs, diag)
    assert diag.errors == []
    result = apply_plan(plan, catalogs, snapshot)

    assert result.status == APPLIED
    assert result.versions == {"warm": 1, "cold": 1}
    # Each row summarizes only its own catalog's tables, but carries the whole spec.
    assert [e["table"] for e in MetaStore(warm).latest().summary_data()] == ["warm.crm.customers"]
    assert [e["table"] for e in MetaStore(cold).latest().summary_data()] == ["cold.logs.events"]
    assert MetaStore(warm).latest().content_hash == MetaStore(cold).latest().content_hash


def test_a_catalog_joining_late_starts_at_the_current_version(tmp_path):
    """Derived counter, not a per-catalog one: a lake added to a project at version 3 records its
    first row as version 3 rather than restarting at 1."""
    ontology, snapshot = _two_catalog_ontology(tmp_path), snapshot_spec(tmp_path)
    warm, cold = FakeWritableCatalog("warm", {}), FakeWritableCatalog("cold", {})
    catalogs = {"warm": warm, "cold": cold}

    diag = Diagnostics()
    # `warm` has been applied to twice already; `cold` has never been seen.
    for _ in range(2):
        MetaStore(warm, warm).record(snapshot, [], version=MetaStore(warm).current_version() + 1)
    result = apply_plan(diff_ontology(ontology, catalogs, diag), catalogs, snapshot)

    assert result.versions == {"warm": 3, "cold": 3}
    assert [r.version for r in MetaStore(cold).history()] == [3]


# --- the meta table ------------------------------------------------------------------------


def test_the_history_accumulates_and_carries_the_spec(ontology, snapshot):
    catalog = FakeWritableCatalog(tables={})
    apply_plan(_plan(ontology, catalog), {catalog.name: catalog}, snapshot, now=datetime(2026, 8, 1, tzinfo=UTC))

    history = MetaStore(catalog).history()
    assert [r.version for r in history] == [1]
    entry = history[0]
    assert entry.status == STATUS_APPLIED
    assert entry.content_hash == snapshot.content_hash
    assert json.loads(entry.spec)["customer.yaml"].startswith("objectType:")
    assert entry.applied_at == datetime(2026, 8, 1, tzinfo=UTC)
    assert {e["action"] for e in entry.summary_data()} == {"create"}


def test_the_meta_table_is_not_itself_a_migration_target(ontology, snapshot):
    """`_loom_meta` is Loom's own bookkeeping, not part of the ontology — it must never show up in
    a plan, or every apply would propose changing the table it is recording itself in."""
    catalog = FakeWritableCatalog(tables={})
    apply_plan(_plan(ontology, catalog), {catalog.name: catalog}, snapshot)

    plan = _plan(ontology, catalog)
    assert plan.is_empty
    assert all(META_TABLE != table for _, table in plan.targets)


# --- rendering -----------------------------------------------------------------------------


def test_render_reports_what_landed_and_where_it_was_recorded(ontology, snapshot):
    catalog = FakeWritableCatalog(tables={})
    out = render_apply(apply_plan(_plan(ontology, catalog), {catalog.name: catalog}, snapshot))

    assert "+ rest_main.crm.customers — created · namespace 'crm' created" in out
    assert "Applied 2 table change(s)." in out
    assert "version 1 in `_loom_meta` (rest_main)" in out


def test_render_of_a_refusal_is_the_reason_itself(ontology, snapshot):
    # optional -> required on a live column: the other shape of breaking change.
    tables = {"crm.customers": {**CUSTOMERS, "tier": Column("tier", "string", required=False, field_id=3)},
              "sales.orders": ORDERS}
    catalog = FakeWritableCatalog(tables=tables)
    result = apply_plan(_plan(ontology, catalog), {catalog.name: catalog}, snapshot)

    out = render_apply(result)
    assert out == result.error
    assert "nothing was applied" in out
