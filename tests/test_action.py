"""The action runtime — against a fake catalog that records what it was asked to do.

Same bargain as `test_apply.py`: the ports mean the whole runtime is testable with no Iceberg
stack, and what's asserted here is the **policy** — carry across the columns nobody declared,
refuse a key that matches twice, evaluate every rule, write nothing on a refusal — because that is
the part a real catalog would only tell us about by corrupting someone's table.
`test_action_iceberg.py` proves the same sequence against real pyiceberg.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from loom.action import (
    AMBIGUOUS_KEY,
    APPLIED,
    CONFLICT,
    EXPRESSION_ERROR,
    FAILED,
    MISSING_PARAMETER,
    OBJECT_EXISTS,
    OBJECT_NOT_FOUND,
    PREVIEWED,
    REFUSED,
    RETRYABLE,
    TYPE_ERROR,
    UNKNOWN_PARAMETER,
    VALIDATION_FAILED,
    ActionError,
    ActionRuntime,
)
from loom.catalog.base import CatalogError, Column, TableSchema, row_writer_for, writer_for
from loom.ontology import build

VALID = Path(__file__).parent / "fixtures" / "valid"

# The physical rows behind the `valid` fixture's Customer — including two columns no property maps.
# `region` has a type Loom knows; `segments` has one it does not (§1 defers `array<T>`). Neither is
# the ontology's business, and a modify must carry both across untouched.
CUSTOMERS = [
    {"id": "c1", "full_name": "Ada Lovelace", "tier": "gold", "lifetime_value": 48210.5,
     "region": "emea", "segments": ["enterprise"]},
    {"id": "c2", "full_name": "Grace Hopper", "tier": "silver", "lifetime_value": None,
     "region": "amer", "segments": None},
]


class FakeRowCatalog:
    """An in-memory catalog implementing the read port and `RowWriter` — and deliberately *not*
    `CatalogWriter`.

    That absence is an assertion in itself: the action runtime is handed this and works, which
    means it never reached for a schema verb. The migration fake in `test_apply.py` is its mirror
    image — schema verbs, no row verbs — and neither can do the other's job."""

    def __init__(self, rows=None, snapshot=1, fail_on=""):
        self.name = "rest_main"
        self.rows: dict[str, list[dict]] = {"crm.customers": [dict(r) for r in (rows or CUSTOMERS)],
                                            "sales.orders": []}
        self.snapshots = {t: snapshot for t in self.rows}
        self.log: list[tuple] = []
        self.fail_on = fail_on

    # --- read port
    def table_exists(self, table: str) -> bool:
        return table in self.rows

    def describe(self, table: str) -> TableSchema:  # pragma: no cover - the runtime never asks
        return TableSchema(table=table, columns={c: Column(c, "string", False) for c in self.rows[table][0]})

    def scan(self, table, columns=None, predicates=(), limit=None):
        self.log.append(("scan", table, tuple(predicates), columns))
        rows = self.rows.get(table, [])
        # Deliberately ignoring `predicates`: the port documents them as a pushdown *hint*, and the
        # runtime is required to filter again for itself. A fake that honored them would hide it.
        return _FakeArrow(rows)

    def current_snapshot_id(self, table: str) -> int | None:
        self.log.append(("snapshot", table))
        return self.snapshots.get(table)

    # --- row write port
    def insert_row(self, table, row):
        self._guard(table)
        self.rows.setdefault(table, []).append(dict(row))
        self._bump(table)
        self.log.append(("insert", table, dict(row)))

    def replace_row(self, table, key_column, key_value, row):
        self._guard(table)
        kept = [r for r in self.rows[table] if r.get(key_column) != key_value]
        self.rows[table] = [*kept, dict(row)]
        self._bump(table)
        self.log.append(("replace", table, key_value, dict(row)))

    def delete_row(self, table, key_column, key_value):
        self._guard(table)
        self.rows[table] = [r for r in self.rows[table] if r.get(key_column) != key_value]
        self._bump(table)
        self.log.append(("delete", table, key_value))

    def _guard(self, table):
        if self.fail_on == table:
            raise CatalogError(f"boom: {table}")

    def _bump(self, table):
        self.snapshots[table] = self.snapshots.get(table, 0) + 1

    @property
    def writes(self):
        return [e for e in self.log if e[0] in ("insert", "replace", "delete")]

    def row(self, table, key_column, key):
        return next((r for r in self.rows[table] if r.get(key_column) == key), None)


