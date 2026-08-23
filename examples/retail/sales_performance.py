"""Build and refresh the retail example's daily sales-performance materialization.

This is intentionally an ingestion-time aggregate, not a new Loom query primitive. Loom only
retrieves the small, typed Iceberg table; the business calculation stays explicit and repeatable.

**Two ways to land it, and the difference is the point.** `refresh_daily_sales_performance` writes
the table through pyiceberg directly — a hand-built Arrow schema kept in lockstep with the spec by
hand, a `txn.overwrite`, and a write nothing in the lake records. `write_daily_sales_performance`
stops at a Parquet file and lets the declared `daily-sales` entry do the rest: the same rows, checked
against the ontology's declared types, written as one commit stamped with its own load id, and one
row in `_loom_meta.loads` saying what happened.

**Since M11's fourth slice, the second one is what the example actually runs** — `seed.py` and the
dashboard's refresh route both go through the declared load, and neither had before. The first is
kept anyway, and its job changed rather than ended: it used to be the shipped path and is now the
*comparison*, the thing every lake already does and the record cannot see. An acceptance test runs
both against one orders snapshot and asserts they produce the same table, which is what makes the
claim checkable instead of rhetorical — what the declared entry adds is not different rows, it is a
contract and a record.

Neither computes the aggregate inside Loom. That is the boundary this milestone draws, and it is why
the second one still hands over a file.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal

import pyarrow as pa

SOURCE_TABLE = "sales.orders"
MATERIALIZATION_TABLE = "sales.daily_sales_performance"

DAILY_SALES_PERFORMANCE_SCHEMA = pa.schema(
    [
        pa.field("sales_date", pa.date32(), nullable=False),
        pa.field("gross_sales", pa.decimal128(14, 2), nullable=False),
        pa.field("order_count", pa.int64(), nullable=False),
        pa.field("unique_customers", pa.int64(), nullable=False),
        pa.field("refreshed_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("source_table", pa.string(), nullable=False),
        pa.field("source_snapshot_id", pa.int64(), nullable=False),
    ]
)


def build_daily_sales_performance(
    orders: pa.Table,
    *,
    refreshed_at: dt.datetime,
    source_snapshot_id: int,
) -> pa.Table:
    """Aggregate an orders Arrow table into deterministic daily business metrics."""
    if refreshed_at.tzinfo is None:
        raise ValueError("refreshed_at must be timezone-aware")

    totals: dict[dt.date, Decimal] = defaultdict(lambda: Decimal("0.00"))
    counts: dict[dt.date, int] = defaultdict(int)
    customers: dict[dt.date, set[str]] = defaultdict(set)
    for row in orders.to_pylist():
        day = row["created_at"].date()
        totals[day] += row["total_amount"]
        counts[day] += 1
        customers[day].add(row["customer_id"])

    days = sorted(totals)
    return pa.table(
        {
            "sales_date": days,
            "gross_sales": [totals[day] for day in days],
            "order_count": [counts[day] for day in days],
            "unique_customers": [len(customers[day]) for day in days],
            "refreshed_at": [refreshed_at] * len(days),
            "source_table": [SOURCE_TABLE] * len(days),
            "source_snapshot_id": [source_snapshot_id] * len(days),
        },
        schema=DAILY_SALES_PERFORMANCE_SCHEMA,
    )


# The ontology's names for the same seven columns. A batch handed to `loom ingest` speaks the
# *spec's* vocabulary, not the lake's: an entry maps property names to source columns and defaults
# to the identity, so a file written this way needs no `columns:` block at all. Which is also the
# honest direction of the coupling — the pipeline producing the file is writing against the
# published ontology, and `loom plan` is what moves the physical column underneath it.
PROPERTY_NAMES = {
    "sales_date": "salesDate",
    "gross_sales": "grossSales",
    "order_count": "orderCount",
    "unique_customers": "uniqueCustomers",
    "refreshed_at": "refreshedAt",
    "source_table": "sourceTable",
    "source_snapshot_id": "sourceSnapshotId",
}


def _compute(catalog, refreshed_at: dt.datetime | None) -> pa.Table:
    """The aggregate itself, from the current `sales.orders` snapshot.

    Shared by both halves deliberately: the whole claim being demonstrated is that they produce the
    *same rows* and differ only in how those rows land, and two copies of this would be two chances
    for that to stop being true."""
    source = catalog.load_table(SOURCE_TABLE)
    snapshot = source.current_snapshot()
    if snapshot is None:
        raise RuntimeError(f"{SOURCE_TABLE} has no snapshot to materialize")
    return build_daily_sales_performance(
        source.scan().to_arrow(),
        refreshed_at=refreshed_at or dt.datetime.now(dt.UTC),
        source_snapshot_id=snapshot.snapshot_id,
    )


def write_daily_sales_performance(
    catalog, path, *, refreshed_at: dt.datetime | None = None
) -> pa.Table:
    """Recompute the materialization and write it to `path` as Parquet, touching no table.

    The pipeline half of the declared load, and the one the example runs. It reads `sales.orders` —
    which is the aggregate's input, not Loom's business — and stops at a file, because everything
    after the file is what the `daily-sales` entry in `loom.yaml` describes:

        loom ingest daily-sales daily.parquet examples/retail/ontology

    Both callers in this repo do that in-process rather than over the command line — `seed.py`'s
    `materialize` and the dashboard's `/api/refresh` — and both write the file to a temporary
    directory, because it is a **handover and not an artifact**. The checked-in drops under `data/`
    are the other kind: data somebody wrote, readable in a diff, with fixed bytes. This one is the
    output of a computation that runs again tomorrow with different numbers in it.

    Which is also why re-running it is never a duplicate load: every recompute stamps a fresh
    `refreshedAt`, so the bytes differ and the derived load id differs. A second refresh is a second
    load rather than the same one twice — `derive_load_id`'s distinction, reached from the far side.

    `mode: replace` rather than `append`, for the reason this function recomputes every day rather
    than the new one: a daily aggregate is a whole answer, so appending would leave two rows per day
    and merging would leave yesterday's answer for a day the source no longer has."""
    import pyarrow.parquet as pq

    rows = _compute(catalog, refreshed_at)
    named = rows.rename_columns([PROPERTY_NAMES[c] for c in rows.column_names])
    pq.write_table(named, path)
    return named


def refresh_daily_sales_performance(catalog, *, refreshed_at: dt.datetime | None = None) -> pa.Table:
    """Recompute and atomically replace the materialization from the current orders snapshot.

    **The hand-rolled half, and since M11's fourth slice nothing shipped calls it.** It creates the
    table if it is missing, writes it with a schema this file maintains by hand, and leaves nothing
    in `_loom_meta` behind it — which is what `loom ingest` exists to change, and what `seed.py` and
    the dashboard both used to do.

    Kept rather than deleted, because it is the comparison and the comparison is checkable: an
    acceptance test runs this and `write_daily_sales_performance` against one orders snapshot and
    asserts the same table comes out either way. Delete this and the claim *the declared load adds a
    contract and a record rather than different rows* becomes something the README asserts and
    nothing verifies."""
    rows = _compute(catalog, refreshed_at)

    namespace = MATERIALIZATION_TABLE.split(".")[0]
    if (namespace,) not in catalog.list_namespaces():
        catalog.create_namespace(namespace)
    target = catalog.create_table_if_not_exists(MATERIALIZATION_TABLE, schema=DAILY_SALES_PERFORMANCE_SCHEMA)
    if target.current_snapshot() is None:
        target.append(rows)
    else:
        with target.transaction() as txn:
            txn.overwrite(rows)
    return rows
