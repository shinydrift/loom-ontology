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
    MAX_ATTEMPTS,
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
from loom.catalog.base import (
    EDIT_LOG_TABLE,
    CatalogError,
    Column,
    ConcurrencyError,
    TableSchema,
    row_writer_for,
    writer_for,
)
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
    """An in-memory catalog implementing the read port, `RowWriter` and `EditLogWriter` — and
    deliberately *not* `CatalogWriter`.

    That absence is an assertion in itself: the action runtime is handed this and works, which
    means it never reached for a schema verb. The migration fake in `test_apply.py` is its mirror
    image — schema verbs, no row verbs — and neither can do the other's job.

    The edit-log port keeps that true through the slice that gave the runtime something to record.
    `append_edit` creates its table and appends to it, so the runtime provably does not need
    `create_table` to log — which is the whole argument for a fourth port, and the one thing only a
    fake can demonstrate. A real catalog implements every port at once and so can never show which
    one was used."""

    def __init__(self, rows=None, snapshot=1, fail_on="", log_fails=False, log_create_fails=False):
        self.name = "rest_main"
        self.rows: dict[str, list[dict]] = {"crm.customers": [dict(r) for r in (rows or CUSTOMERS)],
                                            "sales.orders": []}
        self.snapshots = {t: snapshot for t in self.rows}
        self.log: list[tuple] = []
        self.fail_on = fail_on
        self.log_fails = log_fails
        self.log_create_fails = log_create_fails
        self.edit_columns = None
        self.commits: dict[tuple, dict] = {}

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
    def insert_row(self, table, row, *, expect_snapshot_id, commit_properties):
        self._guard(table, expect_snapshot_id)
        self.rows.setdefault(table, []).append(dict(row))
        self._bump(table, commit_properties)
        self.log.append(("insert", table, dict(row)))

    def replace_row(self, table, key_column, key_value, row, *, expect_snapshot_id, commit_properties):
        self._guard(table, expect_snapshot_id)
        kept = [r for r in self.rows[table] if r.get(key_column) != key_value]
        self.rows[table] = [*kept, dict(row)]
        self._bump(table, commit_properties)
        self.log.append(("replace", table, key_value, dict(row)))

    def delete_row(self, table, key_column, key_value, *, expect_snapshot_id, commit_properties):
        self._guard(table, expect_snapshot_id)
        self.rows[table] = [r for r in self.rows[table] if r.get(key_column) != key_value]
        self._bump(table, commit_properties)
        self.log.append(("delete", table, key_value))

    # --- edit-log port
    def ensure_log(self, columns):
        """The create half, on its own. `governance.edit_log: required` calls it at build time, so a
        fake that only appended could not show that a deployment proves its log before it writes.

        Failing independently of `log_fails` on purpose: a log that can be *created* and then fails
        to accept a record is exactly the window `require_edit_log` says it does not close, and a
        fake whose two halves failed together could not exhibit it."""
        if self.log_create_fails:
            raise CatalogError("boom: the edit log cannot be created")
        self.edit_columns = tuple(columns)
        self.rows.setdefault(EDIT_LOG_TABLE, [])

    def append_edit(self, columns, row):
        """One append, to the one table this port can name. No snapshot argument, because there is
        nothing to assert: the caller read nothing and is putting no row over another."""
        if self.log_fails:
            raise CatalogError("boom: the edit log is unreachable")
        self.edit_columns = tuple(columns)
        self.rows.setdefault(EDIT_LOG_TABLE, []).append(dict(row))
        self.snapshots.setdefault(EDIT_LOG_TABLE, 0)
        self.snapshots[EDIT_LOG_TABLE] += 1

    def _guard(self, table, expect_snapshot_id):
        """The compare-and-swap, which in one process and one thread is exactly what it says.

        A real catalog cannot do it this way — it hands the assertion to the metastore to validate
        as the commit lands, because between the compare and the swap there is a network. Here there
        is nothing between them, so the port's promise ("the check is atomic with the write") is
        kept by the same statement that makes it."""
        if self.fail_on == table:
            raise CatalogError(f"boom: {table}")
        current = self.snapshots.get(table)
        if expect_snapshot_id != current:
            raise ConcurrencyError(
                f"'{table}' moved: expected {expect_snapshot_id}, found {current}",
                table=table,
                expected=expect_snapshot_id,
                found=current,
            )

    def _bump(self, table, commit_properties=None):
        self.snapshots[table] = self.snapshots.get(table, 0) + 1
        # What a real catalog puts in the snapshot summary. Keyed by the snapshot it belongs to,
        # because the point of the stamp is that it is inseparable from *that* commit.
        self.commits[(table, self.snapshots[table])] = dict(commit_properties or {})

    @property
    def writes(self):
        return [e for e in self.log if e[0] in ("insert", "replace", "delete")]

    @property
    def edits(self):
        """The edit log's rows, oldest first — appended in order, so insertion order is that."""
        return [dict(r) for r in self.rows.get(EDIT_LOG_TABLE, [])]

    def row(self, table, key_column, key):
        return next((r for r in self.rows[table] if r.get(key_column) == key), None)