class _FakeArrow:
    """Just enough of a pyarrow.Table for the runtime, which only calls `to_pylist()`."""

    def __init__(self, rows):
        self._rows = rows

    def to_pylist(self):
        return [dict(r) for r in self._rows]


@pytest.fixture(scope="module")
def ontology():
    built, _ = build(VALID)
    return built


@pytest.fixture
def catalog():
    return FakeRowCatalog()


@pytest.fixture
def runtime(ontology, catalog):
    return ActionRuntime(ontology=ontology, catalogs={"rest_main": catalog})


def _with_rules(ontology, catalog, *rules: tuple[str, str]) -> ActionRuntime:
    """`upgradeTier` with its validation block swapped out, so a test can state the rule it is
    about instead of adding a fixture action per rule."""
    from dataclasses import replace

    from loom.expr import parse as parse_expr
    from loom.model import Ontology, ValidationRule

    action = replace(
        ontology.actions["upgradeTier"],
        validation=tuple(ValidationRule(parse_expr(src), message, src) for src, message in rules),
    )
    return ActionRuntime(
        ontology=Ontology(ontology.object_types, ontology.link_types, {"upgradeTier": action}),
        catalogs={"rest_main": catalog},
    )


# ---- modify: the operation that exercises the whole path ------------------------


def test_modify_writes_one_row_as_one_call(runtime, catalog):
    result = runtime.run("upgradeTier", {"customer": "c2", "newTier": "gold"})

    assert result.status == APPLIED, result.failures
    assert result.before["tier"] == "silver" and result.after["tier"] == "gold"
    # One write, and it is the replace verb — not a delete followed by an insert, which would be
    # two commits and a window where the row does not exist.
    assert [e[0] for e in catalog.writes] == ["replace"]
    assert catalog.row("crm.customers", "id", "c2")["tier"] == "gold"


def test_a_modify_carries_across_the_columns_nobody_declared(runtime, catalog):
    """The row-level version of "an objectType maps a subset of a table's columns".

    A modify rewrites the whole row, so a column the ontology never mentions is either carried or
    silently nulled — and `segments` is one whose *type* Loom has no name for, so it cannot even be
    inspected on the way through. Both come out the other side identical."""
    runtime.run("upgradeTier", {"customer": "c1", "newTier": "silver"})

    written = catalog.row("crm.customers", "id", "c1")
    assert written["region"] == "emea"
    assert written["segments"] == ["enterprise"]
    assert written["full_name"] == "Ada Lovelace"  # a mapped column the effect didn't set
    assert written["tier"] == "silver"


def test_the_result_reports_the_ontology_not_the_table(runtime):
    """`before`/`after` are property-named and carry only declared properties. The unmapped columns
    travel across the write but are not the ontology's to show — reporting them would leak somebody
    else's data past a governance layer that does not exist yet."""
    result = runtime.run("upgradeTier", {"customer": "c1", "newTier": "silver"})

    assert set(result.before) == {"customerId", "name", "tier", "ltv"}
    assert "region" not in result.before and "segments" not in result.after


def test_the_read_is_not_pruned_because_the_carry_across_needs_the_whole_row(runtime, catalog):
    runtime.run("upgradeTier", {"customer": "c1", "newTier": "silver"})

    scans = [e for e in catalog.log if e[0] == "scan"]
    assert scans and all(columns is None for *_, columns in scans)


