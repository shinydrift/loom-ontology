"""`loom infer` — the draft, and the two things that make it a draft.

The interesting assertions here are not the type table. They are the pair that keeps a scaffold from
becoming a schema authority: a draft with its placeholders left in **does not validate**, and the
same draft with them filled in **does** — including its `ingest:` entry, parsed by the real config
parser rather than eyeballed. A generator whose output only looks like a spec is a generator that
produces work rather than saving it.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from loom.config import find_config, load_config
from loom.errors import Diagnostics, SpecErrors
from loom.infer import (
    TODO_CATALOG,
    TODO_MODE,
    TODO_PRIMARY_KEY,
    TODO_TABLE,
    InferError,
    infer_draft,
    read_columns,
    render_draft,
)
from loom.ontology import build


def write(path, schema, rows=None):
    pq.write_table(pa.table(rows or {n: [] for n in schema.names}, schema=schema), path)
    return path


def customers(tmp_path):
    """The retail example's `crm.customers`, as a file — unmanaged columns and all."""
    schema = pa.schema(
        [
            pa.field("id", pa.string(), nullable=False),
            pa.field("full_name", pa.string(), nullable=False),
            pa.field("tier", pa.string(), nullable=False),
            pa.field("lifetime_value", pa.float64(), nullable=True),
            pa.field("region", pa.string(), nullable=True),
            pa.field("segments", pa.list_(pa.string()), nullable=True),
        ]
    )
    return write(tmp_path / "customers.parquet", schema)


# ---- the type table --------------------------------------------------------------


@pytest.mark.parametrize(
    "arrow,expected",
    [
        (pa.string(), {"type": "string"}),
        (pa.large_string(), {"type": "string"}),
        (pa.bool_(), {"type": "boolean"}),
        (pa.int32(), {"type": "int"}),
        (pa.int64(), {"type": "long"}),
        (pa.float32(), {"type": "double"}),
        (pa.float64(), {"type": "double"}),
        (pa.date32(), {"type": "date"}),
        (pa.timestamp("us", tz="UTC"), {"type": "timestamp"}),
        (pa.decimal128(14, 2), {"type": "decimal", "precision": 14, "scale": 2}),
    ],
)
def test_a_declared_parquet_type_becomes_the_spec_type_for_it(tmp_path, arrow, expected):
    path = write(tmp_path / "t.parquet", pa.schema([pa.field("c", arrow)]))
    assert read_columns(path)[0].spec == expected


def test_decimal_precision_and_scale_survive_the_trip(tmp_path):
    """The one mapping worth its own test: it is the reason CSV is refused."""
    path = write(tmp_path / "t.parquet", pa.schema([pa.field("total_amount", pa.decimal128(12, 2))]))
    assert read_columns(path)[0].spec == {"type": "decimal", "precision": 12, "scale": 2}


def test_a_tz_naive_timestamp_is_refused_rather_than_called_a_timestamp(tmp_path):
    """Loom's `timestamp` is an Iceberg `timestamptz`, so mapping this one would draft a spec that
    passes `loom validate` and fails `loom validate --physical`."""
    path = write(tmp_path / "t.parquet", pa.schema([pa.field("at", pa.timestamp("us"))]))
    column = read_columns(path)[0]
    assert column.spec is None
    assert "timestamptz" in column.refusal


def test_a_type_the_spec_defers_is_named_along_with_the_section_that_defers_it(tmp_path):
    path = write(tmp_path / "t.parquet", pa.schema([pa.field("segments", pa.list_(pa.string()))]))
    column = read_columns(path)[0]
    assert column.spec is None
    assert "§1" in column.refusal


def test_an_unsigned_column_says_the_spec_has_only_signed_integers(tmp_path):
    path = write(tmp_path / "t.parquet", pa.schema([pa.field("n", pa.uint64())]))
    assert "signed" in read_columns(path)[0].refusal


def test_nullability_is_read_from_the_schema_not_from_the_rows(tmp_path):
    """A column declared nullable that happens to hold no nulls is still nullable, and the draft
    says so — the alternative is a draft that tightens a constraint nobody agreed to."""
    schema = pa.schema([pa.field("a", pa.string(), nullable=True), pa.field("b", pa.string(), nullable=False)])
    path = write(tmp_path / "t.parquet", schema, {"a": ["x"], "b": ["y"]})
    a, b = read_columns(path)
    assert a.nullable and not b.nullable