class _FakeArrow:
    """Just enough of a pyarrow.Table for the runtime, which only calls `to_pylist()`."""

    def __init__(self, rows):
        self._rows = rows

    def to_pylist(self):
        return [dict(r) for r in self._rows]


class Interloper:
    """A catalog that commits somebody else's write in the gap this slice is about.

    **This is the seam, and it is the port itself.** There is no hook in the runtime, no injection
    point, no `_for_testing` flag — a hook nothing in production calls is a hook that drifts out of
    step with the path that matters, and a race that only reproduces under load is a path nobody
    knows works. Instead: reads and writes go through a narrow port, so a test can wrap a catalog
    and have its `scan` commit a competing write *before returning the rows*. The interleaving is
    driven by the runtime's own call sequence rather than by a scheduler, so "another writer got
    there first" is as deterministic as any other assertion in this file, and two threads and hope
    never enter into it.

    `test_action_iceberg.py` wraps the *real* catalog in this same shape, where the competing write
    is a genuine second commit through an independently opened handle. Same seam, same determinism,
    real Iceberg.

    `strike_on` names the attempts to interfere with (1-based), so one test can prove the retry
    recovers and another can prove it gives up. `write` is handed the attempt number, because a
    competing writer that put the same value back every time would be indistinguishable from no
    writer at all in the `changed` diff.

    Every verb is spelled out rather than proxied through `__getattr__`, because a runtime-checkable
    Protocol is tested with `inspect.getattr_static`, which does not consult `__getattr__` — a
    dynamic proxy is not a port implementation as far as `row_writer_for` is concerned, and it is
    right that it isn't."""

    def __init__(self, inner, strike_on=(1,), write=None):
        self.name = inner.name
        self.inner = inner
        self.strike_on = set(strike_on)
        self.write = write or (lambda cat, n: cat.replace_row(
            "crm.customers", "id", "c1",
            {**cat.row("crm.customers", "id", "c1"), "region": f"apac-{n}"},
            expect_snapshot_id=cat.snapshots["crm.customers"], commit_properties={},
        ))
        self.attempts = 0
        self._armed = False

    def current_snapshot_id(self, table):
        # Recording a snapshot is how an attempt announces itself: the runtime reads it, then reads
        # the row, then writes. Arming here rather than counting scans keeps the interference on the
        # read whose rules are about to be evaluated, and off the re-read `_conflict` does
        # afterwards to work out what moved — which is a diagnosis, not an attempt.
        self._armed = True
        return self.inner.current_snapshot_id(table)

    def scan(self, table, columns=None, predicates=(), limit=None):
        rows = self.inner.scan(table, columns, predicates, limit)
        if self._armed:
            self._armed = False
            self.attempts += 1
            if self.attempts in self.strike_on:
                # Between the runtime's read and the runtime's write, and *after* the snapshot it
                # recorded. Exactly the window the check exists to close.
                self.write(self.inner, self.attempts)
        return rows

    # --- everything else, straight through
    def table_exists(self, table):
        return self.inner.table_exists(table)

    def describe(self, table):  # pragma: no cover - the runtime never asks
        return self.inner.describe(table)

    def insert_row(self, table, row, *, expect_snapshot_id, commit_properties):
        self.inner.insert_row(
            table, row, expect_snapshot_id=expect_snapshot_id, commit_properties=commit_properties
        )

    def replace_row(self, table, key_column, key_value, row, *, expect_snapshot_id, commit_properties):
        self.inner.replace_row(
            table, key_column, key_value, row,
            expect_snapshot_id=expect_snapshot_id, commit_properties=commit_properties,
        )

    def delete_row(self, table, key_column, key_value, *, expect_snapshot_id, commit_properties):
        self.inner.delete_row(
            table, key_column, key_value,
            expect_snapshot_id=expect_snapshot_id, commit_properties=commit_properties,
        )

    def append_edit(self, columns, row):
        self.inner.append_edit(columns, row)

    def ensure_log(self, columns):
        self.inner.ensure_log(columns)


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
    else's data past the governance layer below. (What that layer withholds on top of this is
    `test_governance.py`'s: this runtime binds no policy, so every declared property is here.)"""
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


