"""The ingest runtime — against a fake catalog that records what it was asked to do.

`test_action.py`'s bargain, on the bulk plane: the ports mean the whole runtime is testable with no
Iceberg stack, and what is asserted here is the **policy** — refuse a column nobody maps, carry
across the columns nobody declared, refuse the whole batch over a duplicate key, never migrate — because
that is the part a real catalog would only tell us about by ruining someone's table.
`test_ingest_iceberg.py` proves the same sequence against real pyiceberg.

The fake implements the read port, `BulkWriter` and `LoadLogWriter`, and deliberately **not**
`CatalogWriter` and not `RowWriter`. Those absences are assertions in themselves: the runtime is
handed this and works, which means it never reached for a schema verb (ingest never migrates) and
never reached for a single-row verb (a load is not a hundred actions).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.catalog.base import (
    LOAD_LOG_TABLE,
    CatalogError,
    Column,
    ConcurrencyError,
    TableSchema,
    bulk_writer_for,
    load_log_writer_for,
    row_writer_for,
    writer_for,
)
from loom.config import IngestEntry, LoomConfig
from loom.governance import EDIT_LOG_REQUIRED, INGEST_ALLOWED, INGEST_REFUSED, PolicyError
from loom.ingest import IngestError, LoadLog, build_ingest, derive_load_id
from loom.ingest.result import (
    AMBIGUOUS_KEY,
    APPLIED,
    CONFLICT,
    DEPLOYMENT_REFUSED,
    DUPLICATE_KEY,
    DUPLICATE_LOAD,
    FAILED,
    LOG_FAILED,
    MISSING_COLUMN,
    NULL_KEY,
    PREVIEWED,
    REFUSED,
    SOURCE_ERROR,
    TABLE_MISSING,
    TYPE_ERROR,
    UNMAPPED_COLUMN,
    WRITE_FAILED,
)
from loom.ingest.runtime import IngestRuntime
from loom.ontology import build

VALID = Path(__file__).parent / "fixtures" / "valid"

# The physical rows behind the `valid` fixture's Customer, including two columns no property maps.
# A merge must carry both across; an append cannot invent them and does not have to, because neither
# is required.
CUSTOMERS = [
    {"id": "c1", "full_name": "Ada Lovelace", "tier": "gold", "lifetime_value": 48210.5,
     "region": "emea", "segments": ["enterprise"]},
    {"id": "c2", "full_name": "Grace Hopper", "tier": "silver", "lifetime_value": None,
     "region": "amer", "segments": None},
]


class FakeBulkCatalog:
    """An in-memory catalog implementing the read port, `BulkWriter` and `LoadLogWriter`."""

    def __init__(self, rows=None, snapshot=1, fail_on="", log_fails=False, log_create_fails=False,
                 required_columns=()):
        self.name = "rest_main"
        self.rows: dict[str, list[dict]] = {
            "crm.customers": [dict(r) for r in (rows if rows is not None else CUSTOMERS)],
            "sales.orders": [],
        }
        self.snapshots = {t: snapshot for t in self.rows}
        self.log: list[tuple] = []
        self.fail_on = fail_on
        self.log_fails = log_fails
        self.log_create_fails = log_create_fails
        self.required_columns = tuple(required_columns)
        self.load_columns = None
        self.commits: dict[tuple, dict] = {}

    # --- read port
    def table_exists(self, table: str) -> bool:
        return table in self.rows

    def describe(self, table: str) -> TableSchema:
        sample = self.rows[table][0] if self.rows.get(table) else {}
        columns = {
            c: Column(c, "string", c in self.required_columns)
            for c in list(sample) + [c for c in self.required_columns if c not in sample]
        }
        return TableSchema(table=table, columns=columns)

    def scan(self, table, columns=None, predicates=(), limit=None):
        self.log.append(("scan", table, tuple(predicates), columns))
        return _FakeArrow(self.rows.get(table, []))

    def current_snapshot_id(self, table: str) -> int | None:
        self.log.append(("snapshot", table))
        return self.snapshots.get(table)

    # --- bulk write port
    def append_batch(self, table, rows, *, commit_properties):
        """No `expect_snapshot_id` in the signature at all, which is the port's claim made
        structural: a fake that accepted one could not show that an append asserts nothing."""
        if self.fail_on == table:
            raise CatalogError(f"boom: {table}")
        self.rows.setdefault(table, []).extend(self._filled(table, rows))
        self._bump(table, commit_properties)
        self.log.append(("append", table, len(rows)))

    def merge_batch(self, table, key_column, rows, *, expect_snapshot_id, commit_properties):
        self._guard(table, expect_snapshot_id)
        keys = {r.get(key_column) for r in rows}
        kept = [r for r in self.rows[table] if r.get(key_column) not in keys]
        self.rows[table] = [*kept, *self._filled(table, rows)]
        self._bump(table, commit_properties)
        self.log.append(("merge", table, len(rows)))

    def replace_table(self, table, rows, *, expect_snapshot_id, commit_properties):
        self._guard(table, expect_snapshot_id)
        self.rows[table] = self._filled(table, rows)
        self._bump(table, commit_properties)
        self.log.append(("replace", table, len(rows)))

    def _filled(self, table, rows):
        """Rows padded to the table's own column set, which is what `_batch` does for real.

        Not a convenience: the runtime deliberately **omits** a column the batch does not have
        rather than writing null, so that a merge carries the stored value across — and a fake that
        stored the dict as handed over would make an absent column look like a missing *result*
        instead of one the storage layer fills. The distinction it preserves is the one the merge
        correctness rests on."""
        columns = {c for row in self.rows.get(table, []) for c in row}
        return [{**{c: None for c in columns}, **dict(r)} for r in rows]

    # --- load-log port
    def ensure_load_log(self, columns):
        if self.log_create_fails:
            raise CatalogError("boom: the load log cannot be created")
        self.load_columns = tuple(columns)
        self.rows.setdefault(LOAD_LOG_TABLE, [])

    def append_load(self, columns, row):
        if self.log_fails:
            raise CatalogError("boom: the load log is unreachable")
        self.load_columns = tuple(columns)
        self.rows.setdefault(LOAD_LOG_TABLE, []).append(dict(row))

    def _guard(self, table, expect_snapshot_id):
        if self.fail_on == table:
            raise CatalogError(f"boom: {table}")
        current = self.snapshots.get(table)
        if expect_snapshot_id != current:
            raise ConcurrencyError(
                f"'{table}' moved: expected {expect_snapshot_id}, found {current}",
                table=table, expected=expect_snapshot_id, found=current,
            )

    def _bump(self, table, commit_properties=None):
        self.snapshots[table] = self.snapshots.get(table, 0) + 1
        self.commits[(table, self.snapshots[table])] = dict(commit_properties or {})

    @property
    def writes(self):
        return [e for e in self.log if e[0] in ("append", "merge", "replace")]

    @property
    def loads(self):
        return list(self.rows.get(LOAD_LOG_TABLE, []))


class _FakeArrow:
    def __init__(self, rows):
        self._rows = rows

    def to_pylist(self):
        return [dict(r) for r in self._rows]


# ---- fixtures ------------------------------------------------------------------


@pytest.fixture
def ontology():
    ont, _ = build(VALID)
    return ont


def entry(mode="append", fmt="ndjson", columns=None, name="customers"):
    return IngestEntry(
        name=name, object_type="Customer", mode=mode, format=fmt, columns=columns or {}
    )


def runtime(ontology, catalog, entries=None, posture=INGEST_ALLOWED):
    entries = entries or [entry()]
    return IngestRuntime(
        ontology=ontology,
        catalogs={"rest_main": catalog},
        entries={e.name: e for e in entries},
        posture=posture,
    )


def ndjson(tmp_path, rows, name="batch.ndjson"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


GOOD = [
    {"customerId": "c9", "name": "Alan Turing", "tier": "bronze", "ltv": 12.5},
    {"customerId": "c10", "name": "Katherine Johnson", "tier": "gold", "ltv": None},
]


# ---- the happy path ------------------------------------------------------------


def test_append_writes_every_row_and_asserts_no_snapshot(ontology, tmp_path):
    """The headline for `append`: rows land, and the port was never asked to check a snapshot.

    The second half is the one worth a test. `append_batch`'s signature has no `expect_snapshot_id`,
    so a runtime that wanted to assert one could not — and the fake would raise a TypeError if it
    tried. What this asserts positively is that the *result* says so in words rather than by showing
    a null the reader has to interpret."""
    catalog = FakeBulkCatalog()
    result = runtime(ontology, catalog).load("customers", ndjson(tmp_path, GOOD))

    assert result.status == APPLIED
    assert (result.rows_read, result.rows_written, result.rows_rejected) == (2, 2, 0)
    assert result.read_snapshot_id is None
    assert "not asserted" in result.concurrency
    assert catalog.writes == [("append", "crm.customers", 2)]
    assert [r["id"] for r in catalog.rows["crm.customers"]] == ["c1", "c2", "c9", "c10"]


def test_values_are_coerced_through_the_declared_types(ontology, tmp_path):
    """`"12.5"` for a double and `12.5` for a double are the same value, because `coerce_value` is
    the same function the read path binds a filter with. A file reader that interpreted its own
    values would be a third answer to a question this codebase has settled twice."""
    catalog = FakeBulkCatalog()
    rows = [{"customerId": "c9", "name": "Alan Turing", "tier": "bronze", "ltv": "12.5"}]
    result = runtime(ontology, catalog).load("customers", ndjson(tmp_path, rows))

    assert result.status == APPLIED
    written = catalog.rows["crm.customers"][-1]
    assert written["lifetime_value"] == 12.5
    assert isinstance(written["lifetime_value"], float)


def test_columns_map_property_names_onto_source_columns(ontology, tmp_path):
    """`columns:` is an override list, not a whitelist: `name` is not mentioned and is still loaded
    under its own name, so a spec that gains a property does not silently stop loading it."""
    catalog = FakeBulkCatalog()
    rows = [{"cust_id": "c9", "name": "Alan Turing", "tier": "bronze", "ltv": 1.0}]
    result = runtime(
        ontology, catalog, [entry(columns={"customerId": "cust_id"})]
    ).load("customers", ndjson(tmp_path, rows))

    assert result.status == APPLIED
    assert catalog.rows["crm.customers"][-1]["id"] == "c9"


# ---- the batch's columns -------------------------------------------------------


def test_a_source_column_no_property_maps_refuses_the_load(ontology, tmp_path):
    """The mirror image of §2 rule 7's unmanaged column, and the sign is flipped on purpose. There,
    a column the spec does not map is already in the lake and Loom leaves it alone. Here it is
    arriving *from* the operator, so ignoring it silently discards data they believe they are
    loading — which is what a `columns:` typo produces every single time."""
    catalog = FakeBulkCatalog()
    rows = [{**GOOD[0], "loyalty_points": 4}]
    result = runtime(ontology, catalog).load("customers", ndjson(tmp_path, rows))

    assert result.status == REFUSED
    assert [f.code for f in result.failures] == [UNMAPPED_COLUMN]
    assert "loyalty_points" in result.failures[0].message
    assert catalog.writes == []
    assert result.rows_written == 0


def test_a_missing_column_for_a_non_nullable_property_refuses(ontology, tmp_path):
    """The message names the column as the **source** spells it, not as the table does. `name` maps
    to the physical column `full_name`, and an operator staring at their own file needs the word
    that is missing from it — `full_name` is a fact about the lake and no help here."""
    catalog = FakeBulkCatalog()
    rows = [{"customerId": "c9", "tier": "bronze", "ltv": 1.0}]  # no `name`
    result = runtime(ontology, catalog).load("customers", ndjson(tmp_path, rows))

    assert result.status == REFUSED
    assert [f.code for f in result.failures] == [MISSING_COLUMN]
    assert "no column 'name'" in result.failures[0].message
    assert result.failures[0].detail == {"property": "name", "column": "name"}
    assert catalog.writes == []


def test_a_missing_column_for_a_nullable_property_is_fine(ontology, tmp_path):
    """`ltv` is the fixture's one nullable property. Absent means null, which is what nullable
    means."""
    catalog = FakeBulkCatalog()
    rows = [{"customerId": "c9", "name": "Alan Turing", "tier": "bronze"}]
    result = runtime(ontology, catalog).load("customers", ndjson(tmp_path, rows))

    assert result.status == APPLIED
    assert catalog.rows["crm.customers"][-1]["lifetime_value"] is None


def test_a_missing_primary_key_column_refuses_naming_the_key(ontology, tmp_path):
    catalog = FakeBulkCatalog()
    rows = [{"name": "Alan Turing", "tier": "bronze", "ltv": 1.0}]
    result = runtime(ontology, catalog).load("customers", ndjson(tmp_path, rows))

    assert result.status == REFUSED
    assert result.failures[0].code == MISSING_COLUMN
    assert "primary key" in result.failures[0].message


# ---- ingest never migrates -----------------------------------------------------


def test_a_missing_table_refuses_and_points_at_apply(ontology, tmp_path):
    """The no-DDL rule, made visible to an operator. The `BulkWriter` port has no verb that could
    create this table, so the only useful thing to say is which command does."""
    catalog = FakeBulkCatalog()
    del catalog.rows["crm.customers"]
    result = runtime(ontology, catalog).load("customers", ndjson(tmp_path, GOOD))

    assert result.status == REFUSED
    assert result.failures[0].code == TABLE_MISSING
    assert "never creates or alters a table" in result.failures[0].message
    assert "loom apply" in result.failures[0].detail["hint"]


def test_the_runtime_holds_no_schema_or_single_row_port(ontology, tmp_path):
    """The two absences the fake exists to demonstrate. A real catalog implements every port at
    once and so can never show which one a caller used."""
    catalog = FakeBulkCatalog()
    assert runtime(ontology, catalog).load("customers", ndjson(tmp_path, GOOD)).status == APPLIED

    assert bulk_writer_for(catalog) is catalog
    assert load_log_writer_for(catalog) is catalog
    with pytest.raises(CatalogError, match="schema writes"):
        writer_for(catalog)
    with pytest.raises(CatalogError, match="row writes"):
        row_writer_for(catalog)


def test_a_required_physical_column_no_property_maps_refuses_an_append(ontology, tmp_path):
    """An append writes whole rows from the batch, so a required column nothing fills can only ever
    fail — and it fails here, naming the column, rather than three layers down inside pyiceberg."""
    catalog = FakeBulkCatalog(required_columns=("region",))
    result = runtime(ontology, catalog).load("customers", ndjson(tmp_path, GOOD))

    assert result.status == REFUSED
    assert result.failures[0].code == MISSING_COLUMN
    assert "region" in result.failures[0].message
    assert "mode: merge" in result.failures[0].detail["hint"]


def test_the_same_required_column_is_fine_for_a_merge(ontology, tmp_path):
    """Because a merge reads the row that is already there and carries it across. The check exists
    for the two modes that write whole rows from the batch, and knows the difference."""
    catalog = FakeBulkCatalog(required_columns=("region",))
    rows = [{"customerId": "c1", "name": "Ada L.", "tier": "gold", "ltv": 1.0}]
    result = runtime(ontology, catalog, [entry(mode="merge")]).load(
        "customers", ndjson(tmp_path, rows)
    )

    assert result.status == APPLIED


# ---- the rows ------------------------------------------------------------------


def test_a_value_that_will_not_coerce_refuses_the_whole_batch(ontology, tmp_path):
    """Whole-batch refusal is the default, and `rows_written == 0` is the promise: a partial load
    leaves the lake in a state nobody declared."""
    catalog = FakeBulkCatalog()
    rows = [GOOD[0], {"customerId": "c10", "name": "K J", "tier": "platinum", "ltv": 1.0}]
    result = runtime(ontology, catalog).load("customers", ndjson(tmp_path, rows))

    assert result.status == REFUSED
    assert [f.code for f in result.failures] == [TYPE_ERROR]
    assert "platinum" in result.failures[0].message
    assert catalog.writes == []
    assert result.rows_written == 0


def test_reject_to_quarantines_the_bad_rows_and_loads_the_rest(ontology, tmp_path):
    """The escape hatch, and the three counts that make it honest: `rows_read` is what the file
    held, and `written + rejected` accounts for all of it. Without the third number an operator
    would have to notice a subtraction at 3am."""
    catalog = FakeBulkCatalog()
    rows = [GOOD[0], {"customerId": "c10", "name": "K J", "tier": "platinum", "ltv": 1.0}]
    rejects = tmp_path / "rejects.ndjson"
    result = runtime(ontology, catalog).load(
        "customers", ndjson(tmp_path, rows), reject_to=rejects
    )

    assert result.status == APPLIED
    assert (result.rows_read, result.rows_written, result.rows_rejected) == (2, 1, 1)
    assert result.rows_read == result.rows_written + result.rows_rejected
    quarantined = [json.loads(line) for line in rejects.read_text().splitlines()]
    assert len(quarantined) == 1
    assert quarantined[0]["customerId"] == "c10"
    assert "platinum" in quarantined[0]["_loom_rejected"][0]
    assert [r["id"] for r in catalog.rows["crm.customers"]] == ["c1", "c2", "c9"]


def test_a_null_primary_key_is_refused_in_every_mode(ontology, tmp_path):
    """A null key names an object no surface can address — M7 refused `{"prop": null}` as a filter
    permanently, so a row loaded under one could be described and never retrieved."""
    catalog = FakeBulkCatalog()
    rows = [{"customerId": None, "name": "Nobody", "tier": "bronze", "ltv": 1.0}]
    result = runtime(ontology, catalog).load("customers", ndjson(tmp_path, rows))

    assert result.status == REFUSED
    assert [f.code for f in result.failures] == [NULL_KEY]
    assert catalog.writes == []


def test_two_rows_with_one_key_refuse_the_batch_even_with_reject_to(ontology, tmp_path):
    """`--reject-to` absorbs rows, never a batch. Choosing which of two rows sharing a key survives
    is a decision the source does not contain, and Loom will not invent one — so this is the
    refusal that survives the escape hatch."""
    catalog = FakeBulkCatalog()
    rows = [GOOD[0], {**GOOD[0], "name": "Someone Else"}]
    result = runtime(ontology, catalog).load(
        "customers", ndjson(tmp_path, rows), reject_to=tmp_path / "r.ndjson"
    )

    assert result.status == REFUSED
    assert [f.code for f in result.failures] == [DUPLICATE_KEY]
    assert "c9" in result.failures[0].message
    assert catalog.writes == []


# ---- merge ---------------------------------------------------------------------


def test_merge_carries_across_every_column_the_ontology_does_not_map(ontology, tmp_path):
    """§4.1's rule at batch scale, and the reason `merge` is a mode rather than a flag: a merge is
    an equality-delete plus an append, so `region` and `segments` are carried or they are nulled."""
    catalog = FakeBulkCatalog()
    rows = [{"customerId": "c1", "name": "Ada Lovelace", "tier": "silver", "ltv": 50000.0}]
    result = runtime(ontology, catalog, [entry(mode="merge")]).load(
        "customers", ndjson(tmp_path, rows)
    )

    assert result.status == APPLIED
    merged = next(r for r in catalog.rows["crm.customers"] if r["id"] == "c1")
    assert merged["tier"] == "silver"          # the batch's value won
    assert merged["region"] == "emea"          # carried across, untouched
    assert merged["segments"] == ["enterprise"]
    assert len(catalog.rows["crm.customers"]) == 2  # replaced, not appended


def test_merge_inserts_a_key_that_is_not_there_yet(ontology, tmp_path):
    catalog = FakeBulkCatalog()
    result = runtime(ontology, catalog, [entry(mode="merge")]).load(
        "customers", ndjson(tmp_path, GOOD)
    )

    assert result.status == APPLIED
    assert {r["id"] for r in catalog.rows["crm.customers"]} == {"c1", "c2", "c9", "c10"}


def test_merge_asserts_the_snapshot_the_read_saw(ontology, tmp_path):
    catalog = FakeBulkCatalog(snapshot=7)
    result = runtime(ontology, catalog, [entry(mode="merge")]).load(
        "customers", ndjson(tmp_path, GOOD)
    )

    assert result.read_snapshot_id == 7
    assert "enforced" in result.concurrency


def test_a_key_matching_two_existing_rows_refuses_the_merge(ontology, tmp_path):
    """`ambiguous_key`, and it matters more here than on the read path: an equality-delete over a
    duplicated key removes both rows and appends one. Loom cannot repair the table — it can only
    decline to make it worse."""
    catalog = FakeBulkCatalog(rows=[*CUSTOMERS, dict(CUSTOMERS[0])])
    rows = [{"customerId": "c1", "name": "Ada L.", "tier": "gold", "ltv": 1.0}]
    result = runtime(ontology, catalog, [entry(mode="merge")]).load(
        "customers", ndjson(tmp_path, rows)
    )

    assert result.status == REFUSED
    assert [f.code for f in result.failures] == [AMBIGUOUS_KEY]
    assert catalog.writes == []


def test_a_conflict_refuses_and_writes_nothing(ontology, tmp_path):
    """`refused`, not `failed`: the write was declined before it committed, so this is a load that
    changed nothing — which is what lets an operator re-run it without first working out what
    landed. And it is not retried in-process: rebuilding a batch silently is an unbounded amount of
    work nobody asked for."""
    catalog = FakeBulkCatalog(snapshot=3)

    class Moving(type(catalog)):
        pass

    original = catalog.current_snapshot_id

    def moving(table):
        value = original(table)
        catalog.snapshots[table] = catalog.snapshots.get(table, 0) + 1
        return value

    catalog.current_snapshot_id = moving
    result = runtime(ontology, catalog, [entry(mode="merge")]).load(
        "customers", ndjson(tmp_path, GOOD)
    )

    assert result.status == REFUSED
    assert result.retryable
    conflict = next(f for f in result.failures if f.code == CONFLICT)
    assert conflict.detail["expectedSnapshotId"] != conflict.detail["foundSnapshotId"]
    assert catalog.writes == []


# ---- replace -------------------------------------------------------------------


def test_replace_makes_the_table_exactly_the_batch(ontology, tmp_path):
    catalog = FakeBulkCatalog()
    result = runtime(ontology, catalog, [entry(mode="replace")]).load(
        "customers", ndjson(tmp_path, GOOD)
    )

    assert result.status == APPLIED
    assert [r["id"] for r in catalog.rows["crm.customers"]] == ["c9", "c10"]


def test_replace_asserts_the_snapshot_so_it_cannot_destroy_a_write_nobody_saw(ontology, tmp_path):
    """A replace reads nothing and destroys everything, which sounds like the append case and is
    its opposite. What it must not do is overwrite a commit that landed since the operator decided
    the table's whole contents were this batch."""
    catalog = FakeBulkCatalog(snapshot=4)
    result = runtime(ontology, catalog, [entry(mode="replace")]).load(
        "customers", ndjson(tmp_path, GOOD)
    )
    assert result.read_snapshot_id == 4
    assert "enforced" in result.concurrency


