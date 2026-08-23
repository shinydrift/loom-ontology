"""`sequences:` — the grammar, the manifest, and the sentence about atomicity.

The assertion this module exists for is the one `apply` had to make first: a sequence **stops at the
first refusal and reports exactly which loads landed**, rather than rolling anything back or
claiming the run was atomic. Iceberg's unit is the table; there is no cross-table transaction to be
had, and the tests below check that the result says so in both directions — the loads before the
stop are *landed*, and the ones after it were never attempted.

`test_ingest.py`'s bargain, one level up: the fake catalog means the whole runtime is testable with
no Iceberg stack, and what is asserted is the policy.
"""

from __future__ import annotations

import json

import pytest
import yaml

from loom.catalog.base import SEQUENCE_LOG_TABLE, CatalogError
from loom.config import IngestEntry, IngestSequence, load_config
from loom.errors import Diagnostics
from loom.ingest import (
    PARTIAL,
    SequenceError,
    SequenceRuntime,
    derive_sequence_id,
    read_manifest,
)
from loom.ingest.result import APPLIED, PREVIEWED, REFUSED
from loom.ingest.runtime import IngestRuntime
from loom.ontology import build
from test_ingest import VALID, FakeBulkCatalog, ndjson

# ---- the grammar -----------------------------------------------------------------


def config_with(tmp_path, body: str):
    path = tmp_path / "loom.yaml"
    path.write_text(
        "version: 0\n"
        "catalogs:\n  local: { type: iceberg-sql, uri: 'sqlite:///x.db', warehouse: 'file://w' }\n"
        "engine: { type: duckdb }\n" + body
    )
    diag = Diagnostics()
    config = load_config(path, diag)
    return config, diag


INGEST = (
    "ingest:\n"
    "  - { name: customers, objectType: Customer, mode: append, format: ndjson }\n"
    "  - { name: orders, objectType: Order, mode: append, format: ndjson }\n"
    "governance: { ingest: allowed }\n"
)


def test_a_sequence_is_an_ordered_list_of_declared_entry_names(tmp_path):
    config, diag = config_with(tmp_path, INGEST + "sequences:\n  - { name: nightly, loads: [customers, orders] }\n")
    diag.raise_if_errors()
    (sequence,) = config.sequences
    assert sequence.name == "nightly"
    assert sequence.loads == ("customers", "orders")


def test_the_order_is_the_list_and_not_the_order_of_ingest(tmp_path):
    """Declaration order in `ingest:` means nothing — an entry moved during review must not silently
    change what runs when."""
    config, diag = config_with(tmp_path, INGEST + "sequences:\n  - { name: nightly, loads: [orders, customers] }\n")
    diag.raise_if_errors()
    assert config.sequences[0].loads == ("orders", "customers")


def test_a_sequence_naming_an_entry_that_does_not_exist_is_refused_here(tmp_path):
    """Checked against `ingest:` at config load, unlike `objectType`, because both are in this file."""
    _, diag = config_with(tmp_path, INGEST + "sequences:\n  - { name: nightly, loads: [customers, nope] }\n")
    assert any("no ingest entry named 'nope'" in e.message for e in diag.errors)


def test_a_near_miss_entry_name_is_suggested(tmp_path):
    _, diag = config_with(tmp_path, INGEST + "sequences:\n  - { name: nightly, loads: [customer] }\n")
    assert any("customers" in (e.hint or "") for e in diag.errors)


def test_an_entry_twice_in_one_sequence_is_refused(tmp_path):
    _, diag = config_with(
        tmp_path, INGEST + "sequences:\n  - { name: nightly, loads: [customers, customers] }\n"
    )
    assert any("appears twice" in e.message for e in diag.errors)


def test_an_entry_may_appear_in_several_sequences(tmp_path):
    """A sequence is a run somebody schedules, not a category an entry belongs to."""
    config, diag = config_with(
        tmp_path,
        INGEST
        + "sequences:\n"
        "  - { name: nightly, loads: [customers, orders] }\n"
        "  - { name: customers-only, loads: [customers] }\n",
    )
    diag.raise_if_errors()
    assert [s.name for s in config.sequences] == ["nightly", "customers-only"]


def test_an_empty_sequence_is_refused(tmp_path):
    _, diag = config_with(tmp_path, INGEST + "sequences:\n  - { name: nightly, loads: [] }\n")
    assert any("not a sequence" in e.message for e in diag.errors)