# ---- reading the file ------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["csv", "ndjson"])
def test_the_other_two_formats_are_refused_by_name_with_the_reason(tmp_path, fmt):
    with pytest.raises(InferError) as e:
        read_columns(customers(tmp_path), fmt)
    assert fmt in str(e.value)
    assert "decimal" in str(e.value)


def test_a_file_with_no_columns_drafts_nothing(tmp_path):
    path = write(tmp_path / "empty.parquet", pa.schema([]))
    with pytest.raises(InferError, match="no columns"):
        read_columns(path)


def test_a_file_that_is_not_parquet_says_so(tmp_path):
    path = tmp_path / "notparquet.parquet"
    path.write_text("id,name\n1,ada\n")
    with pytest.raises(InferError, match="not readable as parquet"):
        read_columns(path)


# ---- the draft -------------------------------------------------------------------


def test_a_property_name_is_a_reading_of_the_column_and_the_column_is_kept_verbatim(tmp_path):
    draft = infer_draft(customers(tmp_path), "Customer")
    rendered = render_draft(draft)
    assert "name: fullName" in rendered
    assert "column: full_name" in rendered


def test_two_columns_that_read_as_one_property_are_refused_naming_both(tmp_path):
    schema = pa.schema([pa.field("full_name", pa.string()), pa.field("fullName", pa.string())])
    path = write(tmp_path / "t.parquet", schema)
    with pytest.raises(InferError) as e:
        infer_draft(path, "Customer")
    assert "full_name" in str(e.value) and "fullName" in str(e.value)


def test_a_key_that_is_not_a_column_is_refused(tmp_path):
    with pytest.raises(InferError, match="no column 'nope'"):
        infer_draft(customers(tmp_path), "Customer", key="nope")


def test_a_key_on_an_unmappable_column_is_refused_with_that_columns_reason(tmp_path):
    """`segments` has no type, so it cannot be the thing that addresses a row."""
    with pytest.raises(InferError, match="§1"):
        infer_draft(customers(tmp_path), "Customer", key="segments")


def test_the_unmapped_columns_are_reported_as_unmanaged_rather_than_dropped_silently(tmp_path):
    draft = infer_draft(customers(tmp_path), "Customer")
    assert [c.name for c in draft.unmapped] == ["segments"]
    rendered = render_draft(draft)
    assert "segments" in rendered
    assert "rule 7" in rendered
    # And the half a reader would otherwise learn from a refused load.
    assert "refused at load time" in rendered


def test_region_is_mapped_because_nothing_stops_it(tmp_path):
    """The example leaves `region` unmanaged by choice, not by limitation, and a draft is where the
    difference shows: a plain string column gets a property offered, and the choice stays a human's."""
    draft = infer_draft(customers(tmp_path), "Customer")
    assert "region" in {c.name for c in draft.mapped}


# ---- the two that matter ---------------------------------------------------------


def project(tmp_path, drafted: str, config: str = "") -> tuple:
    """Write a drafted objectType into a minimal project and try to load it."""
    root = tmp_path / "proj"
    (root / "ontology").mkdir(parents=True)
    (root / "ontology" / "drafted.yaml").write_text(drafted)
    (root / "loom.yaml").write_text(
        "version: 0\n"
        "catalogs:\n"
        "  local:\n"
        "    type: iceberg-sql\n"
        "    uri: sqlite:///.warehouse/catalog.db\n"
        "    warehouse: file://.warehouse\n"
        "engine:\n"
        "  type: duckdb\n" + config
    )
    return root


def test_a_draft_with_its_placeholders_left_in_does_not_validate(tmp_path):
    """The line between a scaffold and a schema authority, asserted rather than promised."""
    draft = infer_draft(customers(tmp_path), "Customer")
    assert TODO_PRIMARY_KEY in render_draft(draft)
    assert TODO_CATALOG in render_draft(draft)
    assert TODO_TABLE in render_draft(draft)

    root = project(tmp_path, render_draft(draft))
    with pytest.raises(SpecErrors) as e:
        build(root / "ontology")
    assert TODO_PRIMARY_KEY in str(e.value)


