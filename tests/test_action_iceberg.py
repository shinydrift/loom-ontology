"""The action runtime against a real Iceberg catalog — M3's first definition of done.

The fake catalog in `test_action.py` proves the *policy*. This proves the port: that an
equality-delete plus an append really does land as one Iceberg commit, that a column the ontology
never mapped survives the rewrite byte for byte, and that a column whose *type* Loom has no name
for survives it too — because the conversion is driven by the table's own schema rather than by
anything the ontology knows.

It runs the shipped example, seeded, so a broken `examples/retail` fails CI instead of rotting.
"""

from __future__ import annotations

import importlib.util
import shutil
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from loom import build
from loom.action import (
    APPLIED,
    CONFLICT,
    MAX_ATTEMPTS,
    PREVIEWED,
    REFUSED,
    VALIDATION_FAILED,
    ActionRuntime,
)
from loom.config import find_config, load_config
from loom.errors import Diagnostics

pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "retail"

# The two columns `examples/retail/ontology` never mentions. `region` has a type Loom knows and
# `segments` has one it does not (spec §1 defers `array<T>`) — the point being that the runtime
# treats them identically, because it never looks at either.
UNMAPPED = {"region", "segments"}


@pytest.fixture
def seeded(tmp_path):
    """A seeded copy of the example — rows and all, unlike `conftest.project`, because an action
    needs something to act on."""
    target = tmp_path / "retail"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns(".warehouse"))
    spec = importlib.util.spec_from_file_location("action_seed", target / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.seed(target)

    diag = Diagnostics()
    config = load_config(find_config(target / "ontology"), diag)
    ontology, _ = build(target / "ontology")
    diag.raise_if_errors()
    return target, ontology, config


@pytest.fixture
def runtime(seeded):
    from loom.catalog import open_catalogs

    _, ontology, config = seeded
    return ActionRuntime(ontology=ontology, catalogs=open_catalogs(config))


def physical(seeded, table: str) -> list[dict]:
    """Every column of every row, straight off the table — a *fresh* catalog handle each time, so
    a test can never pass on a cached read."""
    from loom.catalog import open_catalogs

    _, _, config = seeded
    rows = open_catalogs(config)["local"].scan(table).to_pylist()
    return sorted(rows, key=lambda r: r["id"])


def test_modify_rewrites_the_row_and_leaves_the_columns_nobody_declared_alone(seeded, runtime):
    """The headline. `c3` is bronze with `region='apac'` and a null `segments`; `c1` is gold with
    two segments. Upgrading one must not disturb either's unmapped columns."""
    before = physical(seeded, "crm.customers")
    assert [r["tier"] for r in before] == ["gold", "silver", "bronze"]
    assert before[2]["region"] == "apac"
    assert before[0]["segments"] == ["enterprise", "early-adopter"]

    result = runtime.run("upgradeTier", {"customer": "c3", "newTier": "gold"})

    assert result.status == APPLIED, result.failures
    assert result.before["tier"] == "bronze" and result.after["tier"] == "gold"

    after = physical(seeded, "crm.customers")
    assert [r["tier"] for r in after] == ["gold", "silver", "gold"]
    # The row that changed: one column different, everything else — mapped or not — identical.
    assert after[2] == {**before[2], "tier": "gold"}
    assert after[2]["region"] == "apac" and after[2]["segments"] is None
    # And the rows that didn't, including the one holding a value of a type Loom cannot name.
    assert after[0] == before[0] and after[1] == before[1]


def test_the_row_count_does_not_change_because_the_delete_and_the_append_are_one_commit(seeded, runtime):
    """An equality-delete that landed without its append would lose the row; an append without its
    delete would duplicate it. Both are visible as a row count, so it is worth asserting."""
    runtime.run("upgradeTier", {"customer": "c3", "newTier": "silver"})

    rows = physical(seeded, "crm.customers")
    assert [r["id"] for r in rows] == ["c1", "c2", "c3"]


def test_the_write_advances_the_snapshot_the_read_recorded(seeded, runtime):
    """The id is real, it belongs to the table the row was read from, and it is *not* the id the
    table sits at afterwards — the write itself moved it, which is what makes an uncontested run's
    own commit the next run's baseline."""
    from loom.catalog import open_catalogs

    _, _, config = seeded
    result = runtime.run("upgradeTier", {"customer": "c3", "newTier": "gold"})

    assert result.read_snapshot_id is not None
    now = open_catalogs(config)["local"].current_snapshot_id("crm.customers")
    assert now != result.read_snapshot_id
    assert result.as_json()["concurrency"] == "enforced — the write asserts the snapshot the read saw"
    assert result.attempts == 1


def test_a_failed_rule_leaves_the_lake_exactly_as_it_was(seeded, runtime):
    before = physical(seeded, "crm.customers")

    result = runtime.run("upgradeTier", {"customer": "c1", "newTier": "gold"})  # c1 is already gold

    assert result.status == REFUSED
    assert [f.code for f in result.failures] == [VALIDATION_FAILED]
    assert physical(seeded, "crm.customers") == before


def test_a_dry_run_writes_nothing(seeded, runtime):
    before = physical(seeded, "crm.customers")

    result = runtime.preview("upgradeTier", {"customer": "c3", "newTier": "gold"})

    assert result.status == PREVIEWED and result.after["tier"] == "gold"
    assert physical(seeded, "crm.customers") == before


def test_create_writes_a_row_whose_declared_types_survived_the_trip(seeded, runtime):
    """decimal and timestamptz are where a write layer usually loses its promises: the total must
    come back as a `Decimal` with its scale intact, not a float."""
    result = runtime.run("recordOrder", {"orderId": "o6", "customer": "c1", "total": "42.50"})

    assert result.status == APPLIED, result.failures
    written = next(r for r in physical(seeded, "sales.orders") if r["id"] == "o6")
    assert written["total_amount"] == Decimal("42.50")
    assert isinstance(written["created_at"], datetime) and written["created_at"].tzinfo is not None
    assert written["customer_id"] == "c1"
    assert len(physical(seeded, "sales.orders")) == 6


def test_delete_removes_one_row_and_only_that_row(seeded, runtime):
    """Loom still never drops a column or a table. This is one row, addressed by primary key,
    because an action declared `operation: delete` and a caller named it."""
    result = runtime.run("forgetCustomer", {"customer": "c2"})

    assert result.status == APPLIED, result.failures
    assert result.before["name"] == "Grace Hopper" and result.after is None
    rows = physical(seeded, "crm.customers")
    assert [r["id"] for r in rows] == ["c1", "c3"]
    # The surviving rows kept their unmapped columns too — a delete rewrites the files the row
    # lived in, so this is not free.
    assert {r["region"] for r in rows} == {"emea", "apac"}


def test_the_read_path_agrees_with_what_the_action_wrote(seeded, runtime):
    """The whole point of one ontology compiling to four surfaces: the row the action wrote is the
    row `get_customer` returns, through DuckDB, with no cache in between."""
    pytest.importorskip("duckdb", reason="needs the [duckdb] extra")
    from loom.resolver import build_resolver

    _, ontology, config = seeded
    runtime.run("upgradeTier", {"customer": "c3", "newTier": "silver"})

    resolver = build_resolver(ontology, config)
    assert resolver.get("Customer", "c3") == {
        "customerId": "c3", "name": "Alan Turing", "tier": "silver", "ltv": None
    }


def test_the_schema_is_untouched_by_every_action(seeded, runtime):
    """The port the runtime holds has no schema verb, so this cannot fail by accident — which is
    exactly why it is worth writing down once against a real catalog."""
    from loom.catalog import open_catalogs

    _, _, config = seeded
    before = open_catalogs(config)["local"].describe("crm.customers").columns

    runtime.run("upgradeTier", {"customer": "c3", "newTier": "gold"})
    runtime.run("forgetCustomer", {"customer": "c2"})

    after = open_catalogs(config)["local"].describe("crm.customers").columns
    assert {c.name: (c.iceberg_type, c.field_id) for c in after.values()} == {
        c.name: (c.iceberg_type, c.field_id) for c in before.values()
    }
    assert UNMAPPED <= set(after)


# ---- optimistic concurrency, against a catalog that really commits ---------------


class Interloper:
    """The real-Iceberg twin of `test_action.Interloper`: a `Catalog` that commits somebody else's
    write in the window between a run's read and its write.

    The competing write goes through a **second, independently opened catalog handle** — its own
    `SqlCatalog`, its own metadata cache, reaching the same metastore. That is a genuine concurrent
    writer, not a simulation of one: it produces a real Iceberg commit that really advances the
    table's `main` branch, and the run that follows it really loses.

    Deterministic without a thread, because the seam is the port. The runtime's own call sequence
    drives the interleaving — record a snapshot, read the row, write — so arming on
    `current_snapshot_id` and firing on the next `scan` places the competing commit exactly once per
    attempt, in exactly the gap, every run. Two threads and hope would test the same code path a
    fraction of the time and pass either way.
    """

    def __init__(self, inner, config, strike_on=(1,)):
        self.name = inner.name
        self.inner = inner
        self.config = config
        self.strike_on = set(strike_on)
        self.attempts = 0
        self._armed = False

    def current_snapshot_id(self, table):
        self._armed = True
        return self.inner.current_snapshot_id(table)

    def scan(self, table, columns=None, predicates=(), limit=None):
        rows = self.inner.scan(table, columns, predicates, limit)
        if self._armed:
            self._armed = False
            self.attempts += 1
            if self.attempts in self.strike_on:
                self._compete(table)
        return rows

    def _compete(self, table):
        """Somebody else's `loom run`, in effect — a different process upgrading `c1`."""
        from loom.catalog import open_catalogs

        other = open_catalogs(self.config)["local"]
        row = next(r for r in other.scan(table).to_pylist() if r["id"] == "c1")
        other.replace_row(
            table, "id", "c1",
            {**row, "region": f"apac-{self.attempts}"},
            expect_snapshot_id=other.current_snapshot_id(table),
            # Empty on purpose: this writer is not Loom. Its commit carries no `loom.edit_id`, which
            # is exactly what distinguishes it from one of ours in the table's own history.
            commit_properties={},
        )

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


def _contended(seeded, strike_on):
    from loom.catalog import open_catalogs

    _, ontology, config = seeded
    catalog = Interloper(open_catalogs(config)["local"], config, strike_on=strike_on)
    return ActionRuntime(ontology=ontology, catalogs={"local": catalog})


def test_a_real_commit_in_the_gap_refuses_the_write_and_the_row_is_unchanged(seeded):
    """M3's definition of done for this slice, against real Iceberg.

    Every attempt loses to a real commit, so the run refuses — and the row it was about is byte for
    byte what it was. Not rolled back: never written. The assertion rides inside the transaction, so
    the catalog declined the commit rather than the runtime undoing one."""
    before = physical(seeded, "crm.customers")
    runtime = _contended(seeded, strike_on=range(1, MAX_ATTEMPTS + 1))

    result = runtime.run("upgradeTier", {"customer": "c3", "newTier": "gold"})

    assert result.status == REFUSED and result.retryable
    assert [f.code for f in result.failures] == [CONFLICT]
    assert result.attempts == MAX_ATTEMPTS

    after = physical(seeded, "crm.customers")
    c3_before = next(r for r in before if r["id"] == "c3")
    c3_after = next(r for r in after if r["id"] == "c3")
    assert c3_after == c3_before, "the contested row was written despite the refusal"
    assert c3_after["tier"] == "bronze"
    assert len(after) == len(before)
    # The interloper's commits are all there. Loom lost the race and left the winner alone.
    assert next(r for r in after if r["id"] == "c1")["region"] == f"apac-{MAX_ATTEMPTS}"


def test_the_conflict_detail_names_real_snapshots_and_says_the_table_was_merely_busy(seeded):
    """The failure an agent has to act on, filled in by a real catalog. `c1` moved and `c3` did not,
    so nothing this run reads or writes changed — the honest answer is that the table is busy, which
    is a different decision from "your intent was overtaken"."""
    runtime = _contended(seeded, strike_on=range(1, MAX_ATTEMPTS + 1))

    result = runtime.run("upgradeTier", {"customer": "c3", "newTier": "gold"})

    detail = result.failures[0].detail
    assert detail["table"] == "crm.customers"
    assert isinstance(detail["expectedSnapshotId"], int)
    assert isinstance(detail["foundSnapshotId"], int)
    assert detail["expectedSnapshotId"] != detail["foundSnapshotId"]
    assert detail["changed"] == [] and detail["contended"] is False
    assert "the table is simply busy" in result.failures[0].message


def test_one_commit_in_the_gap_is_retried_and_the_run_applies(seeded):
    """The common case, and why the conflict is retried here rather than handed back. A
    table-snapshot check refuses on *any* concurrent commit, so an unrelated write to `c1` refuses a
    run about `c3` — correct and useless on its own. The retry re-reads, re-evaluates every rule
    against the row actually about to be written over, and applies."""
    runtime = _contended(seeded, strike_on=(1,))

    result = runtime.run("upgradeTier", {"customer": "c3", "newTier": "gold"})

    assert result.status == APPLIED, result.failures
    assert result.attempts == 2

    after = physical(seeded, "crm.customers")
    assert next(r for r in after if r["id"] == "c3")["tier"] == "gold"
    # Both writes survived: ours, and the one that beat us to the first attempt.
    assert next(r for r in after if r["id"] == "c1")["region"] == "apac-1"
    assert next(r for r in after if r["id"] == "c3")["region"] == "apac"  # unmapped, carried


def test_the_snapshot_assertion_is_really_on_the_transaction(seeded):
    """The guarantee rests on pyiceberg keeping the requirement we stage rather than the one its
    snapshot producer stages for itself, which is a deduplication rule in a library we do not own.
    If a release ever changes it, this write must fail loudly — a silent downgrade from a closed
    race to a narrower one is the single worst outcome available here, because everything above
    would go on claiming "enforced".

    So: drive the adapter directly with a stale expectation and require a refusal. If the assertion
    stopped being carried, this commit would simply succeed."""
    from loom.catalog import open_catalogs
    from loom.catalog.base import ConcurrencyError

    _, _, config = seeded
    catalog = open_catalogs(config)["local"]
    stale = catalog.current_snapshot_id("crm.customers")
    row = next(r for r in catalog.scan("crm.customers").to_pylist() if r["id"] == "c2")

    # Somebody else commits, so `stale` is now genuinely stale.
    catalog.replace_row(
        "crm.customers", "id", "c1",
        {**next(r for r in catalog.scan("crm.customers").to_pylist() if r["id"] == "c1"), "region": "amer"},
        expect_snapshot_id=stale, commit_properties={},
    )

    with pytest.raises(ConcurrencyError) as e:
        catalog.replace_row(
            "crm.customers", "id", "c2", {**row, "tier": "gold"},
            expect_snapshot_id=stale, commit_properties={},
        )

    assert e.value.expected == stale
    assert e.value.found != stale
    assert e.value.table == "crm.customers"
    assert next(r for r in physical(seeded, "crm.customers") if r["id"] == "c2")["tier"] == "silver"
