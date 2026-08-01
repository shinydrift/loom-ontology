"""`loom apply` against a real Iceberg catalog — M2's definition of done.

The fake catalog in test_apply.py proves the *policy*. This proves the port: that the DDL Loom
emits is DDL pyiceberg accepts, that a promotion really does keep its field id, that `_loom_meta`
round-trips through Parquet, and that the whole thing is idempotent when the second run is a
genuinely fresh look at a real metastore.

It starts from an empty warehouse rather than the seeded example, because bootstrapping a lake
from nothing but a spec is the thing `apply` is for.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from loom import build
from loom.config import find_config, load_config
from loom.errors import Diagnostics
from loom.migrate import (
    APPLIED,
    REFUSED,
    UP_TO_DATE,
    MetaStore,
    apply_plan,
    diff_ontology,
    snapshot_spec,
)
from loom.migrate.meta import META_TABLE

pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")

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


def _catalogs(config):
    from loom.catalog import open_catalogs

    return open_catalogs(config)


def _local(config):
    """A *fresh* handle each time — PyIcebergCatalog caches introspection, and a test that reused
    one could pass on a schema that only exists in that cache."""
    return _catalogs(config)["local"]


def _apply(target, ontology, catalogs):
    diag = Diagnostics()
    plan = diff_ontology(ontology, catalogs, diag)
    diag.raise_if_errors()
    return apply_plan(plan, catalogs, snapshot_spec(target / "ontology"))


def test_apply_bootstraps_an_empty_warehouse(project):
    target, ontology, config = project
    catalogs = _catalogs(config)

    result = _apply(target, ontology, catalogs)

    assert result.status == APPLIED, result.error
    assert sorted(o.table for o in result.applied) == ["crm.customers", "sales.orders"]
    # Namespaces did not exist a moment ago; apply created them rather than failing.
    assert [o.namespace_created for o in result.applied] == ["crm", "sales"]

    schema = catalogs["local"].describe("crm.customers")
    assert {c.name: c.iceberg_type for c in schema.columns.values()} == {
        "id": "string",
        "full_name": "string",
        "tier": "string",
        "lifetime_value": "double",
    }
    assert schema.columns["id"].required is True
    assert schema.columns["lifetime_value"].required is False
    # decimal and timestamptz survive the trip through the type map, which is where a DDL layer
    # usually loses them.
    orders = catalogs["local"].describe("sales.orders")
    assert orders.columns["total_amount"].iceberg_type == "decimal(12,2)"
    assert orders.columns["created_at"].iceberg_type == "timestamptz"


def test_the_result_is_what_validate_physical_and_the_read_path_expect(project):
    """The proof that apply and the rest of Loom agree: the tables it just made are tables the
    physical validator accepts without a single warning."""
    target, ontology, config = project
    _apply(target, ontology, _catalogs(config))

    from loom.loader import load_dir
    from loom.validator import check_physical, validate

    diag = Diagnostics()
    loaded = load_dir(target / "ontology", diag)
    validate(loaded, diag)
    check_physical(loaded, _catalogs(config), diag)
    assert [e.render() for e in diag.errors] == []
    assert [w.render() for w in diag.warnings] == []


def test_a_second_run_against_a_fresh_catalog_handle_does_nothing(project):
    """Idempotency where it counts: a new process, a new catalog connection, no cached schemas."""
    target, ontology, config = project
    first = _apply(target, ontology, _catalogs(config))

    second = _apply(target, ontology, _catalogs(config))

    assert (first.status, second.status) == (APPLIED, UP_TO_DATE)
    assert second.tables == ()
    history = MetaStore(_local(config)).history()
    assert [r.version for r in history] == [1], "the no-op run wrote no second row"


def test_an_added_property_migrates_the_live_table_in_place(project):
    """The everyday case: edit the YAML, apply, and the existing table gains a column — with the
    other columns' field ids untouched, which is what makes the existing data files still readable."""
    target, ontology, config = project
    # One catalog handle for both applies, deliberately: PyIcebergCatalog caches introspection, so
    # a DDL path that forgets to invalidate it would plan the same add twice and fail here.
    catalogs = _catalogs(config)
    _apply(target, ontology, catalogs)
    before = catalogs["local"].describe("crm.customers")

    customer = target / "ontology" / "customer.yaml"
    customer.write_text(
        customer.read_text().replace(
            "  searchable: [name, tier]",
            "    - { name: region, type: string, column: region, nullable: true }\n  searchable: [name, tier]",
        )
    )
    edited, _ = build(target / "ontology")
    result = _apply(target, edited, catalogs)

    assert result.status == APPLIED, result.error
    assert [o.action for o in result.applied] == ["alter"]
    after = _local(config).describe("crm.customers")
    assert "region" in after.columns
    assert after.columns["region"].required is False
    assert {c.name: c.field_id for c in before.columns.values()}.items() <= {
        c.name: c.field_id for c in after.columns.values()
    }.items()
    assert after.columns["region"].field_id == 5, "a new field id, never a reused one"
    # The same handle again: it must see the column it just added, not the schema it cached before.
    assert _apply(target, edited, catalogs).status == UP_TO_DATE