# ---- identity and retries ------------------------------------------------------


def test_the_same_file_through_the_same_entry_is_refused_the_second_time(ontology, tmp_path):
    """The retry guard, and the whole reason a load has an id. A pipeline that times out and
    re-runs hands Loom the same bytes; answering *this is one load happening twice* is what stops an
    append silently doubling."""
    catalog = FakeBulkCatalog()
    rt = runtime(ontology, catalog)
    source = ndjson(tmp_path, GOOD)

    first = rt.load("customers", source)
    second = rt.load("customers", source)

    assert first.status == APPLIED
    assert second.status == REFUSED
    assert [f.code for f in second.failures] == [DUPLICATE_LOAD]
    assert second.failures[0].detail["loadId"] == first.load_id
    assert len(catalog.rows["crm.customers"]) == 4  # not 6


def test_an_explicit_load_id_is_how_you_say_this_file_again_on_purpose(ontology, tmp_path):
    catalog = FakeBulkCatalog()
    rt = runtime(ontology, catalog)
    source = ndjson(tmp_path, GOOD)

    assert rt.load("customers", source).status == APPLIED
    again = rt.load("customers", source, load_id="deliberate-second-run")

    assert again.status == APPLIED
    assert len(catalog.rows["crm.customers"]) == 6


def test_the_derived_id_is_a_function_of_entry_mode_and_bytes(ontology, tmp_path):
    """Stated as an equality rather than left implicit, because three things are in the hash and
    each has a reason: the same file appended and then merged are two different loads, and the same
    bytes through two entries are two loads."""
    assert derive_load_id("a", "append", "ff") == derive_load_id("a", "append", "ff")
    assert derive_load_id("a", "append", "ff") != derive_load_id("a", "merge", "ff")
    assert derive_load_id("a", "append", "ff") != derive_load_id("b", "append", "ff")
    assert derive_load_id("a", "append", "ff") != derive_load_id("a", "append", "ee")