# ---- create and delete ---------------------------------------------------------


def test_create_inserts_and_needs_no_prior_object(runtime, catalog):
    result = runtime.run("createOrder", {"orderId": "o9", "customerId": "c1", "total": "42.50"})

    assert result.status == APPLIED, result.failures
    assert result.before is None
    # Coerced to the declared types on the way out, by the same function the read path coerces with:
    # a decimal is a Decimal (never a float) and `now()` is a real timestamp.
    written = catalog.row("sales.orders", "id", "o9")
    assert written["total_amount"] == Decimal("42.50")
    assert isinstance(written["created_at"], datetime)


def test_create_refuses_a_primary_key_that_already_exists(runtime, catalog):
    catalog.rows["sales.orders"].append({"id": "o9", "customer_id": "c1"})

    result = runtime.run("createOrder", {"orderId": "o9", "customerId": "c1", "total": "1.00"})

    assert result.status == REFUSED
    assert [f.code for f in result.failures] == [OBJECT_EXISTS]
    assert catalog.writes == []


def test_delete_removes_one_row(ontology, catalog):
    """`operation: delete` does not contradict "Loom never drops". Never-drop is Loom refusing to
    infer a destruction from *silence* in a spec; this is a declared action naming a key. And the
    scopes differ — never-drop is about a column or a table, and neither is touched here."""
    runtime = ActionRuntime(ontology=ontology, catalogs={"rest_main": catalog})

    result = runtime.run("forgetCustomer", {"customer": "c2"})

    assert result.status == APPLIED, result.failures
    assert result.before["name"] == "Grace Hopper" and result.after is None
    assert [r["id"] for r in catalog.rows["crm.customers"]] == ["c1"]


# ---- refusals change nothing ---------------------------------------------------


def test_a_failed_rule_is_a_typed_result_carrying_the_authors_own_message(runtime, catalog):
    result = runtime.run("upgradeTier", {"customer": "c1", "newTier": "gold"})  # c1 is already gold

    assert result.status == REFUSED
    (failure,) = result.failures
    assert failure.code == VALIDATION_FAILED
    assert failure.message == "New tier must differ from current tier"  # verbatim from the YAML
    assert failure.detail["rule"] == "newTier != object.tier"
    assert failure.retryable is False
    assert catalog.writes == []


def test_every_rule_is_evaluated_not_just_the_first_to_fail(ontology, catalog):
    """The same accumulate-everything bargain the spec validator makes. An agent fixing one
    precondition per call is as miserable as an author fixing one typo per run."""
    runtime = _with_rules(ontology, catalog, ("newTier != object.tier", "tiers must differ"),
                          ("object.ltv != null", "needs a lifetime value"))

    result = runtime.run("upgradeTier", {"customer": "c2", "newTier": "silver"})  # c2 is silver, ltv null

    assert [f.message for f in result.failures] == ["tiers must differ", "needs a lifetime value"]
    assert catalog.writes == []


def test_a_missing_object_is_reported_without_a_second_complaint_about_it(runtime, catalog):
    """`object.tier` genuinely cannot be evaluated when there is no object, but saying so after
    "no Customer with customerId 'c9'" is noise, not information."""
    result = runtime.run("upgradeTier", {"customer": "c9", "newTier": "gold"})

    assert result.status == REFUSED
    assert [f.code for f in result.failures] == [OBJECT_NOT_FOUND]
    assert catalog.writes == []