def test_a_promotion_keeps_the_field_id_and_the_rows(project):
    """`int -> long` is classified physical-safe on the promise that Iceberg rewrites no data. If
    that promise were wrong, the rows written before the migration would be the ones to notice."""
    import pyarrow as pa
    from pyiceberg.catalog.sql import SqlCatalog
    from pyiceberg.schema import Schema
    from pyiceberg.types import IntegerType, NestedField, StringType

    target, ontology, config = project
    cfg = config.catalogs["local"]
    impl = SqlCatalog("local", uri=cfg.uri, warehouse=cfg.warehouse)
    impl.create_namespace("hr")
    impl.create_table(
        "hr.people",
        schema=Schema(
            NestedField(1, "id", StringType(), required=True),
            NestedField(2, "headcount", IntegerType(), required=False),
        ),
    ).append(
        pa.table(
            {"id": ["p1"], "headcount": [7]},
            schema=pa.schema([pa.field("id", pa.string(), nullable=False), pa.field("headcount", pa.int32())]),
        )
    )

    (target / "ontology" / "person.yaml").write_text(
        """
objectType:
  apiName: Person
  primaryKey: id
  title: id
  backing: { catalog: local, table: hr.people }
  properties:
    - { name: id, type: string, column: id, unique: true }
    - { name: headcount, type: long, column: headcount, nullable: true }
"""
    )
    edited, _ = build(target / "ontology")
    result = _apply(target, edited, _catalogs(config))

    assert result.status == APPLIED, result.error
    after = _local(config).describe("hr.people")
    assert after.columns["headcount"].iceberg_type == "long"
    assert after.columns["headcount"].field_id == 2
    assert impl.load_table("hr.people").scan().to_arrow().to_pylist() == [{"id": "p1", "headcount": 7}]


def test_a_breaking_change_leaves_the_live_table_alone(project):
    target, ontology, config = project
    _apply(target, ontology, _catalogs(config))

    customer = target / "ontology" / "customer.yaml"
    customer.write_text(customer.read_text().replace("column: lifetime_value, nullable: true", "column: lifetime_value"))
    edited, _ = build(target / "ontology")
    result = _apply(target, edited, _catalogs(config))

    assert result.status == REFUSED
    assert _local(config).describe("crm.customers").columns["lifetime_value"].required is False
    assert [r.version for r in MetaStore(_local(config)).history()] == [1]


def test_the_meta_table_is_a_readable_iceberg_table(project):
    """`_loom_meta` is a plain table in the lake — anyone with an Iceberg client can read the
    history, which is the whole reason it isn't a state file in the repo."""
    target, ontology, config = project
    _apply(target, ontology, _catalogs(config))

    catalog = _local(config)
    assert catalog.table_exists(META_TABLE)
    entry = MetaStore(catalog).latest()
    assert entry.version == 1
    assert entry.status == "applied"
    assert entry.content_hash == snapshot_spec(target / "ontology").content_hash
    assert entry.loom_version and entry.actor
    # The spec is stored verbatim, which is what a rollback will restore from.
    assert json.loads(entry.spec)["customer.yaml"] == (target / "ontology" / "customer.yaml").read_text()
    assert {e["action"] for e in entry.summary_data()} == {"create"}

    # ...and the managed tables carry the same version, without anyone having to find this table.
    from pyiceberg.catalog.sql import SqlCatalog

    cfg = config.catalogs["local"]
    props = SqlCatalog("local", uri=cfg.uri, warehouse=cfg.warehouse).load_table("crm.customers").properties
    assert props["loom.applied_version"] == "1"
    assert props["loom.spec_hash"] == entry.content_hash