def test_a_refused_load_does_not_block_the_corrected_one(ontology, tmp_path):
    """`landed()` looks for `applied` or `failed`, never `refused`. A refusal wrote nothing, so
    re-running its id is the retry the operator was supposed to make after fixing the file —
    refusing it would make the first typo permanent."""
    catalog = FakeBulkCatalog()
    rt = runtime(ontology, catalog)
    bad = ndjson(tmp_path, [{**GOOD[0], "tier": "platinum"}], name="bad.ndjson")

    assert rt.load("customers", bad).status == REFUSED
    assert rt.load("customers", bad, load_id="same-id-again").status == REFUSED
    good = ndjson(tmp_path, GOOD, name="good.ndjson")
    assert rt.load("customers", good).status == APPLIED


# ---- the record ----------------------------------------------------------------


def test_one_row_per_load_not_per_row_loaded(ontology, tmp_path):
    catalog = FakeBulkCatalog()
    result = runtime(ontology, catalog).load("customers", ndjson(tmp_path, GOOD))

    assert len(catalog.loads) == 1
    record = catalog.loads[0]
    assert record["load_id"] == result.load_id
    assert record["entry"] == "customers"
    assert record["mode"] == "append"
    assert record["status"] == APPLIED
    assert (record["rows_read"], record["rows_written"], record["rows_rejected"]) == (2, 2, 0)
    assert record["table_name"] == "crm.customers"
    assert record["source_fingerprint"]


