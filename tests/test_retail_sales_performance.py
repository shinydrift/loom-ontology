from __future__ import annotations

import datetime as dt
import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow", reason="needs the [iceberg] extra")

MODULE = Path(__file__).resolve().parents[1] / "examples" / "retail" / "sales_performance.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("retail_sales_performance", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_daily_materialization_groups_orders_and_records_provenance():
    materialization = _load_module()
    refreshed_at = dt.datetime(2026, 4, 1, 9, 30, tzinfo=dt.UTC)
    orders = pa.table(
        {
            "customer_id": ["c1", "c1", "c2"],
            "total_amount": [Decimal("10.25"), Decimal("2.75"), Decimal("8.00")],
            "created_at": [
                dt.datetime(2026, 3, 1, 1, tzinfo=dt.UTC),
                dt.datetime(2026, 3, 1, 20, tzinfo=dt.UTC),
                dt.datetime(2026, 3, 2, 1, tzinfo=dt.UTC),
            ],
        }
    )

    rows = materialization.build_daily_sales_performance(
        orders, refreshed_at=refreshed_at, source_snapshot_id=42
    ).to_pylist()

    assert rows == [
        {
            "sales_date": dt.date(2026, 3, 1),
            "gross_sales": Decimal("13.00"),
            "order_count": 2,
            "unique_customers": 1,
            "refreshed_at": refreshed_at,
            "source_table": "sales.orders",
            "source_snapshot_id": 42,
        },
        {
            "sales_date": dt.date(2026, 3, 2),
            "gross_sales": Decimal("8.00"),
            "order_count": 1,
            "unique_customers": 1,
            "refreshed_at": refreshed_at,
            "source_table": "sales.orders",
            "source_snapshot_id": 42,
        },
    ]


def test_materialization_requires_an_unambiguous_refresh_time():
    materialization = _load_module()
    orders = pa.table({"customer_id": [], "total_amount": [], "created_at": []})
    with pytest.raises(ValueError, match="timezone-aware"):
        materialization.build_daily_sales_performance(
            orders, refreshed_at=dt.datetime(2026, 1, 1), source_snapshot_id=1
        )
