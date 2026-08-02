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
from loom.action import APPLIED, PREVIEWED, REFUSED, VALIDATION_FAILED, ActionRuntime
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
    """The seam the concurrency slice needs: the id is real, it belongs to the table the row was
    read from, and it is *not* the id the table sits at afterwards. Nothing checks that yet, and
    the result says so."""
    from loom.catalog import open_catalogs

    _, _, config = seeded
    result = runtime.run("upgradeTier", {"customer": "c3", "newTier": "gold"})

    assert result.read_snapshot_id is not None
    now = open_catalogs(config)["local"].current_snapshot_id("crm.customers")
    assert now != result.read_snapshot_id
    assert result.as_json()["concurrency"] == "recorded, not enforced"


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
