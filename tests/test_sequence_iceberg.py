"""A sequence against a real Iceberg catalog — the two things the fake cannot prove.

`test_sequence.py` proves the *policy*. This proves that `_loom_meta.sequences` is a real table a
real catalog creates and appends to, and — the one that matters — that a run which stops halfway
leaves the earlier tables **actually changed** in the lake. A fake can be made to say either thing;
only pyiceberg can show that a partial run is a state somebody has to deal with rather than a status
string.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from loom.catalog import open_catalogs
from loom.catalog.base import SEQUENCE_LOG_TABLE
from loom.config import IngestEntry, IngestSequence
from loom.governance import EDIT_LOG_REQUIRED, INGEST_ALLOWED
from loom.ingest import PARTIAL, build_sequences
from loom.ingest.result import APPLIED

pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")

from test_ingest_iceberg import catalog_of  # noqa: E402

CUSTOMERS = [{"customerId": "c9", "name": "Alan Turing", "tier": "bronze", "ltv": 12.5}]
ORDERS = [
    {"orderId": "o9", "customerId": "c9", "total": "10.00", "placedAt": "2026-04-01T00:00:00Z"}
]

ENTRIES = (
    IngestEntry(name="customers", object_type="Customer", mode="append", format="ndjson"),
    IngestEntry(name="orders", object_type="Order", mode="append", format="ndjson"),
)


def ndjson(tmp_path, rows, name):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def manifest(tmp_path, customers=CUSTOMERS, orders=ORDERS):
    import yaml

    path = tmp_path / "manifest.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "customers": str(ndjson(tmp_path, customers, "c.ndjson")),
                "orders": str(ndjson(tmp_path, orders, "o.ndjson")),
            }
        )
    )
    return path


def sequencing(seeded, edit_log="optional"):
    _, ontology, config = seeded
    config = replace(
        config,
        ingest=ENTRIES,
        sequences=(IngestSequence(name="nightly", loads=("customers", "orders")),),
        ingest_posture=INGEST_ALLOWED,
        edit_log=edit_log,
    )
    return build_sequences(ontology, config, open_catalogs(config))


def rows(seeded, table):
    return catalog_of(seeded).scan(table).to_pylist()


def test_a_whole_run_lands_every_table_and_writes_one_row_for_the_run(seeded, tmp_path):
    before = len(rows(seeded, "crm.customers")), len(rows(seeded, "sales.orders"))
    result = sequencing(seeded).run("nightly", manifest(tmp_path))

    assert result.status == APPLIED
    after = len(rows(seeded, "crm.customers")), len(rows(seeded, "sales.orders"))
    assert after == (before[0] + 1, before[1] + 1)

    (row,) = rows(seeded, SEQUENCE_LOG_TABLE)
    assert row["sequence"] == "nightly"
    assert json.loads(row["entries"]) == ["customers", "orders"]
    assert row["landed"] == 2
    assert result.recorded


def test_a_run_that_stops_halfway_leaves_the_first_table_really_changed(seeded, tmp_path):
    """The whole reason this milestone refuses to say "atomic": there is no cross-table transaction,
    so the customers really are in the lake and somebody has to decide what to do about it."""
    before = len(rows(seeded, "crm.customers"))
    bad = [{"orderId": "o9", "customerId": "c9", "total": "not-a-decimal", "placedAt": "2026-04-01T00:00:00Z"}]
    result = sequencing(seeded).run("nightly", manifest(tmp_path, orders=bad))

    assert result.status == PARTIAL
    assert result.stopped_at == "orders"
    assert len(rows(seeded, "crm.customers")) == before + 1

    (row,) = rows(seeded, SEQUENCE_LOG_TABLE)
    assert row["status"] == PARTIAL
    assert row["stopped_at"] == "orders"
    assert (row["landed"], row["attempted"]) == (1, 2)


def test_the_loads_are_recorded_individually_as_well(seeded, tmp_path):
    from loom.catalog.base import LOAD_LOG_TABLE

    result = sequencing(seeded).run("nightly", manifest(tmp_path))
    logged = {r["load_id"] for r in rows(seeded, LOAD_LOG_TABLE)}
    assert logged == {s.result.load_id for s in result.steps}


def test_a_required_edit_log_creates_the_sequence_log_before_anything_runs(seeded, tmp_path):
    """`governance.edit_log: required` is a demand about writes, and a run is how a deployment makes
    several at once — so the third log is created up front like the other two."""
    sequencing(seeded, edit_log=EDIT_LOG_REQUIRED)
    assert catalog_of(seeded).table_exists(SEQUENCE_LOG_TABLE)
    assert rows(seeded, SEQUENCE_LOG_TABLE) == []


# ---- the command -----------------------------------------------------------------


def declare(seeded):
    """Add the two entries and the sequence to the example's own loom.yaml, as an operator would."""
    import yaml

    target, _, _ = seeded
    path = target / "loom.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["ingest"] = [
        *doc.get("ingest", []),
        {"name": "customers", "objectType": "Customer", "mode": "append", "format": "ndjson"},
        {"name": "orders", "objectType": "Order", "mode": "append", "format": "ndjson"},
    ]
    doc["sequences"] = [{"name": "nightly", "loads": ["customers", "orders"]}]
    path.write_text(yaml.safe_dump(doc))
    return str(target / "ontology")


def test_a_dry_run_previews_every_load_and_writes_nothing(seeded, tmp_path, capsys):
    from loom.cli import main

    path = declare(seeded)
    before = len(rows(seeded, "crm.customers"))
    assert main(["sequence", "nightly", str(manifest(tmp_path)), path, "--dry-run"]) == 0

    out = capsys.readouterr()
    assert "customers:" in out.err and "orders:" in out.err
    # The sentence `apply` had to write first, printed above the prompt rather than in a docstring.
    assert "pretending the run was atomic" in out.err
    assert len(rows(seeded, "crm.customers")) == before


def test_the_command_runs_the_sequence_and_names_the_record(seeded, tmp_path, capsys):
    from loom.cli import main

    path = declare(seeded)
    assert main(["sequence", "nightly", str(manifest(tmp_path)), path, "-y"]) == 0
    assert f"recorded in {SEQUENCE_LOG_TABLE}" in capsys.readouterr().err
    assert len(rows(seeded, SEQUENCE_LOG_TABLE)) == 1


def test_a_sequence_that_would_stop_is_refused_before_anything_is_written(seeded, tmp_path, capsys):
    """`cmd_ingest` runs a refused preview for real so the log records who tried. Here the loads
    already do that individually, and running anyway would record an order never attempted."""
    from loom.cli import main

    path = declare(seeded)
    bad = [{"orderId": "o9", "customerId": "c9", "total": "nope", "placedAt": "2026-04-01T00:00:00Z"}]
    before = len(rows(seeded, "crm.customers"))
    assert main(["sequence", "nightly", str(manifest(tmp_path, orders=bad)), path, "-y"]) == 1

    assert "nothing was loaded" in capsys.readouterr().err
    assert len(rows(seeded, "crm.customers")) == before
    assert not catalog_of(seeded).table_exists(SEQUENCE_LOG_TABLE)


def test_an_unknown_sequence_is_reported_by_the_command(seeded, tmp_path, capsys):
    from loom.cli import main

    path = declare(seeded)
    assert main(["sequence", "weekly", str(manifest(tmp_path)), path]) == 1
    assert "no sequence named 'weekly'" in capsys.readouterr().err