# ---- optimistic concurrency ----------------------------------------------------


def test_the_snapshot_the_read_saw_is_what_the_write_asserts(runtime, catalog):
    """The whole slice in one assertion: the id the read recorded is the id handed to the write.

    Not a re-read and a comparison inside the runtime — that has a window between the comparison and
    the commit, which narrows the race rather than closing it. The runtime never compares anything;
    it states what it read and the write is declined if that is no longer true."""
    result = runtime.run("upgradeTier", {"customer": "c2", "newTier": "gold"})

    assert result.read_snapshot_id == 1
    assert result.as_json()["concurrency"] == "enforced — the write asserts the snapshot the read saw"
    assert result.status == APPLIED and result.attempts == 1


def test_a_preview_says_it_holds_nothing(runtime):
    """A preview writes nothing, so there is nothing for a check to have been carried into. Saying
    "enforced" beside a snapshot id would read as a claim that the row is being held while somebody
    decides — the exact misreading that would make a slow answer at the `loom run` prompt unsafe."""
    result = runtime.preview("upgradeTier", {"customer": "c2", "newTier": "gold"})

    assert result.as_json()["concurrency"] == "not checked — a preview writes nothing, and holds nothing"


def test_the_snapshot_is_read_before_the_rows(runtime, catalog):
    """The order is load-bearing, and it is why false conflicts exist at all. Snapshot-then-scan
    records an id at-or-before the data, so the check raises conflicts that weren't ones but can
    never miss one that was. Scan-then-snapshot silently blesses a lost update."""
    runtime.run("upgradeTier", {"customer": "c2", "newTier": "gold"})

    reads = [e[0] for e in catalog.log if e[0] in ("snapshot", "scan")]
    assert reads.index("snapshot") < reads.index("scan")


def test_conflict_is_the_only_retryable_code():
    assert CONFLICT in RETRYABLE
    assert all(code not in RETRYABLE for code in (VALIDATION_FAILED, OBJECT_NOT_FOUND, TYPE_ERROR))


def test_a_write_that_lands_in_the_gap_is_retried_and_the_run_still_applies(ontology):
    """One competing commit between the read and the write. The runtime loses the race, reads again,
    re-evaluates every rule against the row that is actually there, and writes.

    The competing write here touches `region` — a column no property maps. It still conflicts, and
    that is the point: a modify rewrites the whole row from what it read, so committing anyway would
    put the old `region` back over somebody else's newer one. Loom refuses to look at that column
    and refuses to overwrite it blind, and the snapshot check is how it does the second without
    doing the first."""
    inner = FakeRowCatalog()
    catalog = Interloper(inner, strike_on=(1,))
    runtime = ActionRuntime(ontology=ontology, catalogs={"rest_main": catalog})

    result = runtime.run("upgradeTier", {"customer": "c2", "newTier": "gold"})

    assert result.status == APPLIED, result.failures
    assert result.attempts == 2  # said out loud: "applied" after two reads is a different fact
    assert inner.row("crm.customers", "id", "c2")["tier"] == "gold"
    # The interloper's write survived — it was not rolled back and not overwritten.
    assert inner.row("crm.customers", "id", "c1")["region"] == "apac-1"


