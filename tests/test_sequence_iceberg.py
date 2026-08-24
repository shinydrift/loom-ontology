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
TICKETS = [
    {
        "ticketId": "t9000",
        "customerId": "c9",
        "orderId": "o9",
        "status": "open",
        "body": "The parcel arrived open at one end.",
        "openedAt": "2026-04-02T00:00:00Z",
    }
]

ENTRIES = (
    IngestEntry(name="customers", object_type="Customer", mode="append", format="ndjson"),
    IngestEntry(name="orders", object_type="Order", mode="append", format="ndjson"),
)


def ndjson(tmp_path, rows, name):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def manifest(tmp_path, customers=CUSTOMERS, orders=ORDERS, tickets=None):
    """A manifest supplies exactly what its sequence runs — naming more is refused as loudly as
    naming fewer. So `tickets` is opt-in: the injected `nightly` sequence below runs two loads and
    the *shipped* `seed` sequence runs three."""
    import yaml

    path = tmp_path / "manifest.yaml"
    drops = {
        "customers": str(ndjson(tmp_path, customers, "c.ndjson")),
        "orders": str(ndjson(tmp_path, orders, "o.ndjson")),
    }
    if tickets is not None:
        drops["tickets"] = str(ndjson(tmp_path, tickets, "t.ndjson"))
    path.write_text(yaml.safe_dump(drops))
    return path


def sequencing(guest, edit_log="optional"):
    _, ontology, config = guest
    config = replace(
        config,
        ingest=ENTRIES,
        sequences=(IngestSequence(name="nightly", loads=("customers", "orders")),),
        ingest_posture=INGEST_ALLOWED,
        edit_log=edit_log,
    )
    return build_sequences(ontology, config, open_catalogs(config))


def rows(guest, table):
    return catalog_of(guest).scan(table).to_pylist()


def test_a_whole_run_lands_every_table_and_writes_one_row_for_the_run(guest, tmp_path):
    before = len(rows(guest, "crm.customers")), len(rows(guest, "sales.orders"))
    result = sequencing(guest).run("nightly", manifest(tmp_path))

    assert result.status == APPLIED
    after = len(rows(guest, "crm.customers")), len(rows(guest, "sales.orders"))
    assert after == (before[0] + 1, before[1] + 1)

    (row,) = rows(guest, SEQUENCE_LOG_TABLE)
    assert row["sequence"] == "nightly"
    assert json.loads(row["entries"]) == ["customers", "orders"]
    assert row["landed"] == 2
    assert result.recorded


def test_a_run_that_stops_halfway_leaves_the_first_table_really_changed(guest, tmp_path):
    """The whole reason this milestone refuses to say "atomic": there is no cross-table transaction,
    so the customers really are in the lake and somebody has to decide what to do about it."""
    before = len(rows(guest, "crm.customers"))
    bad = [{"orderId": "o9", "customerId": "c9", "total": "not-a-decimal", "placedAt": "2026-04-01T00:00:00Z"}]
    result = sequencing(guest).run("nightly", manifest(tmp_path, orders=bad))

    assert result.status == PARTIAL
    assert result.stopped_at == "orders"
    assert len(rows(guest, "crm.customers")) == before + 1

    (row,) = rows(guest, SEQUENCE_LOG_TABLE)
    assert row["status"] == PARTIAL
    assert row["stopped_at"] == "orders"
    assert (row["landed"], row["attempted"]) == (1, 2)


def test_the_loads_are_recorded_individually_as_well(guest, tmp_path):
    from loom.catalog.base import LOAD_LOG_TABLE

    result = sequencing(guest).run("nightly", manifest(tmp_path))
    logged = {r["load_id"] for r in rows(guest, LOAD_LOG_TABLE)}
    assert logged == {s.result.load_id for s in result.steps}


def test_a_required_edit_log_creates_the_sequence_log_before_anything_runs(guest, tmp_path):
    """`governance.edit_log: required` is a demand about writes, and a run is how a deployment makes
    several at once — so the third log is created up front like the other two."""
    sequencing(guest, edit_log=EDIT_LOG_REQUIRED)
    assert catalog_of(guest).table_exists(SEQUENCE_LOG_TABLE)
    assert rows(guest, SEQUENCE_LOG_TABLE) == []


# ---- the command -----------------------------------------------------------------


def shipped(guest):
    """The example's own ontology path — nothing injected.

    The `seed` sequence and the `customers`/`orders` entries it runs are in the shipped
    `loom.yaml` since M11's third slice, so the command tests exercise the config as published
    rather than one a test wrote."""
    target, _, _ = guest
    return str(target / "ontology")


def test_a_dry_run_previews_every_load_and_writes_nothing(guest, tmp_path, capsys):
    from loom.cli import main

    before = len(rows(guest, "crm.customers"))
    assert main(["sequence", "seed", str(manifest(tmp_path, tickets=TICKETS)), shipped(guest), "--dry-run"]) == 0

    out = capsys.readouterr()
    assert "customers:" in out.err and "orders:" in out.err
    # The sentence `apply` had to write first, printed above the prompt rather than in a docstring.
    assert "pretending the run was atomic" in out.err
    assert len(rows(guest, "crm.customers")) == before


def test_the_command_runs_the_sequence_and_names_the_record(guest, tmp_path, capsys):
    from loom.cli import main

    assert main(["sequence", "seed", str(manifest(tmp_path, tickets=TICKETS)), shipped(guest), "-y"]) == 0
    assert f"recorded in {SEQUENCE_LOG_TABLE}" in capsys.readouterr().err
    assert len(rows(guest, SEQUENCE_LOG_TABLE)) == 1


def test_a_sequence_that_would_stop_is_refused_before_anything_is_written(guest, tmp_path, capsys):
    """`cmd_ingest` runs a refused preview for real so the log records who tried. Here the loads
    already do that individually, and running anyway would record an order never attempted."""
    from loom.cli import main

    bad = [{"orderId": "o9", "customerId": "c9", "total": "nope", "placedAt": "2026-04-01T00:00:00Z"}]
    before = len(rows(guest, "crm.customers"))
    assert main(["sequence", "seed", str(manifest(tmp_path, orders=bad, tickets=TICKETS)), shipped(guest), "-y"]) == 1

    assert "nothing was loaded" in capsys.readouterr().err
    assert len(rows(guest, "crm.customers")) == before
    assert not catalog_of(guest).table_exists(SEQUENCE_LOG_TABLE)


def test_an_unknown_sequence_is_reported_by_the_command(guest, tmp_path, capsys):
    from loom.cli import main

    assert main(["sequence", "weekly", str(manifest(tmp_path, tickets=TICKETS)), shipped(guest)]) == 1
    assert "no sequence named 'weekly'" in capsys.readouterr().err
