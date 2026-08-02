#!/usr/bin/env python3
"""Create and populate the local Iceberg warehouse this example reads from.

Run once, then serve:

    python examples/retail/seed.py
    loom validate --physical examples/retail/ontology
    loom serve examples/retail/ontology

This script talks to pyiceberg directly rather than going through Loom, and that's deliberate:
Loom writes one row at a time, through a declared action. Bulk loading is the *user's* concern —
the framework's claim is that it can serve, migrate and act on what's in the lake, not that it is
the way data gets there. (`loom apply` would create these tables from the spec alone; this script
also puts rows in them, and adds two columns the spec never mentions.)

The schemas below are the physical side of the contract in `ontology/*.yaml`. Change one without
the other and `loom validate --physical` will say so, which is the point.
"""

from __future__ import annotations

import datetime as dt
import shutil
import sys
from decimal import Decimal
from pathlib import Path

import pyarrow as pa

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from loom.config import find_config, load_config  # noqa: E402
from loom.errors import Diagnostics  # noqa: E402

EXAMPLE_DIR = Path(__file__).resolve().parent

CUSTOMERS_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string(), nullable=False),
        pa.field("full_name", pa.string(), nullable=False),
        pa.field("tier", pa.string(), nullable=False),
        # The only property the spec declares nullable.
        pa.field("lifetime_value", pa.float64(), nullable=True),
        # Two columns the ontology never mentions, because a real lake always has some. An
        # objectType maps a *subset* of a table's columns, so these are someone else's data:
        # `loom plan` reports them as unmanaged and leaves them alone, and the action runtime
        # carries them across a modify untouched — a modify rewrites the whole row, so anything it
        # didn't carry it would silently null.
        pa.field("region", pa.string(), nullable=True),
        # And this one has a type Loom has no name for at all: `array<T>` is deferred in spec §1.
        # It is carried the same way, because the carry-across is driven by the table's own schema
        # rather than by anything the ontology knows about the value.
        pa.field("segments", pa.list_(pa.string()), nullable=True),
    ]
)

ORDERS_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string(), nullable=False),
        pa.field("customer_id", pa.string(), nullable=False),
        # decimal, not double: money must not round-trip through binary floating point.
        pa.field("total_amount", pa.decimal128(12, 2), nullable=False),
        pa.field("created_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)


def _utc(y: int, m: int, d: int) -> dt.datetime:
    return dt.datetime(y, m, d, 12, 0, tzinfo=dt.UTC)


CUSTOMERS = pa.table(
    {
        "id": ["c1", "c2", "c3"],
        "full_name": ["Ada Lovelace", "Grace Hopper", "Alan Turing"],
        "tier": ["gold", "silver", "bronze"],
        "lifetime_value": [48210.50, 12750.00, None],
        "region": ["emea", "amer", "apac"],
        "segments": [["enterprise", "early-adopter"], ["smb"], None],
    },
    schema=CUSTOMERS_SCHEMA,
)

ORDERS = pa.table(
    {
        "id": ["o1", "o2", "o3", "o4", "o5"],
        "customer_id": ["c1", "c1", "c2", "c2", "c2"],
        "total_amount": [Decimal(x) for x in ("1299.99", "450.00", "89.95", "2100.00", "17.50")],
        "created_at": [_utc(2026, 1, 4), _utc(2026, 2, 11), _utc(2026, 2, 14), _utc(2026, 3, 2), _utc(2026, 3, 9)],
    },
    schema=ORDERS_SCHEMA,
)

TABLES = {
    "crm.customers": (CUSTOMERS_SCHEMA, CUSTOMERS),
    "sales.orders": (ORDERS_SCHEMA, ORDERS),
}


def open_sql_catalog(config, name: str = "local"):
    """Build the pyiceberg SQL catalog the example's loom.yaml describes."""
    from pyiceberg.catalog.sql import SqlCatalog

    cfg = config.catalogs[name]
    warehouse_dir = cfg.warehouse.removeprefix("file://")
    Path(warehouse_dir).mkdir(parents=True, exist_ok=True)
    return SqlCatalog(name, uri=cfg.uri, warehouse=cfg.warehouse)


def seed(example_dir: Path = EXAMPLE_DIR, fresh: bool = True):
    """Create the namespaces, tables, and rows. Idempotent when `fresh` is set."""
    diag = Diagnostics()
    config_path = find_config(example_dir / "ontology")
    assert config_path is not None, f"no loom.yaml found near {example_dir}"
    config = load_config(config_path, diag)
    diag.raise_if_errors()
    assert config is not None

    warehouse_dir = Path(config.catalogs["local"].warehouse.removeprefix("file://"))
    if fresh and warehouse_dir.exists():
        shutil.rmtree(warehouse_dir)

    catalog = open_sql_catalog(config)
    for identifier, (schema, rows) in TABLES.items():
        namespace = identifier.split(".")[0]
        if (namespace,) not in catalog.list_namespaces():
            catalog.create_namespace(namespace)
        table = catalog.create_table_if_not_exists(identifier, schema=schema)
        if table.scan().to_arrow().num_rows == 0:
            table.append(rows)
    return config, catalog


def main() -> int:
    config, catalog = seed()
    warehouse = config.catalogs["local"].warehouse
    print(f"seeded {len(TABLES)} table(s) into {warehouse}")
    for identifier in TABLES:
        n = catalog.load_table(identifier).scan().to_arrow().num_rows
        print(f"  {identifier}: {n} row(s)")
    print("\nnext:")
    print("  loom validate --physical examples/retail/ontology")
    print("  loom query Customer examples/retail/ontology --key c1")
    print("  loom run upgradeTier examples/retail/ontology --param customer=c3 --param newTier=gold")
    print("  loom serve examples/retail/ontology")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