def test_the_losing_attempt_writes_nothing(ontology):
    """A run that conflicts refuses, on the same definition every other refusal uses: the write was
    declined before it committed, not undone afterwards."""
    inner = FakeRowCatalog()
    catalog = Interloper(inner, strike_on=(1,))
    runtime = ActionRuntime(ontology=ontology, catalogs={"rest_main": catalog})

    runtime.run("upgradeTier", {"customer": "c2", "newTier": "gold"})

    # The interloper's replace, then exactly one of ours. The lost attempt left no trace at all.
    assert [e[0] for e in inner.writes] == ["replace", "replace"]


def test_a_permanently_contended_row_comes_back_as_a_retryable_conflict(ontology):
    """Every attempt loses. The bound is about liveness, not correctness — spinning forever against
    a busy table is a livelock dressed up as a slow success — so the caller gets the conflict back
    with enough to decide whether retrying is even the right move."""
    inner = FakeRowCatalog()
    catalog = Interloper(inner, strike_on=range(1, MAX_ATTEMPTS + 1))
    runtime = ActionRuntime(ontology=ontology, catalogs={"rest_main": catalog})

    result = runtime.run("upgradeTier", {"customer": "c2", "newTier": "gold"})

    assert result.status == REFUSED and result.retryable
    assert result.attempts == MAX_ATTEMPTS
    assert [f.code for f in result.failures] == [CONFLICT]
    assert inner.row("crm.customers", "id", "c2")["tier"] == "silver"  # nothing was written
    assert result.as_json()["failures"][0]["retryable"] is True


def test_the_conflict_says_a_busy_table_is_not_a_contested_row(ontology):
    """The failure an agent actually has to act on. Told only "conflict, retry", it will hammer a
    table that is merely busy and give up just as readily when its intent has really been overtaken.
    Here nothing this action reads or writes moved — a different customer's row did — so the detail
    says so."""
    inner = FakeRowCatalog()
    catalog = Interloper(inner, strike_on=range(1, MAX_ATTEMPTS + 1))
    runtime = ActionRuntime(ontology=ontology, catalogs={"rest_main": catalog})

    result = runtime.run("upgradeTier", {"customer": "c2", "newTier": "gold"})

    detail = result.failures[0].detail
    assert detail["changed"] == [] and detail["contended"] is False
    assert detail["attempts"] == MAX_ATTEMPTS and detail["table"] == "crm.customers"
    assert detail["expectedSnapshotId"] != detail["foundSnapshotId"]
    assert "the table is simply busy" in result.failures[0].message


def test_the_conflict_names_the_property_that_moved_when_it_is_one_this_action_is_about(ontology):
    """The other half. `upgradeTier` reads `object.tier` in its rule and writes it in its effect, so
    a competing write to `tier` is not noise — it is the caller's intent being overtaken, and an
    agent can decide from `contended` alone whether its reason for calling still holds."""
    inner = FakeRowCatalog()
    catalog = Interloper(
        inner,
        strike_on=range(1, MAX_ATTEMPTS + 1),
        write=lambda cat, n: cat.replace_row(
            "crm.customers", "id", "c2",
            {**cat.row("crm.customers", "id", "c2"), "tier": ["bronze", "silver"][n % 2]},
            expect_snapshot_id=cat.snapshots["crm.customers"], commit_properties={},
        ),
    )
    runtime = ActionRuntime(ontology=ontology, catalogs={"rest_main": catalog})

    result = runtime.run("upgradeTier", {"customer": "c2", "newTier": "gold"})

    detail = result.failures[0].detail
    assert detail["changed"] == ["tier"] and detail["contended"] is True
    assert "tier changed under it" in result.failures[0].message


def test_the_conflict_diff_never_compares_a_column_no_property_maps(ontology):
    """`changed` is diffed through the same projection `before` and `after` use. `region` moved and
    is not named — the never-inspect rule holds on the diagnosis path too, which is the one place it
    would have been easy to leak past."""
    inner = FakeRowCatalog()
    catalog = Interloper(
        inner,
        strike_on=range(1, MAX_ATTEMPTS + 1),
        write=lambda cat, n: cat.replace_row(
            "crm.customers", "id", "c2",
            {**cat.row("crm.customers", "id", "c2"), "region": f"apac-{n}", "full_name": f"G. Hopper {n}"},
            expect_snapshot_id=cat.snapshots["crm.customers"], commit_properties={},
        ),
    )
    runtime = ActionRuntime(ontology=ontology, catalogs={"rest_main": catalog})

    result = runtime.run("upgradeTier", {"customer": "c2", "newTier": "gold"})

    # `name` is declared and is named; `region` is not declared and is not.
    assert result.failures[0].detail["changed"] == ["name"]
    assert result.failures[0].detail["contended"] is False


