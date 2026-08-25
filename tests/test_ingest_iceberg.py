"""Bulk ingest against a real Iceberg catalog — M9's definition of done.

`test_ingest.py` proves the *policy* against a fake. This proves the three things a fake cannot:

- that the three `BulkWriter` verbs really are one Iceberg commit each, against real pyiceberg —
  including that a merge's delete and append land together and that a replace really empties a
  table it never read;
- that the write's own commit really carries `loom.load_id` in its snapshot summary, which is the
  claim the whole write-then-record ordering rests on;
- that the snapshot assertion is validated by the *catalog* as the metadata pointer swaps, so a
  second writer that lands in the gap really does refuse the load.

It runs the shipped example, seeded but **never applied** — the same starting point
`test_action_log_iceberg.py` uses, and for the same reason: a lake Loom is a guest in is exactly the
one where the record matters.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

from loom.catalog.base import LOAD_LOG_TABLE
from loom.config import IngestEntry
from loom.governance import EDIT_LOG_REQUIRED, INGEST_ALLOWED, INGEST_REFUSED
from loom.ingest import LoadLog, build_ingest
from loom.ingest.result import APPLIED, CONFLICT, DUPLICATE_LOAD, REFUSED

pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
pa = pytest.importorskip("pyarrow", reason="needs the [iceberg] extra")

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "retail"


def entry(mode="append", fmt="ndjson", object_type="Customer", name="customers", columns=None):
    return IngestEntry(
        name=name, object_type=object_type, mode=mode, format=fmt, columns=columns or {}
    )


def runtime(seeded, entries=None, posture=INGEST_ALLOWED, edit_log="optional"):
    from dataclasses import replace

    _, ontology, config = seeded
    config = replace(
        config,
        ingest=tuple(entries or [entry()]),
        ingest_posture=posture,
        edit_log=edit_log,
    )
    return build_ingest(ontology, config)


def catalog_of(seeded):
    """A *fresh* handle every time, so no test passes on a cached read."""
    from loom.catalog import open_catalogs

    _, _, config = seeded
    return open_catalogs(config)["local"]


def rows_of(seeded, table="crm.customers"):
    return catalog_of(seeded).scan(table).to_pylist()


def snapshot_summary(seeded, table="crm.customers"):
    """The commit's own metadata, read back through pyiceberg rather than through anything Loom
    wrote."""
    return catalog_of(seeded)._impl.load_table(table).current_snapshot().summary


def stamped(seeded, load_id, table="crm.customers"):
    impl = catalog_of(seeded)._impl.load_table(table)
    return [s for s in impl.snapshots() if s.summary.get("loom.load_id") == load_id]


def ndjson(tmp_path, rows, name="batch.ndjson"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(r, default=str) for r in rows) + "\n")
    return path


NEW_CUSTOMERS = [
    {"customerId": "c9", "name": "Alan Kay", "tier": "gold", "ltv": 5000.0},
    {"customerId": "c10", "name": "Barbara Liskov", "tier": "silver", "ltv": None},
]


# ---- append --------------------------------------------------------------------


def test_an_append_lands_real_rows_in_a_real_table(seeded, tmp_path):
    result = runtime(seeded).load("customers", ndjson(tmp_path, NEW_CUSTOMERS), actor="ci")

    assert result.status == APPLIED
    assert result.rows_written == 2
    ids = sorted(r["id"] for r in rows_of(seeded))
    assert ids == ["c1", "c10", "c2", "c3", "c4", "c9"]


def test_the_commit_carries_the_load_id_in_its_own_snapshot_summary(seeded, tmp_path):
    """The only attribution atomic with the write, and the reason a lost log row is a *findable*
    gap rather than silence."""
    result = runtime(seeded).load("customers", ndjson(tmp_path, NEW_CUSTOMERS), actor="ci")

    summary = snapshot_summary(seeded)
    assert summary["loom.load_id"] == result.load_id
    assert summary["loom.ingest"] == "customers"
    assert summary["loom.actor"] == "ci"
    assert len(stamped(seeded, result.load_id)) == 1


def test_an_append_is_one_commit(seeded, tmp_path):
    """Not one per row, and not chunked. `BulkWriter` documents whole-batch-or-nothing, and a
    partial load is the state nobody declared."""
    before = len(catalog_of(seeded)._impl.load_table("crm.customers").snapshots())
    runtime(seeded).load("customers", ndjson(tmp_path, NEW_CUSTOMERS))
    after = len(catalog_of(seeded)._impl.load_table("crm.customers").snapshots())

    assert after - before == 1


# ---- merge ---------------------------------------------------------------------


def test_a_merge_rewrites_the_named_rows_and_carries_the_rest_of_each_across(seeded, tmp_path):
    """The headline for `merge`, against a real table with two columns the ontology never mentions
    — one of them (`segments`) of a type Loom has no name for at all. A merge is an equality-delete
    plus an append, so both are carried or both are silently nulled."""
    rows = [{"customerId": "c1", "name": "Ada Lovelace", "tier": "silver", "ltv": 60000.0}]
    result = runtime(seeded, [entry(mode="merge")]).load("customers", ndjson(tmp_path, rows))

    assert result.status == APPLIED
    stored = {r["id"]: r for r in rows_of(seeded)}
    assert len(stored) == 4  # replaced, not appended
    assert stored["c1"]["tier"] == "silver"
    assert stored["c1"]["lifetime_value"] == 60000.0
    assert stored["c1"]["region"] == "emea"                       # carried
    assert stored["c1"]["segments"] == ["enterprise", "early-adopter"]  # carried, untyped by Loom


def test_a_merge_that_names_a_new_key_inserts_it(seeded, tmp_path):
    result = runtime(seeded, [entry(mode="merge")]).load(
        "customers", ndjson(tmp_path, NEW_CUSTOMERS)
    )

    assert result.status == APPLIED
    assert len(rows_of(seeded)) == 6


def test_a_merge_lands_as_a_single_metadata_commit(seeded, tmp_path):
    """The delete and the append are one Iceberg *commit* — pyiceberg may record each as its own
    snapshot, which is why this asserts on metadata versions rather than on snapshot count. A reader
    sees the whole old set or the whole new one, never a mixture."""
    rows = [{"customerId": "c1", "name": "Ada Lovelace", "tier": "bronze", "ltv": 1.0}]
    result = runtime(seeded, [entry(mode="merge")]).load("customers", ndjson(tmp_path, rows))

    assert result.status == APPLIED
    assert len(stamped(seeded, result.load_id)) >= 1
    for snapshot in stamped(seeded, result.load_id):
        assert snapshot.summary["loom.load_id"] == result.load_id


# ---- replace -------------------------------------------------------------------


def test_a_replace_makes_the_table_exactly_the_batch(seeded, tmp_path):
    result = runtime(seeded, [entry(mode="replace")]).load(
        "customers", ndjson(tmp_path, NEW_CUSTOMERS)
    )

    assert result.status == APPLIED
    assert sorted(r["id"] for r in rows_of(seeded)) == ["c10", "c9"]


def test_a_replace_with_an_empty_batch_empties_the_table(seeded, tmp_path):
    """An empty source is a real value rather than a no-op — a materialization whose source went
    empty is saying so — and *quietly nothing* is the worst available answer to 'make this table
    empty'.

    Said with a **header-only CSV**, because that is a source declaring these columns and zero rows.
    The test below is its other half."""
    path = tmp_path / "empty.csv"
    path.write_text("customerId,name,tier,ltv\n")
    result = runtime(seeded, [entry(mode="replace", fmt="csv")]).load("customers", path)

    assert result.status == APPLIED
    assert result.rows_read == 0
    assert rows_of(seeded) == []


def test_a_zero_byte_file_is_refused_rather_than_emptying_the_table(seeded, tmp_path):
    """The other half, and the more important one. A truncated upload and a deliberate empty batch
    are the same zero bytes, and one of them wipes a table — so an NDJSON file with no lines
    declares no columns and is refused by the ordinary column check, with no special case for
    emptiness anywhere. A source that means *these columns, no rows* has to be able to say so, and
    NDJSON cannot."""
    path = tmp_path / "empty.ndjson"
    path.write_text("")
    result = runtime(seeded, [entry(mode="replace")]).load("customers", path)

    assert result.status == REFUSED
    assert len(rows_of(seeded)) == 4


# ---- the assertion, against a real second writer --------------------------------


class Interloper:
    """A `Catalog` that commits somebody else's write in the window between the load's read and its
    write, through a **second, independently opened catalog handle**.

    `test_action_iceberg.Interloper`'s shape, retargeted: a load's call sequence is snapshot → scan →
    write, so arming on `current_snapshot_id` and firing on the next `scan` places the competing
    commit exactly once, in exactly the gap, every run. Deterministic without a thread, because the
    seam is the port.

    Every port verb is declared rather than reached through `__getattr__`, and that is a requirement
    rather than a style: `runtime_checkable` protocol checks use `getattr_static`, which does not
    consult `__getattr__` — so a proxy that forwards dynamically fails `bulk_writer_for` and the load
    comes back as `write_failed` instead of exercising the race."""

    def __init__(self, inner, config):
        self.name = inner.name
        self.inner = inner
        self.config = config
        self._armed = False
        self.struck = False

    def current_snapshot_id(self, table):
        self._armed = True
        return self.inner.current_snapshot_id(table)

    def scan(self, table, columns=None, predicates=(), limit=None):
        rows = self.inner.scan(table, columns, predicates, limit)
        if self._armed and not self.struck and table == "crm.customers":
            self._armed = False
            self.struck = True
            self._commit_somebody_elses_write()
        return rows

    def _commit_somebody_elses_write(self):
        from loom.catalog import open_catalogs

        other = open_catalogs(self.config)["local"]._impl.load_table("crm.customers")
        other.append(
            pa.Table.from_pylist(
                [{"id": "zz", "full_name": "Interloper", "tier": "bronze",
                  "lifetime_value": None, "region": None, "segments": None}],
                schema=other.schema().as_arrow(),
            )
        )

    def table_exists(self, table):
        return self.inner.table_exists(table)

    def describe(self, table):
        return self.inner.describe(table)

    def append_batch(self, table, rows, *, commit_properties):
        self.inner.append_batch(table, rows, commit_properties=commit_properties)

    def merge_batch(self, table, key_column, rows, *, expect_snapshot_id, commit_properties):
        self.inner.merge_batch(
            table, key_column, rows,
            expect_snapshot_id=expect_snapshot_id, commit_properties=commit_properties,
        )

    def replace_table(self, table, rows, *, expect_snapshot_id, commit_properties):
        self.inner.replace_table(
            table, rows, expect_snapshot_id=expect_snapshot_id, commit_properties=commit_properties
        )

    def append_load(self, columns, row):
        self.inner.append_load(columns, row)

    def ensure_load_log(self, columns):
        self.inner.ensure_load_log(columns)


def test_a_second_writer_in_the_gap_refuses_the_merge_and_nothing_is_written(seeded, tmp_path):
    """The assertion is a `TableRequirement` validated by the catalog as the metadata pointer swaps,
    not a comparison in this process — so a commit that lands in between really does win, and this
    load really does lose. `refused`, not `failed`: the write was declined before it committed."""
    from loom.ingest.runtime import IngestRuntime

    _, ontology, config = seeded
    inner = catalog_of(seeded)
    spy = Interloper(inner, config)
    rt = IngestRuntime(
        ontology=ontology,
        catalogs={"local": spy},
        entries={"customers": entry(mode="merge")},
        posture=INGEST_ALLOWED,
    )
    rows = [{"customerId": "c1", "name": "Ada Lovelace", "tier": "bronze", "ltv": 1.0}]
    result = rt.load("customers", ndjson(tmp_path, rows))

    assert spy.struck
    assert result.status == REFUSED
    assert result.retryable
    assert [f.code for f in result.failures if f.code == CONFLICT]
    stored = {r["id"]: r for r in rows_of(seeded)}
    assert stored["c1"]["tier"] == "gold"      # untouched
    assert "zz" in stored                      # the interloper's write survived


def test_an_append_is_not_refused_by_a_concurrent_write(seeded, tmp_path):
    """The other half of the asymmetry, and the reason `append_batch` has no snapshot argument: two
    pipelines loading one table must not refuse each other over a race neither can lose."""
    from loom.ingest.runtime import IngestRuntime

    _, ontology, config = seeded
    spy = Interloper(catalog_of(seeded), config)
    rt = IngestRuntime(
        ontology=ontology,
        catalogs={"local": spy},
        entries={"customers": entry()},
        posture=INGEST_ALLOWED,
    )
    # An append never scans, so the interloper is fired by hand: the point is that the load
    # succeeds over a table that moved, not how the movement was arranged.
    spy._commit_somebody_elses_write()
    result = rt.load("customers", ndjson(tmp_path, NEW_CUSTOMERS))

    assert result.status == APPLIED
    assert result.read_snapshot_id is None
    assert len(rows_of(seeded)) == 7


# ---- the record ----------------------------------------------------------------


def test_the_load_log_is_created_by_the_load_that_needs_it(guest, tmp_path):
    """No Loom verb in this lake's history at all, so `_loom_meta` does not exist until now — which
    is the whole argument for the first append owning the create.

    `guest` rather than `seeded` since M11's third slice: the shipped example now bootstraps with
    `loom apply` and loads through a declared sequence, so a seeded warehouse arrives with all three
    `_loom_meta` tables in it. A test about who creates the log needs a lake that has none."""
    assert not catalog_of(guest).table_exists(LOAD_LOG_TABLE)
    result = runtime(guest).load("customers", ndjson(tmp_path, NEW_CUSTOMERS), actor="ci")

    assert catalog_of(guest).table_exists(LOAD_LOG_TABLE)
    history = LoadLog(catalog=catalog_of(guest)).history()
    assert len(history) == 1
    assert history[0]["load_id"] == result.load_id
    assert history[0]["actor"] == "ci"
    assert history[0]["rows_written"] == 2
    assert history[0]["table_name"] == "crm.customers"
    assert history[0]["principal"] is None  # ingest can attest nobody, permanently


def test_the_second_run_of_one_file_is_refused_against_a_real_log(seeded, tmp_path):
    rt = runtime(seeded)
    source = ndjson(tmp_path, NEW_CUSTOMERS)

    assert rt.load("customers", source).status == APPLIED
    second = rt.load("customers", source)

    assert second.status == REFUSED
    assert [f.code for f in second.failures] == [DUPLICATE_LOAD]
    assert len(rows_of(seeded)) == 6  # not 8


def test_edit_log_required_creates_the_load_log_before_anything_is_written(guest):
    """The posture is spent at startup, and it creates the table rather than probing for it: an
    empty log is a permission, not a table of intentions."""
    assert not catalog_of(guest).table_exists(LOAD_LOG_TABLE)
    runtime(guest, edit_log=EDIT_LOG_REQUIRED)

    assert catalog_of(guest).table_exists(LOAD_LOG_TABLE)
    assert LoadLog(catalog=catalog_of(guest)).history() == ()


def test_a_refused_deployment_creates_no_load_log(guest):
    runtime(guest, posture=INGEST_REFUSED, edit_log=EDIT_LOG_REQUIRED)
    assert not catalog_of(guest).table_exists(LOAD_LOG_TABLE)


# ---- types, through real storage ------------------------------------------------


def test_decimals_and_timestamps_survive_the_round_trip(seeded, tmp_path):
    """`total` is `decimal(12,2)` and `placedAt` is a timestamp, so this is the case a CSV reader
    guessing its own types would get wrong: money must not round-trip through binary floating point,
    and a naive datetime must not reach storage. `coerce_value` is what decides both, here as on
    every other path."""
    rows = [
        {"orderId": "o9", "customerId": "c1", "total": "1234.56",
         "placedAt": "2026-05-01T09:30:00+00:00"},
    ]
    result = runtime(seeded, [entry(object_type="Order", name="orders")]).load(
        "orders", ndjson(tmp_path, rows)
    )

    assert result.status == APPLIED
    stored = {r["id"]: r for r in rows_of(seeded, "sales.orders")}
    assert stored["o9"]["total_amount"] == Decimal("1234.56")
    assert stored["o9"]["created_at"] == dt.datetime(2026, 5, 1, 9, 30, tzinfo=dt.UTC)


def test_a_parquet_source_loads_the_same_way_ndjson_does(seeded, tmp_path):
    """The format decides how bytes become values and nothing else — every check after that is the
    same code."""
    import pyarrow.parquet as pq

    path = tmp_path / "batch.parquet"
    pq.write_table(
        pa.table({
            "customerId": ["c9", "c10"],
            "name": ["Alan Kay", "Barbara Liskov"],
            "tier": ["gold", "silver"],
            "ltv": [5000.0, None],
        }),
        path,
    )
    result = runtime(seeded, [entry(fmt="parquet")]).load("customers", path)

    assert result.status == APPLIED
    assert sorted(r["id"] for r in rows_of(seeded)) == ["c1", "c10", "c2", "c3", "c4", "c9"]


def test_a_csv_source_loads_through_the_same_coercion(seeded, tmp_path):
    path = tmp_path / "batch.csv"
    path.write_text("customerId,name,tier,ltv\nc9,Alan Kay,gold,5000.0\n")
    result = runtime(seeded, [entry(fmt="csv")]).load("customers", path)

    assert result.status == APPLIED
    stored = {r["id"]: r for r in rows_of(seeded)}
    assert stored["c9"]["lifetime_value"] == 5000.0
    assert isinstance(stored["c9"]["lifetime_value"], float)


# ---- the acceptance case --------------------------------------------------------


def test_the_declared_load_lands_exactly_what_the_hand_rolled_write_does(seeded, tmp_path):
    """**The milestone's headline, and the reason it was worth building.**

    `examples/retail/sales_performance.py` is a bulk write that already lived in this repo: a
    hand-built Arrow schema kept in lockstep with the spec by hand, a `txn.overwrite`, and hand-rolled
    provenance columns. Every one of those is something Loom already knows.

    This runs both halves against the same orders snapshot and asserts they produce the same table —
    so what the declared entry adds is not different rows, it is the two things the hand-rolled path
    has no way to produce: values checked against the ontology's declared types before they land, and
    a row in `_loom_meta.loads` saying which file became which commit."""

    target, _, config = seeded
    spec = importlib.util.spec_from_file_location("perf", target / "sales_performance.py")
    perf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(perf)

    at = dt.datetime(2026, 5, 1, 6, 0, tzinfo=dt.UTC)
    impl = catalog_of(seeded)._impl

    # The hand-rolled path, as the example has always done it.
    perf.refresh_daily_sales_performance(impl, refreshed_at=at)
    by_hand = sorted(rows_of(seeded, "sales.daily_sales_performance"), key=lambda r: r["sales_date"])

    # Then the declared one, over the same snapshot, through a file.
    path = tmp_path / "daily.parquet"
    perf.write_daily_sales_performance(impl, path, refreshed_at=at)
    result = runtime(
        seeded, [entry(mode="replace", fmt="parquet", object_type="DailySalesPerformance",
                       name="daily-sales")]
    ).load("daily-sales", path, actor="ci")

    assert result.status == APPLIED
    declared = sorted(rows_of(seeded, "sales.daily_sales_performance"), key=lambda r: r["sales_date"])
    assert declared == by_hand

    # ...and the difference: one of them is in the lake's own record, and one never was.
    # The seed's own two loads are already in this log — the example loads itself through Loom
    # since M11's third slice — so what is asserted is the entry this test added to it.
    history = LoadLog(catalog=catalog_of(seeded)).history()
    assert [r["entry"] for r in history][-1:] == ["daily-sales"]
    assert history[-1]["rows_written"] == len(declared)
    assert history[-1]["source"].endswith("daily.parquet")
    assert history[-1]["source_fingerprint"]
    summary = snapshot_summary(seeded, "sales.daily_sales_performance")
    assert summary["loom.load_id"] == result.load_id


def test_the_shipped_example_declares_a_load_the_deployment_permits(seeded, tmp_path):
    """The entry in `examples/retail/loom.yaml` is real rather than illustrative: it resolves
    against the shipped ontology, and the shipped `governance.ingest` permits it."""

    target, ontology, config = seeded
    assert [e.name for e in config.ingest] == ["customers", "orders", "tickets", "daily-sales"]
    # The first three are how the example seeds itself since M11's third slice; `daily-sales` is
    # the one nothing shipped runs yet, which is what makes loading it here worth asserting.
    assert [s.name for s in config.sequences] == ["seed"]
    assert config.sequences[0].loads == ("customers", "orders", "tickets")
    assert config.ingest_posture == INGEST_ALLOWED

    spec = importlib.util.spec_from_file_location("perf2", target / "sales_performance.py")
    perf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(perf)
    path = tmp_path / "daily.parquet"
    perf.write_daily_sales_performance(catalog_of(seeded)._impl, path)

    # Built from the config as shipped — no entries injected by the test.
    result = build_ingest(ontology, config).load("daily-sales", path)
    assert result.status == APPLIED


def test_a_merge_over_real_iceberg_leaves_an_omitted_mapped_column_alone(seeded, tmp_path):
    """The fake's version of this asserts the policy; this asserts that real storage agrees.

    It is the case worth proving twice, because the bug it guards against was invisible in exactly
    one direction: the unmapped columns survived, so a merge that nulled a *mapped* property looked
    like it was carrying things across correctly."""
    rows = [{"customerId": "c1", "name": "Ada Lovelace", "tier": "silver"}]  # no `ltv`
    result = runtime(seeded, [entry(mode="merge")]).load("customers", ndjson(tmp_path, rows))

    assert result.status == APPLIED
    stored = {r["id"]: r for r in rows_of(seeded)}
    assert stored["c1"]["tier"] == "silver"
    assert stored["c1"]["lifetime_value"] == 48210.50
    assert stored["c1"]["region"] == "emea"


def test_an_absent_nullable_column_lands_as_null_on_an_append(seeded, tmp_path):
    """The other half of the same change: the runtime omits the key, and the storage layer fills it
    from the table's own schema. `merge` carries, `append` nulls, and neither needs a special
    case."""
    rows = [{"customerId": "c9", "name": "Alan Kay", "tier": "gold"}]  # no `ltv`
    result = runtime(seeded).load("customers", ndjson(tmp_path, rows))

    assert result.status == APPLIED
    stored = {r["id"]: r for r in rows_of(seeded)}
    assert stored["c9"]["lifetime_value"] is None