def test_a_key_matching_two_rows_is_refused_before_the_write(ontology):
    """An equality-delete on a key with two matching rows deletes both and appends one. The read
    path already refuses this (`Resolver.get`); refusing it here matters more, because here the
    consequence is losing a row nobody asked about. Loom cannot repair the table — it can only
    decline to make it worse."""
    catalog = FakeRowCatalog(rows=[*CUSTOMERS, {"id": "c1", "full_name": "A Duplicate",
                                                "tier": "bronze", "lifetime_value": None}])
    runtime = ActionRuntime(ontology=ontology, catalogs={"rest_main": catalog})

    result = runtime.run("upgradeTier", {"customer": "c1", "newTier": "silver"})

    assert result.status == REFUSED
    (failure,) = result.failures
    assert failure.code == AMBIGUOUS_KEY and failure.detail["matched"] == 2
    assert "violates the uniqueness the spec declares" in failure.message
    assert catalog.writes == []


def test_a_write_that_fails_is_reported_as_failed_not_refused(ontology):
    """Different words for different things: `refused` means nothing ran, `failed` means the write
    was attempted. A caller retrying blindly needs to be able to tell them apart."""
    catalog = FakeRowCatalog(fail_on="crm.customers")
    runtime = ActionRuntime(ontology=ontology, catalogs={"rest_main": catalog})

    result = runtime.run("upgradeTier", {"customer": "c1", "newTier": "silver"})

    assert result.status == FAILED and not result.ok
    assert "boom" in result.failures[0].message


# ---- parameter binding ---------------------------------------------------------


def test_parameters_are_coerced_by_the_same_function_the_read_path_uses(ontology, catalog):
    """An LLM sends `"42.50"` for a decimal and `"c1"` for an objectRef. Both are coerced once,
    where the declared type is known — the read path's problem, in the other direction."""
    runtime = ActionRuntime(ontology=ontology, catalogs={"rest_main": catalog})

    runtime.run("createOrder", {"orderId": "o1", "customerId": "c1", "total": "42.50"})

    assert catalog.row("sales.orders", "id", "o1")["total_amount"] == Decimal("42.50")


def test_a_value_that_cannot_be_read_as_its_declared_type_is_refused_not_rounded(runtime, catalog):
    result = runtime.run("createOrder", {"orderId": "o1", "customerId": "c1", "total": "42.505"})

    assert result.status == REFUSED
    assert [f.code for f in result.failures] == [TYPE_ERROR]
    assert "decimal(12,2)" in result.failures[0].message
    assert catalog.writes == []


def test_an_enum_parameter_outside_its_values_is_refused(runtime, catalog):
    result = runtime.run("upgradeTier", {"customer": "c1", "newTier": "platinum"})

    assert [f.code for f in result.failures] == [TYPE_ERROR]
    assert "is not one of: silver, gold" in result.failures[0].message


def test_a_missing_required_parameter_is_named(runtime):
    result = runtime.run("upgradeTier", {"customer": "c1"})

    assert [f.code for f in result.failures] == [MISSING_PARAMETER]
    assert result.failures[0].detail["parameter"] == "newTier"


def test_an_undeclared_parameter_is_rejected_rather_than_ignored(runtime):
    """A spec language that silently drops fields rots — the same rule the loader applies to
    unknown YAML keys, applied to a tool call."""
    result = runtime.run("upgradeTier", {"customer": "c1", "newTier": "silver", "reason": "vip"})

    assert [f.code for f in result.failures] == [UNKNOWN_PARAMETER]


def test_an_unknown_action_is_an_exception_not_a_result(runtime):
    """A `Failure` is an action that ran and refused. Asking for one that doesn't exist is a
    caller bug, and there is no result to attach it to."""
    with pytest.raises(ActionError) as e:
        runtime.run("noSuchAction", {})
    assert "createOrder" in str(e.value)


# ---- dry run -------------------------------------------------------------------


def test_a_dry_run_shows_the_whole_outcome_and_writes_nothing(runtime, catalog):
    result = runtime.preview("upgradeTier", {"customer": "c2", "newTier": "gold"})

    assert result.status == PREVIEWED and result.ok
    assert result.before["tier"] == "silver" and result.after["tier"] == "gold"
    assert catalog.writes == []
    assert catalog.row("crm.customers", "id", "c2")["tier"] == "silver"