def test_two_sequences_with_one_name_are_refused(tmp_path):
    _, diag = config_with(
        tmp_path,
        INGEST
        + "sequences:\n"
        "  - { name: nightly, loads: [customers] }\n"
        "  - { name: nightly, loads: [orders] }\n",
    )
    assert any("both named 'nightly'" in e.message for e in diag.errors)


def test_an_unknown_key_in_a_sequence_is_refused(tmp_path):
    _, diag = config_with(
        tmp_path, INGEST + "sequences:\n  - { name: nightly, loads: [customers], mode: append }\n"
    )
    assert any("mode" in e.message for e in diag.errors)


# ---- the manifest ----------------------------------------------------------------


SEQ = IngestSequence(name="nightly", loads=("customers", "orders"))


def manifest(tmp_path, body: dict, name: str = "manifest.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(body))
    return path


def test_a_manifest_maps_entry_names_to_files(tmp_path):
    path = manifest(tmp_path, {"customers": "c.ndjson", "orders": "o.ndjson"})
    files = read_manifest(path, SEQ)
    assert set(files) == {"customers", "orders"}
    assert files["customers"].endswith("c.ndjson")


def test_manifest_paths_resolve_against_the_manifest_not_the_cwd(tmp_path):
    """A manifest describes a drop, and a drop is a directory of files beside it."""
    (tmp_path / "drop").mkdir()
    path = manifest(tmp_path / "drop", {"customers": "c.ndjson", "orders": "sub/o.ndjson"})
    files = read_manifest(path, SEQ)
    assert files["customers"] == str(tmp_path / "drop" / "c.ndjson")
    assert files["orders"] == str(tmp_path / "drop" / "sub" / "o.ndjson")


def test_a_manifest_missing_an_entry_the_sequence_runs_is_refused(tmp_path):
    """The failure this whole slice is against: two of three tables loaded, reported as success."""
    path = manifest(tmp_path, {"customers": "c.ndjson"})
    with pytest.raises(SequenceError) as e:
        read_manifest(path, SEQ)
    assert "orders" in str(e.value)


def test_a_manifest_naming_an_entry_the_sequence_does_not_run_is_refused(tmp_path):
    """A different mistake: a file somebody expects to land that nothing will read."""
    path = manifest(tmp_path, {"customers": "c.ndjson", "orders": "o.ndjson", "extra": "x.ndjson"})
    with pytest.raises(SequenceError, match="extra"):
        read_manifest(path, SEQ)


def test_a_manifest_that_is_not_a_mapping_is_refused(tmp_path):
    path = tmp_path / "m.yaml"
    path.write_text("- customers\n- orders\n")
    with pytest.raises(SequenceError, match="must be a mapping"):
        read_manifest(path, SEQ)


def test_a_manifest_entry_whose_value_is_not_a_path_is_refused(tmp_path):
    path = manifest(tmp_path, {"customers": "c.ndjson", "orders": 7})
    with pytest.raises(SequenceError, match="orders"):
        read_manifest(path, SEQ)


def test_a_manifest_that_is_not_there_says_so(tmp_path):
    with pytest.raises(SequenceError, match="cannot read manifest"):
        read_manifest(tmp_path / "nope.yaml", SEQ)


# ---- identity --------------------------------------------------------------------


def test_a_sequence_id_is_derived_from_the_loads_and_not_from_the_manifest(tmp_path):
    """A nightly run points the same manifest at new files every night, so hashing the manifest
    would make every night the same run."""
    assert derive_sequence_id("nightly", ["a", "b"]) == derive_sequence_id("nightly", ["a", "b"])
    assert derive_sequence_id("nightly", ["a", "b"]) != derive_sequence_id("nightly", ["a", "c"])
    assert derive_sequence_id("nightly", ["a"]) != derive_sequence_id("daily", ["a"])


# ---- running one -----------------------------------------------------------------


class FakeSequenceCatalog(FakeBulkCatalog):
    """The ingest fake plus the eighth port, and nothing else.

    Subclassed rather than folded into `FakeBulkCatalog` so that `test_ingest`'s runtime is still
    handed a catalog with *no* sequence-log verbs — which keeps that module's central absence
    assertion true: a single load cannot reach the table that says several were one run."""

    def __init__(self, *args, sequence_log_fails=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.sequence_log_fails = sequence_log_fails
        self.sequence_columns = None

    def ensure_sequence_log(self, columns):
        self.sequence_columns = tuple(columns)
        self.rows.setdefault(SEQUENCE_LOG_TABLE, [])

    def append_sequence(self, columns, row):
        if self.sequence_log_fails:
            raise CatalogError("boom: the sequence log is unreachable")
        self.sequence_columns = tuple(columns)
        self.rows.setdefault(SEQUENCE_LOG_TABLE, []).append(dict(row))

    @property
    def runs(self):
        return list(self.rows.get(SEQUENCE_LOG_TABLE, []))


CUSTOMER_ROWS = [{"customerId": "c9", "name": "Alan Turing", "tier": "bronze", "ltv": 1.0}]
ORDER_ROWS = [{"orderId": "o9", "customerId": "c9", "total": "10.00", "placedAt": "2026-01-01T00:00:00Z"}]


@pytest.fixture
def ontology():
    ont, _ = build(VALID)
    return ont


def sequencing(ontology, catalog, loads=("customers", "orders")):
    entries = [
        IngestEntry(name="customers", object_type="Customer", mode="append", format="ndjson"),
        IngestEntry(name="orders", object_type="Order", mode="append", format="ndjson"),
    ]
    return SequenceRuntime(
        ingest=IngestRuntime(
            ontology=ontology,
            catalogs={"rest_main": catalog},
            entries={e.name: e for e in entries},
        ),
        sequences={"nightly": IngestSequence(name="nightly", loads=tuple(loads))},
        catalog=catalog,
    )


def drop(tmp_path, customers=CUSTOMER_ROWS, orders=ORDER_ROWS):
    files = {
        "customers": str(ndjson(tmp_path, customers, "c.ndjson")),
        "orders": str(ndjson(tmp_path, orders, "o.ndjson")),
    }
    return manifest(tmp_path, files)


def test_every_load_runs_in_the_declared_order(ontology, tmp_path):
    catalog = FakeSequenceCatalog()
    result = sequencing(ontology, catalog).run("nightly", drop(tmp_path))

    assert result.status == APPLIED
    assert [s.entry for s in result.steps] == ["customers", "orders"]
    assert [w[1] for w in catalog.writes] == ["crm.customers", "sales.orders"]


def test_the_reversed_sequence_reverses_the_writes(ontology, tmp_path):
    catalog = FakeSequenceCatalog()
    sequencing(ontology, catalog, loads=("orders", "customers")).run("nightly", drop(tmp_path))
    assert [w[1] for w in catalog.writes] == ["sales.orders", "crm.customers"]


def test_a_refusal_stops_the_run_and_the_loads_before_it_stay_landed(ontology, tmp_path):
    """The sentence `apply` had to write first, at batch scale: an order, not a transaction."""
    catalog = FakeSequenceCatalog()
    bad = [{"orderId": "o9", "customerId": "c9", "total": "not-a-decimal", "placedAt": "2026-01-01T00:00:00Z"}]
    result = sequencing(ontology, catalog).run("nightly", drop(tmp_path, orders=bad))

    assert result.status == PARTIAL
    assert result.stopped_at == "orders"
    assert [s.entry for s in result.landed] == ["customers"]
    # And the landed load is *landed* — nothing was rolled back, because nothing could be.
    assert [w[1] for w in catalog.writes] == ["crm.customers"]
    assert len(catalog.rows["crm.customers"]) == len(CUSTOMER_ROWS) + 2


def test_a_first_load_that_refuses_leaves_the_run_refused_rather_than_partial(ontology, tmp_path):
    """`partial` is the status only a sequence can have, and it means something landed."""
    catalog = FakeSequenceCatalog()
    bad = [{"customerId": None, "name": "x", "tier": "gold", "ltv": 1.0}]
    result = sequencing(ontology, catalog).run("nightly", drop(tmp_path, customers=bad))

    assert result.status == REFUSED
    assert result.landed == []
    assert catalog.writes == []


def test_the_loads_after_the_stop_are_never_attempted(ontology, tmp_path):
    catalog = FakeSequenceCatalog()
    bad = [{"customerId": None, "name": "x", "tier": "gold", "ltv": 1.0}]
    result = sequencing(ontology, catalog).run("nightly", drop(tmp_path, customers=bad))
    assert [s.entry for s in result.steps] == ["customers"]


def test_an_unknown_sequence_name_lists_the_declared_ones(ontology, tmp_path):
    with pytest.raises(SequenceError, match="nightly"):
        sequencing(ontology, FakeSequenceCatalog()).run("weekly", drop(tmp_path))


# ---- the record ------------------------------------------------------------------


def test_a_run_writes_one_row_naming_the_loads_it_grouped(ontology, tmp_path):
    catalog = FakeSequenceCatalog()
    result = sequencing(ontology, catalog).run("nightly", drop(tmp_path))

    (row,) = catalog.runs
    assert row["sequence"] == "nightly"
    assert row["status"] == APPLIED
    assert json.loads(row["entries"]) == ["customers", "orders"]
    assert json.loads(row["loads"]) == [s.result.load_id for s in result.steps]
    assert row["landed"] == 2 and row["attempted"] == 2
    assert row["stopped_at"] is None
    assert result.recorded


def test_the_loads_still_record_themselves_individually(ontology, tmp_path):
    """One row per load in `_loom_meta.loads` and one for the run — the grouping is what is added,
    not a replacement for what M9 already recorded."""
    catalog = FakeSequenceCatalog()
    sequencing(ontology, catalog).run("nightly", drop(tmp_path))
    assert len(catalog.loads) == 2
    assert len(catalog.runs) == 1


def test_a_partial_run_records_where_it_stopped(ontology, tmp_path):
    catalog = FakeSequenceCatalog()
    bad = [{"orderId": "o9", "customerId": "c9", "total": "nope", "placedAt": "2026-01-01T00:00:00Z"}]
    sequencing(ontology, catalog).run("nightly", drop(tmp_path, orders=bad))

    (row,) = catalog.runs
    assert row["status"] == PARTIAL
    assert row["stopped_at"] == "orders"
    assert (row["landed"], row["attempted"]) == (1, 2)


def test_a_preview_records_nothing_and_writes_nothing(ontology, tmp_path):
    catalog = FakeSequenceCatalog()
    result = sequencing(ontology, catalog).run("nightly", drop(tmp_path), dry_run=True)

    assert result.status == PREVIEWED
    assert catalog.runs == []
    assert catalog.writes == []
    # An id is still derived, so a preview can say what the run would be called.
    assert result.sequence_id


def test_a_preview_that_would_stop_still_records_nothing(ontology, tmp_path):
    """The gate is the run's own `dry_run` and not its status. A preview that stops halfway reports
    `partial` — it is describing what *would* happen — so gating on the status would put a row in
    the log for a run nobody performed."""
    catalog = FakeSequenceCatalog()
    bad = [{"orderId": "o9", "customerId": "c9", "total": "nope", "placedAt": "2026-01-01T00:00:00Z"}]
    result = sequencing(ontology, catalog).run("nightly", drop(tmp_path, orders=bad), dry_run=True)

    assert result.status == PARTIAL
    assert catalog.runs == []
    assert catalog.writes == []


def test_a_failed_sequence_log_does_not_fail_the_run(ontology, tmp_path):
    """The loads are in the lake and each carries its own row. What is lost is the grouping."""
    catalog = FakeSequenceCatalog(sequence_log_fails=True)
    result = sequencing(ontology, catalog).run("nightly", drop(tmp_path))

    assert result.status == APPLIED
    assert result.recorded is False
    assert len(catalog.loads) == 2


def test_the_recorded_columns_are_the_declared_schema(ontology, tmp_path):
    from loom.ingest import SEQUENCE_COLUMNS

    catalog = FakeSequenceCatalog()
    sequencing(ontology, catalog).run("nightly", drop(tmp_path))
    assert catalog.sequence_columns == SEQUENCE_COLUMNS
    assert [c.name for c in SEQUENCE_COLUMNS if c.required] == ["sequence_id", "recorded_at"]


def test_a_catalog_with_no_sequence_log_port_is_reported_not_crashed(ontology, tmp_path):
    """`FakeBulkCatalog` implements the load log and not the sequence log, which is the deployment
    whose catalog backend can record a load and not a run."""
    catalog = FakeBulkCatalog()
    result = sequencing(ontology, catalog).run("nightly", drop(tmp_path))
    assert result.status == APPLIED
    assert result.recorded is False