def test_the_record_carries_a_fingerprint_and_never_the_batch(ontology, tmp_path):
    """The difference from `EditRecord`, stated as a test. An action's record carries `before` and
    `after` because a handful of values fit in a row; a load's answer is the batch, and copying it
    would make this table an unabridged second copy of somebody's nightly drop."""
    catalog = FakeBulkCatalog()
    runtime(ontology, catalog).load("customers", ndjson(tmp_path, GOOD))

    record = catalog.loads[0]
    assert "before" not in record and "after" not in record
    assert "Alan Turing" not in json.dumps(record, default=str)


def test_a_refusal_that_named_itself_is_recorded(ontology, tmp_path):
    """*Who tried to replace this table* is as much an audit question as *who tried to delete this
    customer*, which is the edit log's own argument for recording refusals."""
    catalog = FakeBulkCatalog()
    rows = [{**GOOD[0], "loyalty_points": 4}]
    result = runtime(ontology, catalog).load("customers", ndjson(tmp_path, rows))

    assert result.status == REFUSED
    assert len(catalog.loads) == 1
    assert catalog.loads[0]["status"] == REFUSED
    assert catalog.loads[0]["rows_written"] == 0
    assert UNMAPPED_COLUMN in catalog.loads[0]["failures"]


def test_a_refusal_that_never_named_itself_is_not_recorded(ontology, tmp_path):
    """The gate is the load id — `run.addressed`'s counterpart. A file that cannot be read produces
    no batch, no fingerprint and no id, so the record would carry an empty key and cite nothing."""
    catalog = FakeBulkCatalog()
    result = runtime(ontology, catalog).load("customers", tmp_path / "nope.ndjson")

    assert result.status == REFUSED
    assert [f.code for f in result.failures] == [SOURCE_ERROR]
    assert catalog.loads == []