def test_a_retry_re_evaluates_the_rules_against_the_row_it_is_about_to_write_over(ontology, catalog):
    """The sharp end of retrying rather than returning. The competing write makes the action's own
    rule false, and the retry does not paper over it: the run comes back `validation_failed`, the
    real reason, instead of a conflict inviting an agent to retry something that can never succeed.

    This is what makes "a retry can succeed against a row the caller never saw" defensible — the
    spec's rules *are* the caller's statement of which states it will act on, and they are checked
    against the newer row, which is stricter than the caller's own stale read could be."""
    inner = FakeRowCatalog()
    interloper = Interloper(
        inner,
        strike_on=(1,),
        write=lambda cat, n: cat.replace_row(
            "crm.customers", "id", "c2", {**cat.row("crm.customers", "id", "c2"), "tier": "gold"},
            expect_snapshot_id=cat.snapshots["crm.customers"], commit_properties={},
        ),
    )
    runtime = _with_rules(ontology, interloper, ("newTier != object.tier", "Already on that tier"))

    result = runtime.run("upgradeTier", {"customer": "c2", "newTier": "gold"})

    assert [f.code for f in result.failures] == [VALIDATION_FAILED]
    assert not result.retryable  # nothing here gets better by trying again


def test_a_delete_whose_row_was_deleted_under_it_says_so_rather_than_claiming_the_work(ontology):
    """The case that argues `delete` should skip the check — the row is gone either way, so refusing
    is refusing something that already happened. It gets its outcome, but honestly coded: the retry
    re-reads, finds nothing, and returns `object_not_found`. A delete that reported `applied` would
    be claiming work it did not do."""
    inner = FakeRowCatalog()
    catalog = Interloper(
        inner,
        strike_on=(1,),
        write=lambda cat, n: cat.delete_row(
            "crm.customers", "id", "c2", expect_snapshot_id=cat.snapshots["crm.customers"],
            commit_properties={},
        ),
    )
    runtime = ActionRuntime(ontology=ontology, catalogs={"rest_main": catalog})

    result = runtime.run("forgetCustomer", {"customer": "c2"})

    assert [f.code for f in result.failures] == [OBJECT_NOT_FOUND]
    assert result.status == REFUSED


def test_two_creates_on_one_key_cannot_both_append(ontology):
    """The reason `create` is checked. Its read is the primary-key existence check, and two
    concurrent creates both pass it — then both append, manufacturing exactly the duplicate row
    `_read` refuses as `ambiguous_key` every time it meets one afterwards, and which Loom can never
    repair. Checked, the loser re-reads, finds the row, and refuses cleanly."""
    inner = FakeRowCatalog()
    catalog = Interloper(
        inner,
        strike_on=(1,),
        write=lambda cat, n: cat.insert_row(
            "sales.orders",
            {"id": "o7", "customer_id": "c1", "total": Decimal("1.00"), "created_at": datetime.now()},
            expect_snapshot_id=cat.snapshots["sales.orders"], commit_properties={},
        ),
    )
    runtime = ActionRuntime(ontology=ontology, catalogs={"rest_main": catalog})

    result = runtime.run("createOrder", {"orderId": "o7", "customerId": "c1", "total": "1.00"})

    assert [f.code for f in result.failures] == [OBJECT_EXISTS]
    assert len([r for r in inner.rows["sales.orders"] if r["id"] == "o7"]) == 1


def test_every_row_verb_is_checked_and_none_of_them_can_be_called_without_an_expectation(catalog):
    """Required, not optional. An argument that can be omitted is a check that can be skipped by
    forgetting — the sibling of the rule that kept it off the port until something passed it. There
    is no value meaning "don't check": `None` is a real expectation, "I read a table with no
    snapshots yet"."""
    import inspect

    for verb in ("insert_row", "replace_row", "delete_row"):
        param = inspect.signature(getattr(catalog, verb)).parameters["expect_snapshot_id"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is inspect.Parameter.empty, f"{verb} lets the check be skipped"


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
