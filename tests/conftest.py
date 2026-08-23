"""Fixtures shared by the tests that need a real Iceberg warehouse.

`project` lives here rather than in `test_apply_iceberg` because `test_rollback_iceberg` needs the
same starting point — the shipped example, pointed at an empty warehouse — and a fixture imported
across test modules is a fixture defined twice.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from loom import build
from loom.config import find_config, load_config
from loom.errors import Diagnostics

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "retail"


@pytest.fixture
def project(tmp_path):
    """The shipped example's spec and config, pointed at an *empty* warehouse — no seed step."""
    target = tmp_path / "retail"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns(".warehouse"))
    (target / ".warehouse").mkdir()

    diag = Diagnostics()
    config = load_config(find_config(target / "ontology"), diag)
    ontology, _ = build(target / "ontology")
    diag.raise_if_errors()
    return target, ontology, config


@pytest.fixture
def seeded(tmp_path):
    """The shipped example, *seeded* — real Iceberg tables with rows in them.

    `project`'s counterpart, and here for the same reason it is: four iceberg modules had defined
    this identically before a fifth wanted it, which is exactly the "a fixture imported across test
    modules is a fixture defined twice" this file opens with. A test that needs the warehouse empty
    takes `project`; one that needs something to act on takes this; one that needs a lake **Loom has
    never written to** takes `guest`, which is a distinction the example did not use to make."""
    target = tmp_path / "retail"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns(".warehouse"))
    spec = importlib.util.spec_from_file_location("retail_seed", target / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.seed(target)

    diag = Diagnostics()
    config = load_config(find_config(target / "ontology"), diag)
    ontology, _ = build(target / "ontology")
    diag.raise_if_errors()
    return target, ontology, config


# The physical shape of the example's two base tables, as a lake that never heard of Loom would have
# them: the four declared columns of each, plus the two on `crm.customers` that no property maps.
_CUSTOMERS = (
    ["id", "full_name", "tier", "lifetime_value", "region", "segments"],
    [
        ("c1", "Ada Lovelace", "gold", 48210.50, "emea", ["enterprise", "early-adopter"]),
        ("c2", "Grace Hopper", "silver", 12750.00, "amer", ["smb"]),
        ("c3", "Alan Turing", "bronze", None, "apac", None),
        ("c4", "Karen Spärck Jones", "closed", 3120.00, "emea", ["smb"]),
    ],
)
_ORDERS = (
    ["id", "customer_id", "total_amount", "created_at"],
    [
        ("o1", "c1", "1299.99", (2026, 1, 4)),
        ("o2", "c1", "450.00", (2026, 2, 11)),
        ("o3", "c2", "89.95", (2026, 2, 14)),
        ("o4", "c2", "2100.00", (2026, 3, 2)),
        ("o5", "c2", "17.50", (2026, 3, 9)),
        ("o6", "c4", "640.25", (2026, 3, 17)),
    ],
)


@pytest.fixture
def guest(tmp_path):
    """The same two tables, built by pyiceberg alone — a lake Loom has never written to.

    **This exists because the example stopped being one.** `seed.py` now bootstraps with `loom apply`
    and loads through a declared sequence, which is the whole subject of M11's third slice — so a
    seeded warehouse arrives with `_loom_meta.applied`, `_loom_meta.loads` and `_loom_meta.sequences`
    already in it. That is the right thing for the example to demonstrate and the wrong starting
    point for the tests whose premise is *a lake Loom is a guest in is exactly the one where the
    record matters*: a test that asserts the load log is created by the load that needs it cannot
    start from a warehouse that already has one.

    So the mechanism the example used to carry lives here instead, where it is scaffolding rather
    than a demonstration — and being scaffolding, it says nothing about what Loom recommends."""
    import datetime as dt
    from decimal import Decimal

    pa = pytest.importorskip("pyarrow", reason="needs the [iceberg] extra")
    pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
    from pyiceberg.catalog.sql import SqlCatalog

    target = tmp_path / "retail"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns(".warehouse"))

    diag = Diagnostics()
    config = load_config(find_config(target / "ontology"), diag)
    ontology, _ = build(target / "ontology")
    diag.raise_if_errors()

    warehouse = Path(config.catalogs["local"].warehouse.removeprefix("file://"))
    warehouse.mkdir(parents=True, exist_ok=True)
    catalog = SqlCatalog("local", uri=config.catalogs["local"].uri, warehouse=config.catalogs["local"].warehouse)

    customers = pa.table(
        dict(zip(_CUSTOMERS[0], map(list, zip(*_CUSTOMERS[1], strict=True)), strict=True)),
        schema=pa.schema(
            [
                pa.field("id", pa.string(), nullable=False),
                pa.field("full_name", pa.string(), nullable=False),
                pa.field("tier", pa.string(), nullable=False),
                pa.field("lifetime_value", pa.float64(), nullable=True),
                pa.field("region", pa.string(), nullable=True),
                pa.field("segments", pa.list_(pa.string()), nullable=True),
            ]
        ),
    )
    ids, customer_ids, totals, days = map(list, zip(*_ORDERS[1], strict=True))
    orders = pa.table(
        {
            "id": ids,
            "customer_id": customer_ids,
            "total_amount": [Decimal(t) for t in totals],
            "created_at": [dt.datetime(*d, 12, 0, tzinfo=dt.UTC) for d in days],
        },
        schema=pa.schema(
            [
                pa.field("id", pa.string(), nullable=False),
                pa.field("customer_id", pa.string(), nullable=False),
                # decimal, not double: money must not round-trip through binary floating point.
                pa.field("total_amount", pa.decimal128(12, 2), nullable=False),
                pa.field("created_at", pa.timestamp("us", tz="UTC"), nullable=False),
            ]
        ),
    )

    for identifier, rows in (("crm.customers", customers), ("sales.orders", orders)):
        namespace = identifier.split(".")[0]
        if (namespace,) not in catalog.list_namespaces():
            catalog.create_namespace(namespace)
        catalog.create_table_if_not_exists(identifier, schema=rows.schema).append(rows)

    return target, ontology, config