def test_a_preview_writes_nothing_and_records_nothing(ontology, tmp_path):
    """`loom ingest` previews before every real load, so recording previews would put two rows in
    the log for every one load."""
    catalog = FakeBulkCatalog()
    result = runtime(ontology, catalog).load("customers", ndjson(tmp_path, GOOD), dry_run=True)

    assert result.status == PREVIEWED
    assert result.rows_written == 2  # what *would* be written
    assert catalog.writes == []
    assert catalog.loads == []
    assert "not checked" in result.concurrency


def test_a_failed_log_does_not_fail_the_load(ontology, tmp_path):
    """By the time the record is written the rows have committed, and reporting `failed` would tell
    an operator to re-run a load that already landed — which, for an append, doubles it."""
    catalog = FakeBulkCatalog(log_fails=True)
    result = runtime(ontology, catalog).load("customers", ndjson(tmp_path, GOOD))

    assert result.status == APPLIED
    assert result.ok
    assert [f.code for f in result.failures] == [LOG_FAILED]
    assert not result.retryable
    assert len(catalog.rows["crm.customers"]) == 4


def test_a_write_that_fails_comes_back_as_failed_and_is_recorded(ontology, tmp_path):
    catalog = FakeBulkCatalog(fail_on="crm.customers")
    result = runtime(ontology, catalog).load("customers", ndjson(tmp_path, GOOD))

    assert result.status == FAILED
    assert [f.code for f in result.failures] == [WRITE_FAILED]
    assert catalog.loads[0]["status"] == FAILED