# ---- the concurrency seam ------------------------------------------------------


def test_the_snapshot_the_read_saw_is_recorded_and_labelled_as_unenforced(runtime, catalog):
    """Recorded, not checked. The write is one Iceberg transaction; the read before it is not, and
    a result that implied otherwise would be the dishonest part of this slice."""
    result = runtime.run("upgradeTier", {"customer": "c2", "newTier": "gold"})

    assert result.read_snapshot_id == 1
    assert result.as_json()["concurrency"] == "recorded, not enforced"


def test_the_snapshot_is_read_before_the_rows(runtime, catalog):
    """The order is load-bearing. Snapshot-then-scan records an id at-or-before the data, so a
    later check can raise a conflict that wasn't one but can never miss one that was. Scan-then-
    snapshot silently blesses a lost update."""
    runtime.run("upgradeTier", {"customer": "c2", "newTier": "gold"})

    reads = [e[0] for e in catalog.log if e[0] in ("snapshot", "scan")]
    assert reads.index("snapshot") < reads.index("scan")


def test_conflict_exists_as_a_code_before_anything_raises_it():
    """M4 wants typed results an agent can act on, so the shape has to exist before it can wrap it.
    The next slice adds a check and one `Failure` — not a new result shape every caller written
    against this one would have to relearn."""
    assert CONFLICT in RETRYABLE
    assert all(code not in RETRYABLE for code in (VALIDATION_FAILED, OBJECT_NOT_FOUND, TYPE_ERROR))


# ---- the ports -----------------------------------------------------------------


def test_the_action_runtime_never_needs_a_schema_writer(catalog):
    """`FakeRowCatalog` implements no `CatalogWriter`, and every test above passed against it. The
    runtime cannot alter a table because the port it asks for has no verb for one."""
    assert row_writer_for(catalog) is catalog
    with pytest.raises(CatalogError) as e:
        writer_for(catalog)
    assert "schema writes" in str(e.value) and "'rest_main'" in str(e.value)


def test_apply_cannot_delete_a_row_because_its_port_has_no_verb_for_one():
    """The mirror image, and the other half of the argument: the schema writer `loom apply` holds
    has `append_rows` (that is how `_loom_meta` records history, and it destroys nothing) and no
    way at all to remove or replace one."""
    from test_apply import FakeWritableCatalog

    catalog = FakeWritableCatalog()
    assert writer_for(catalog) is catalog
    with pytest.raises(CatalogError) as e:
        row_writer_for(catalog)
    assert "row writes" in str(e.value)


def test_a_catalog_that_is_not_bound_is_named(ontology):
    runtime = ActionRuntime(ontology=ontology, catalogs={})
    with pytest.raises(ActionError) as e:
        runtime.run("upgradeTier", {"customer": "c1", "newTier": "silver"})
    assert "rest_main" in str(e.value)


# ---- the effect language -------------------------------------------------------


def test_an_effect_value_may_be_an_expression_not_only_a_parameter(runtime, catalog):
    """`placedAt: "now()"` — the effect block and the validation block are the same grammar, and
    narrowing effects to bare references would make the most obvious thing a create wants
    inexpressible."""
    runtime.run("createOrder", {"orderId": "o7", "customerId": "c1", "total": "1.00"})

    assert isinstance(catalog.row("sales.orders", "id", "o7")["created_at"], datetime)


def test_an_expression_that_cannot_be_evaluated_is_its_own_code(ontology, catalog):
    """Distinct from `validation_failed`: the rule did not come back false, it came back not at
    all, and an agent that treated the two the same would retry the wrong thing."""
    runtime = _with_rules(ontology, catalog, ("object.ltv > 100", "needs a big ltv"))

    result = runtime.run("upgradeTier", {"customer": "c2", "newTier": "gold"})  # c2's ltv is null

    assert [f.code for f in result.failures] == [EXPRESSION_ERROR]
    assert catalog.writes == []
