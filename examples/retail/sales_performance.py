"""Build and refresh the retail example's daily sales-performance materialization.

This is intentionally an ingestion-time aggregate, not a new Loom query primitive. Loom only
retrieves the small, typed Iceberg table; the business calculation stays explicit and repeatable.
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


def refresh_daily_sales_performance(catalog, *, refreshed_at: dt.datetime | None = None) -> pa.Table:
    """Recompute and atomically replace the materialization from the current orders snapshot."""
    source = catalog.load_table(SOURCE_TABLE)
    snapshot = source.current_snapshot()
    if snapshot is None:
        raise RuntimeError(f"{SOURCE_TABLE} has no snapshot to materialize")
    rows = build_daily_sales_performance(
        source.scan().to_arrow(),
        refreshed_at=refreshed_at or dt.datetime.now(dt.UTC),
        source_snapshot_id=snapshot.snapshot_id,
    )

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