def test_the_commit_carries_the_load_id(ontology, tmp_path):
    """The only attribution that is atomic with the write. Everything else, the log included, is a
    second commit a crash can land on the wrong side of."""
    catalog = FakeBulkCatalog()
    result = runtime(ontology, catalog).load("customers", ndjson(tmp_path, GOOD), actor="ci")

    stamp = catalog.commits[("crm.customers", catalog.snapshots["crm.customers"])]
    assert stamp == {
        "loom.load_id": result.load_id,
        "loom.ingest": "customers",
        "loom.actor": "ci",
    }


def test_an_actor_is_never_invented(ontology, tmp_path):
    catalog = FakeBulkCatalog()
    runtime(ontology, catalog).load("customers", ndjson(tmp_path, GOOD))
    assert catalog.loads[0]["actor"] == "unknown"


# ---- the posture ---------------------------------------------------------------


def test_a_refused_deployment_does_not_load_and_does_not_read_the_file(ontology, tmp_path):
    """Default-refused is `mcp.writes`' posture: an upgrade that shipped ingest and a config that
    happens to describe a load must not multiply into a lake that quietly gained a way to be
    overwritten. And the file is not opened — a deployment that does not load has no business
    reading somebody's data to say so."""
    catalog = FakeBulkCatalog()
    result = runtime(ontology, catalog, posture=INGEST_REFUSED).load(
        "customers", tmp_path / "does-not-exist.ndjson"
    )

    assert result.status == REFUSED
    assert [f.code for f in result.failures] == [DEPLOYMENT_REFUSED]
    assert catalog.writes == []
    assert catalog.loads == []  # and no `_loom_meta.loads` conjured in a deployment that refuses


def test_an_unknown_entry_raises_rather_than_refusing(ontology, tmp_path):
    """A `Failure` is a load that ran and refused. Asking after an entry nobody declared never
    became a load at all."""
    catalog = FakeBulkCatalog()
    with pytest.raises(IngestError, match="unknown ingest entry 'nope'"):
        runtime(ontology, catalog).load("nope", ndjson(tmp_path, GOOD))


# ---- build_ingest --------------------------------------------------------------


def config(entries=(), posture=INGEST_ALLOWED, edit_log="optional"):
    return LoomConfig(ingest=tuple(entries), ingest_posture=posture, edit_log=edit_log)


def test_build_resolves_entries_against_the_ontology(ontology):
    catalog = FakeBulkCatalog()
    rt = build_ingest(ontology, config([entry()]), {"rest_main": catalog})
    assert set(rt.entries) == {"customers"}


def test_build_refuses_an_entry_naming_an_object_type_that_is_not_there(ontology):
    bad = IngestEntry(name="x", object_type="Widget", mode="append", format="csv")
    with pytest.raises(IngestError, match="which the ontology does not declare"):
        build_ingest(ontology, config([bad]), {"rest_main": FakeBulkCatalog()})


def test_build_refuses_a_columns_entry_naming_a_property_that_is_not_there(ontology):
    bad = IngestEntry(
        name="x", object_type="Customer", mode="append", format="csv", columns={"nope": "n"}
    )
    with pytest.raises(IngestError, match="which Customer does not declare"):
        build_ingest(ontology, config([bad]), {"rest_main": FakeBulkCatalog()})


def test_build_accumulates_every_problem(ontology):
    entries = [
        IngestEntry(name="a", object_type="Widget", mode="append", format="csv"),
        IngestEntry(name="b", object_type="Customer", mode="append", format="csv",
                    columns={"nope": "n"}),
    ]
    with pytest.raises(IngestError) as excinfo:
        build_ingest(ontology, config(entries), {"rest_main": FakeBulkCatalog()})
    assert "Widget" in str(excinfo.value) and "nope" in str(excinfo.value)


