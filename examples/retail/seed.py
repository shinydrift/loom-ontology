#!/usr/bin/env python3
"""Build the local Iceberg warehouse this example reads from, in the three stages it really has.

Run once, then serve:

    python examples/retail/seed.py
    loom validate --physical examples/retail/ontology
    loom serve examples/retail/ontology

**This script used to be one stage, and hiding the other two was costing the example its point.**
It created the tables, appended the rows, and built `crm.customers` with six columns — four the
ontology declares and two it does not — all in one `pa.schema(...)`. Which made `region` and
`segments` read like a Loom decision, when the whole reason they are there is that they are *not*:
§2 rule 7 says a column no property maps is somebody else's data, reported by `plan`, never dropped,
and carried across untouched by every write. A column born in the same breath as the managed ones
cannot demonstrate that. A column that arrives afterwards, from a writer that is not Loom, is what
the rule is actually about.

So the three stages are separate now, and only the last one is outside the framework:

1. **`bootstrap`** — `loom apply`, from nothing but the spec. Every column the ontology declares,
   created by Loom's own migration engine, and not one more.
2. **`load`** — `loom sequence seed`, which runs the declared `customers` and `orders` loads from
   `data/manifest.yaml`. Checked against the declared types, one commit each, and a row apiece in
   `_loom_meta.loads`.
3. **`arrive`** — pyiceberg directly, adding `region` and `segments` and filling them. This stage is
   *supposed* to be outside Loom. It is what a real lake does: a team adds two columns to a table
   for a reason the ontology never hears about, and everything Loom does afterwards has to leave
   them alone.

Then `materialize`, which is stage 2 again on a delay: the daily aggregate is computed *from* the
orders stage 2 landed, so it cannot be part of the same run as its own input. It is a fourth call
rather than a fourth stage.

What has not changed is the claim M9 narrowed: **Loom is not the way data is produced or moved, but
it is the way a batch becomes rows in a table the ontology describes.** Stage 3 produces data and
alters a schema, so it is not Loom's. Stages 1 and 2 are entirely Loom's — and until this rewrite
neither of them was in the example anybody actually ran.

`segments` is also the one column that could never be declared: `list<string>` has no name in the
spec, since §1 defers `array<T>`. `region` is a plain string and is unmanaged **by choice**, which
is the more interesting half — the framework is not stopping anyone, and what the example is showing
is what happens when a lake holds columns the ontology is not trying to own.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pyarrow as pa

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sales_performance import write_daily_sales_performance  # noqa: E402

from loom.catalog import open_catalogs  # noqa: E402
from loom.config import find_config, load_config  # noqa: E402
from loom.errors import Diagnostics  # noqa: E402
from loom.ingest import build_ingest, build_sequences  # noqa: E402
from loom.migrate import apply_plan, diff_ontology, snapshot_spec  # noqa: E402
from loom.ontology import build  # noqa: E402

EXAMPLE_DIR = Path(__file__).resolve().parent

# ---- stage 3: the two columns that are not Loom's ----------------------------------

UNMANAGED = {
    # A plain string. Nothing stops this being a property — it is unmanaged because this example
    # chose it, which is the point: rule 7 governs what Loom does with columns it was not told
    # about, not only columns it could not have been told about.
    "region": (pa.string(), {"c1": "emea", "c2": "amer", "c3": "apac", "c4": "emea"}),
    # And this one has a type Loom has no name for at all: `array<T>` is deferred in spec §1. It is
    # carried across the same way, because the carry-across is driven by the table's own schema
    # rather than by anything the ontology knows about the value.
    "segments": (
        pa.list_(pa.string()),
        {"c1": ["enterprise", "early-adopter"], "c2": ["smb"], "c3": None, "c4": ["smb"]},
    ),
}


def open_sql_catalog(config, name: str = "local"):
    """Build the pyiceberg SQL catalog the example's loom.yaml describes.

    Still here, and still used, because stage 3 is genuinely not a Loom operation — and because the
    dashboard reaches for it for the same reason."""
    from pyiceberg.catalog.sql import SqlCatalog

    cfg = config.catalogs[name]
    warehouse_dir = cfg.warehouse.removeprefix("file://")
    Path(warehouse_dir).mkdir(parents=True, exist_ok=True)
    return SqlCatalog(name, uri=cfg.uri, warehouse=cfg.warehouse)


def bootstrap(ontology, catalogs) -> None:
    """Stage 1 — every table, from nothing but the spec, through Loom's own migration engine.

    `loom apply` called as a library rather than shelled out. Note what it creates: exactly the
    columns the ontology declares. Which is the reason stage 3 has to exist at all — an all-Loom
    bootstrap structurally *cannot* produce a table with columns Loom does not manage."""
    diag = Diagnostics()
    plan = diff_ontology(ontology, catalogs, diag)
    diag.raise_if_errors()
    result = apply_plan(plan, catalogs, snapshot_spec(str(EXAMPLE_DIR / "ontology")))
    if not result.ok:
        raise RuntimeError(f"the example's own spec did not apply: {result.status}")


def load(ontology, config, catalogs) -> None:
    """Stage 2 — the declared rows, through the declared loads, in the declared order.

    `loom sequence seed`. Every row is checked against the ontology's types on the way in, lands as
    one commit per table, and leaves a row in `_loom_meta.loads` — which is the whole difference
    between this and the `table.append(rows)` it replaces."""
    runtime = build_sequences(ontology, config, catalogs)
    result = runtime.run("seed", EXAMPLE_DIR / "data" / "manifest.yaml", actor="seed.py")
    if not result.ok:
        stopped = result.stopped_at or "?"
        raise RuntimeError(
            f"the seed sequence stopped at '{stopped}' — {len(result.landed)} load(s) landed. "
            f"Loading the same drop twice is refused on purpose: a load's id is derived from the "
            f"file's bytes, so the same file through the same entry is one load. Delete "
            f".warehouse, or call seed(fresh=True)."
        )


def arrive(catalog) -> None:
    """Stage 3 — two columns Loom did not create, filled by something that is not Loom.

    **Deliberately the only stage that talks to pyiceberg**, and deliberately last. The order is the
    demonstration: the ontology described a table, Loom created and filled exactly what it described,
    and then somebody else added their own columns to it for their own reasons. Everything Loom does
    from here — `plan`, a `modifyObject`, a `merge` load — has to leave them alone, and does.

    A schema update plus an overwrite rather than a merge, because this is what the other team's
    pipeline looks like: it owns these two columns and rewrites them wholesale."""
    from pyiceberg import types as t

    table = catalog.load_table("crm.customers")
    with table.update_schema() as update:
        update.add_column("region", t.StringType())
        update.add_column("segments", t.ListType(element_id=-1, element_type=t.StringType(), element_required=False))

    table = catalog.load_table("crm.customers")
    rows = table.scan().to_arrow()
    keys = rows.column("id").to_pylist()
    for name, (arrow_type, values) in UNMANAGED.items():
        column = pa.array([values.get(key) for key in keys], type=arrow_type)
        rows = rows.set_column(rows.schema.get_field_index(name), name, column)
    with table.transaction() as txn:
        txn.overwrite(rows)


def materialize(ontology, config, catalogs) -> None:
    """The daily aggregate, computed outside Loom and landed through the `daily-sales` entry.

    **Loom does not compute this, and the file is where the boundary is.**
    `write_daily_sales_performance` reads the orders snapshot and stops at a Parquet file; the
    declared entry does the rest. That split is the milestone's whole claim: Loom is not the way data
    is produced or moved, but it is the way a batch becomes rows in a table the ontology describes.

    The file is a **handover and not an artifact**, so it goes to a temporary directory rather than
    beside the checked-in seed drops. Which is the difference between this entry and the other two —
    `customers.ndjson` is data somebody wrote and can read in a diff, and this is the output of a
    computation that will run again tomorrow with different numbers in it.

    That also settles the duplicate question the checked-in drops raise: every recompute stamps a new
    `refreshedAt`, so the bytes differ, so the derived load id differs and a second refresh is a
    second load rather than the same one twice. Exactly the distinction `derive_load_id` exists to
    draw, arrived at from the other side."""
    import tempfile

    runtime = build_ingest(ontology, config, catalogs)
    with tempfile.TemporaryDirectory(prefix="loom-daily-") as tmp:
        path = Path(tmp) / "daily.parquet"
        write_daily_sales_performance(open_sql_catalog(config), path)
        result = runtime.load("daily-sales", path, actor="seed.py")
    if not result.ok:
        raise RuntimeError(
            f"the daily-sales load was refused: "
            f"{'; '.join(f.message for f in result.failures) or result.status}"
        )


def seed(example_dir: Path = EXAMPLE_DIR, fresh: bool = True):
    """The three stages, in order. Idempotent when `fresh` is set, and refuses to double-load
    otherwise — which is `derive_load_id` doing exactly what it exists for."""
    diag = Diagnostics()
    config_path = find_config(example_dir / "ontology")
    assert config_path is not None, f"no loom.yaml found near {example_dir}"
    config = load_config(config_path, diag)
    diag.raise_if_errors()
    assert config is not None

    warehouse_dir = Path(config.catalogs["local"].warehouse.removeprefix("file://"))
    if fresh and warehouse_dir.exists():
        shutil.rmtree(warehouse_dir)
    warehouse_dir.mkdir(parents=True, exist_ok=True)

    ontology, _ = build(example_dir / "ontology")
    catalogs = open_catalogs(config)

    bootstrap(ontology, catalogs)
    load(ontology, config, catalogs)
    catalog = open_sql_catalog(config)
    arrive(catalog)
    materialize(ontology, config, catalogs)
    return config, catalog


def main() -> int:
    config, catalog = seed()
    warehouse = config.catalogs["local"].warehouse
    identifiers = [
        "crm.customers",
        "sales.orders",
        "support.tickets",
        "sales.daily_sales_performance",
    ]
    print(f"seeded {len(identifiers)} table(s) into {warehouse}")
    for identifier in identifiers:
        n = catalog.load_table(identifier).scan().to_arrow().num_rows
        print(f"  {identifier}: {n} row(s)")
    print("\n  crm.customers also holds region and segments, which no property maps: added after")
    print("  the load by something that is not Loom, and left alone by everything Loom does from")
    print("  here (spec-v0 §2 rule 7).")
    print("\n  support.tickets holds the text this example searches by *meaning*. Nothing is")
    print("  embedded yet — a spec may declare `semantic:` and be served before a reconcile has")
    print("  ever run, so `match_` refuses with the command below rather than answering empty.")
    print("\nnext:")
    print("  loom validate --physical examples/retail/ontology")
    print("  loom query Customer examples/retail/ontology --key c1")
    print("  loom query DailySalesPerformance examples/retail/ontology --key 2026-02-11")
    print("  loom run upgradeTier examples/retail/ontology --param customer=c3 --param newTier=gold")
    print("\n  # the ranked plane: reconcile once (downloads a ~130MB model), then ask it something")
    print("  # nobody in the data actually wrote")
    print("  loom embed examples/retail/ontology")
    print("  loom query SupportTicket examples/retail/ontology --match 'payment dispute'")
    print("  loom query SupportTicket examples/retail/ontology --match 'never turned up' \\")
    print("      --via raisedBy.tier=gold")
    print("\n  loom serve examples/retail/ontology")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