def test_the_same_draft_validates_once_a_person_has_answered_the_questions(tmp_path):
    """And it must, or the placeholders would be hiding a generator that emits invalid YAML."""
    draft = infer_draft(
        customers(tmp_path), "Customer", key="id", catalog="local", table="crm.customers"
    )
    rendered = render_draft(draft)
    assert TODO_PRIMARY_KEY not in rendered

    root = project(tmp_path, rendered)
    ontology, _ = build(root / "ontology")
    customer = ontology.object_types["Customer"]
    assert customer.primary_key == "id"
    assert customer.backing_table == "crm.customers"
    assert customer.pk_property.unique
    # The mapped columns, and only those.
    assert set(customer.properties) == {"id", "fullName", "tier", "lifetimeValue", "region"}
    assert customer.properties["lifetimeValue"].nullable


def test_the_drafted_ingest_entry_parses_as_an_ingest_entry(tmp_path):
    """Uncommented and pasted into loom.yaml, it is a real entry — with `mode` still unanswered,
    because the three modes differ in what they destroy and no file knows which you meant."""
    draft = infer_draft(
        customers(tmp_path), "Customer", key="id", catalog="local", table="crm.customers"
    )
    block = _uncomment(render_draft(draft))
    assert TODO_MODE in block

    parsed = yaml.safe_load(block.replace(TODO_MODE, "append"))
    root = project(
        tmp_path,
        render_draft(draft),
        config=yaml.safe_dump({"ingest": parsed["ingest"], "governance": {"ingest": "allowed"}}),
    )
    diag = Diagnostics()
    config = load_config(find_config(root / "ontology"), diag)
    diag.raise_if_errors()
    (entry,) = config.ingest
    assert entry.name == "customer"
    assert entry.object_type == "Customer"
    assert entry.format == "parquet"
    # The property-to-source-column map, in the spec's direction, for exactly the renamed ones.
    assert entry.columns == {"fullName": "full_name", "lifetimeValue": "lifetime_value"}


def _uncomment(rendered: str) -> str:
    """The `ingest:` half of the output, as it would be after somebody deleted the `# `."""
    tail = rendered.split("# ---- and in loom.yaml", 1)[1]
    lines = [line[2:] if line.startswith("# ") else "" for line in tail.splitlines()]
    keep = [line for line in lines if line.startswith(("ingest:", " ")) or not line.strip()]
    return "\n".join(keep)


def test_an_entry_needs_no_column_map_when_every_property_reads_its_own_name(tmp_path):
    """`write_daily_sales_performance` already writes property names, and this is the case it is."""
    schema = pa.schema([pa.field("salesDate", pa.date32()), pa.field("grossSales", pa.decimal128(14, 2))])
    path = write(tmp_path / "daily.parquet", schema)
    rendered = render_draft(infer_draft(path, "DailySalesPerformance", key="salesDate"))
    assert "#     columns:" not in rendered
    assert "no `columns:` block" in rendered


def test_the_entry_name_defaults_to_a_kebab_reading_of_the_api_name(tmp_path):
    draft = infer_draft(customers(tmp_path), "DailySalesPerformance")
    assert draft.entry_name == "daily-sales-performance"
    assert infer_draft(customers(tmp_path), "Customer", entry="nightly").entry_name == "nightly"


# ---- the command -----------------------------------------------------------------


def test_the_command_prints_the_draft_and_says_it_does_not_validate(tmp_path, capsys):
    from loom.cli import main

    assert main(["infer", str(customers(tmp_path)), "--as", "Customer"]) == 0
    out = capsys.readouterr()
    assert "objectType:" in out.out
    assert "does not validate yet" in out.err
    # The unmapped column is on stderr too, so a redirected stdout still reports it.
    assert "segments" in out.err


def test_the_command_refuses_a_format_it_cannot_read(tmp_path, capsys):
    from loom.cli import main

    assert main(["infer", str(customers(tmp_path)), "--as", "Customer", "--format", "csv"]) == 1
    assert "csv" in capsys.readouterr().err


def test_the_command_writes_nothing(tmp_path):
    """The whole safety argument in one assertion: it produced a spec and touched no file."""
    from loom.cli import main

    source = customers(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    assert main(["infer", str(source), "--as", "Customer", "--key", "id"]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == before