def test_entries_are_resolved_even_when_the_posture_refuses_them(ontology):
    """A deployment that declares a load it will not perform is in the same legitimate state a spec
    declaring actions is in under `mcp.writes: false`, and a typo is worth reporting either way."""
    bad = IngestEntry(name="x", object_type="Widget", mode="append", format="csv")
    with pytest.raises(IngestError):
        build_ingest(ontology, config([bad], posture=INGEST_REFUSED), {"rest_main": FakeBulkCatalog()})


def test_edit_log_required_proves_the_load_log_too(ontology):
    """The posture's own words are *a deployment that cannot log does not run*, and it was written
    when the only thing Loom could write was one row. A deployment that demanded it and then
    bulk-loaded unrecorded would satisfy the letter of a check while contradicting the sentence."""
    catalog = FakeBulkCatalog(log_create_fails=True)
    with pytest.raises(PolicyError, match="cannot record what it loads"):
        build_ingest(
            ontology,
            config([entry()], edit_log=EDIT_LOG_REQUIRED),
            {"rest_main": catalog},
        )


def test_edit_log_required_creates_the_load_log_rather_than_probing(ontology):
    catalog = FakeBulkCatalog()
    build_ingest(ontology, config([entry()], edit_log=EDIT_LOG_REQUIRED), {"rest_main": catalog})
    assert catalog.table_exists(LOAD_LOG_TABLE)
    assert catalog.rows[LOAD_LOG_TABLE] == []  # a permission, not a table of intentions


def test_a_refused_deployment_is_not_asked_to_prove_a_log_it_will_never_write(ontology):
    """Otherwise `governance.ingest: refused` would create `_loom_meta.loads` in every catalog of
    every deployment that declared an entry and meant it for later."""
    catalog = FakeBulkCatalog()
    build_ingest(
        ontology,
        config([entry()], posture=INGEST_REFUSED, edit_log=EDIT_LOG_REQUIRED),
        {"rest_main": catalog},
    )
    assert not catalog.table_exists(LOAD_LOG_TABLE)


def test_the_load_log_history_is_ordered(ontology, tmp_path):
    catalog = FakeBulkCatalog()
    rt = runtime(ontology, catalog)
    rt.load("customers", ndjson(tmp_path, GOOD, name="a.ndjson"))
    rt.load("customers", ndjson(tmp_path, [GOOD[0]], name="b.ndjson"))

    history = LoadLog(catalog=catalog).history()
    assert len(history) == 2
    assert [r["status"] for r in history] == [APPLIED, APPLIED]


# ---- the source ----------------------------------------------------------------


def test_ndjson_reports_the_line_it_could_not_parse(ontology, tmp_path):
    """A file that is *almost* NDJSON is the case where a silent skip loses rows an operator
    believed they had loaded, so the line number is in the message."""
    path = tmp_path / "bad.ndjson"
    path.write_text(json.dumps(GOOD[0]) + "\n{nope\n")
    result = runtime(ontology, FakeBulkCatalog()).load("customers", path)

    assert result.status == REFUSED
    assert [f.code for f in result.failures] == [SOURCE_ERROR]
    assert "line 2" in result.failures[0].message


def test_ndjson_skips_blank_lines_but_not_json_that_is_not_an_object(ontology, tmp_path):
    path = tmp_path / "mixed.ndjson"
    path.write_text(json.dumps(GOOD[0]) + "\n\n[1, 2]\n")
    result = runtime(ontology, FakeBulkCatalog()).load("customers", path)

    assert [f.code for f in result.failures] == [SOURCE_ERROR]
    assert "not a JSON object" in result.failures[0].message


def test_a_csv_without_a_header_cannot_have_its_columns_named(ontology, tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("")
    result = runtime(ontology, FakeBulkCatalog(), [entry(fmt="csv")]).load("customers", path)

    assert [f.code for f in result.failures] == [SOURCE_ERROR]
    assert "no header row" in result.failures[0].message


def test_a_header_only_csv_is_a_batch_of_zero_rows(ontology, tmp_path):
    """The other half of the zero-byte rule: a source that means *these columns, and no rows* has
    to be able to say so, and a CSV header can. Under `mode: replace` this is how a
    materialization whose source went empty says it went empty — and a truncated upload, which is
    zero bytes and no header, cannot say the same thing by accident."""
    path = tmp_path / "header.csv"
    path.write_text("customerId,name,tier,ltv\n")
    catalog = FakeBulkCatalog()
    result = runtime(ontology, catalog, [entry(mode="replace", fmt="csv")]).load("customers", path)

    assert result.status == APPLIED
    assert result.rows_read == 0
    assert catalog.rows["crm.customers"] == []


def test_an_empty_ndjson_declares_no_columns_and_is_refused(ontology, tmp_path):
    path = tmp_path / "empty.ndjson"
    path.write_text("")
    catalog = FakeBulkCatalog()
    result = runtime(ontology, catalog, [entry(mode="replace")]).load("customers", path)

    assert result.status == REFUSED
    assert {f.code for f in result.failures} == {MISSING_COLUMN}
    assert len(catalog.rows["crm.customers"]) == 2  # nothing was wiped


def test_csv_values_are_strings_and_coerce_like_every_other_path(ontology, tmp_path):
    """Strings all the way through is the point rather than a limitation: letting a CSV reader
    guess types would introduce a second, worse type system — one that decides `007` is an integer
    on evidence the spec already answered."""
    path = tmp_path / "b.csv"
    path.write_text("customerId,name,tier,ltv\nc9,Alan Turing,bronze,12.5\n")
    catalog = FakeBulkCatalog()
    result = runtime(ontology, catalog, [entry(fmt="csv")]).load("customers", path)

    assert result.status == APPLIED
    assert catalog.rows["crm.customers"][-1]["lifetime_value"] == 12.5


def test_a_refused_load_reports_no_rejected_rows_however_many_were_bad(ontology, tmp_path):
    """`rows_rejected` counts rows that were *quarantined*, not rows that were unacceptable. A
    refused load set nothing aside — the whole batch was declined — and reporting otherwise would
    describe a partial load that did not happen, and would break
    `rows_read == rows_written + rows_rejected` on the results an operator reads most carefully."""
    catalog = FakeBulkCatalog()
    rows = [GOOD[0], {"customerId": "c10", "name": "K J", "tier": "platinum", "ltv": 1.0}]
    result = runtime(ontology, catalog).load("customers", ndjson(tmp_path, rows))

    assert result.status == REFUSED
    assert (result.rows_read, result.rows_written, result.rows_rejected) == (2, 0, 0)
    assert [f.code for f in result.failures] == [TYPE_ERROR]  # the bad row is still named
    assert catalog.loads[0]["rows_rejected"] == 0


# ---- the six the review found ---------------------------------------------------


def test_merge_does_not_null_a_mapped_property_whose_column_the_batch_omits(ontology, tmp_path):
    """The inverse of the mode's promise, and the sharpest bug in the slice.

    `merge` exists to carry values across. Writing an absent column as null carried the *unmapped*
    columns faithfully while destroying a **mapped** one — so the property the ontology declares was
    the only thing a merge could lose, which is exactly backwards. A column the batch does not have
    is now left out of the row rather than written as null, and `_carry_across` therefore has
    something to carry."""
    catalog = FakeBulkCatalog()
    rows = [{"customerId": "c1", "name": "Ada Lovelace", "tier": "silver"}]  # no `ltv`
    result = runtime(ontology, catalog, [entry(mode="merge")]).load(
        "customers", ndjson(tmp_path, rows)
    )

    assert result.status == APPLIED
    merged = next(r for r in catalog.rows["crm.customers"] if r["id"] == "c1")
    assert merged["tier"] == "silver"            # the batch won where it spoke
    assert merged["lifetime_value"] == 48210.5   # ...and did not where it was silent
    assert merged["region"] == "emea"


def test_a_preview_records_nothing_even_when_it_refuses(ontology, tmp_path):
    """`ActionRuntime.run` returns before `_record` on a dry run, and this matches it. A preview
    that recorded would create `_loom_meta.loads` in a catalog whose operator was asking a
    question."""
    catalog = FakeBulkCatalog()
    rows = [{**GOOD[0], "loyalty_points": 4}]
    result = runtime(ontology, catalog).load("customers", ndjson(tmp_path, rows), dry_run=True)

    assert result.status == REFUSED
    assert catalog.loads == []
    assert not catalog.table_exists(LOAD_LOG_TABLE)


def test_two_properties_reading_one_column_by_default_is_refused_at_build(ontology):
    """Config validation compares only *declared* mappings, and cannot see this: the effective
    mapping is `columns` laid over the identity, so mapping `name` onto `tier` leaves `tier` reading
    its own column too. One source value into two physical columns, with nothing raising."""
    aliased = IngestEntry(
        name="x", object_type="Customer", mode="append", format="ndjson",
        columns={"name": "tier"},
    )
    with pytest.raises(IngestError, match="both read source column 'tier'"):
        build_ingest(ontology, config([aliased]), {"rest_main": FakeBulkCatalog()})


def test_a_load_refused_after_quarantine_reports_no_rejected_rows(ontology, tmp_path):
    """The quarantine file is written after every check that can still refuse, so the count and the
    file agree. A conflict can still leave a file behind a refused load — which is correct, since it
    is the input to the next attempt — and the *count* stays zero, because nothing was set aside
    from a batch that was declined whole."""
    catalog = FakeBulkCatalog(rows=[*CUSTOMERS, dict(CUSTOMERS[0])])  # c1 twice → ambiguous
    rows = [
        {"customerId": "c1", "name": "Ada L.", "tier": "gold", "ltv": 1.0},
        {"customerId": "c10", "name": "K J", "tier": "platinum", "ltv": 1.0},  # bad enum
    ]
    rejects = tmp_path / "r.ndjson"
    result = runtime(ontology, catalog, [entry(mode="merge")]).load(
        "customers", ndjson(tmp_path, rows), reject_to=rejects
    )

    assert result.status == REFUSED
    assert [f.code for f in result.failures] == [TYPE_ERROR, AMBIGUOUS_KEY]
    assert result.rows_rejected == 0
    assert not rejects.exists()  # refused before the file was written
    assert catalog.writes == []


def test_a_refusal_message_carries_no_python_internals(ontology, tmp_path):
    """`decimal.InvalidOperation` stringifies to its own signal list — `[<class
    'decimal.ConversionSyntax'>]` — and that repr was reaching the operator, the `failures` column of
    `_loom_meta.loads`, and the JSON a caller reads. `_as_decimal` guarded against an *empty* message
    and the signal list is truthy, so the guard never fired.

    Asserted as an absence of `<class` rather than as an exact string, because the point is the
    class of leak and not this one type's wording."""
    catalog = FakeBulkCatalog()
    orders = IngestEntry(name="orders", object_type="Order", mode="append", format="ndjson")
    rows = [{"orderId": "o9", "customerId": "c9", "total": "12,50",
             "placedAt": "2026-01-01T00:00:00Z"}]
    result = runtime(ontology, catalog, entries=[orders]).load("orders", ndjson(tmp_path, rows))

    assert result.status == REFUSED
    message = result.failures[0].message
    assert "<class" not in message and "ConversionSyntax" not in message
    assert "cannot read '12,50' as decimal (not a number)" in message
    assert "<class" not in json.dumps(result.as_json())
