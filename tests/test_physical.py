"""Physical validation — checked against a fake catalog rather than a live one.

The `Catalog` port exists precisely so this pass is testable without an Iceberg stack: these tests
import no pyiceberg and touch no disk. The real implementations are exercised end-to-end in
test_e2e_iceberg.py.
"""

from pathlib import Path

import pytest

from loom.catalog.base import Column, TableSchema
from loom.errors import Diagnostics
from loom.loader import load_dir
from loom.validator import check_physical, validate

VALID = Path(__file__).parent / "fixtures" / "valid"

# The physical shape the worked-example fixture expects. `rest_main` is the catalog name its
# `backing:` blocks refer to.
CUSTOMERS = {
    "id": Column("id", "string", required=True, field_id=1),
    "full_name": Column("full_name", "string", required=True, field_id=2),
    "tier": Column("tier", "string", required=True, field_id=3),
    "lifetime_value": Column("lifetime_value", "double", required=False, field_id=4),
}
ORDERS = {
    "id": Column("id", "string", required=True, field_id=1),
    "customer_id": Column("customer_id", "string", required=True, field_id=2),
    "total_amount": Column("total_amount", "decimal(12,2)", required=True, field_id=3),
    "created_at": Column("created_at", "timestamptz", required=True, field_id=4),
}


class FakeCatalog:
    """An in-memory `Catalog` — introspection only, which is all check_physical needs."""

    def __init__(self, name="rest_main", tables=None):
        self.name = name
        self.tables = tables if tables is not None else {"crm.customers": CUSTOMERS, "sales.orders": ORDERS}

    def table_exists(self, table: str) -> bool:
        return table in self.tables

    def describe(self, table: str) -> TableSchema:
        if table not in self.tables:
            raise RuntimeError(f"no such table {table}")
        return TableSchema(table=table, columns=self.tables[table])

    def scan(self, table, columns=None, predicates=(), limit=None):  # pragma: no cover - unused here
        raise NotImplementedError


def _check(tables=None, catalog_name="rest_main"):
    diag = Diagnostics()
    loaded = load_dir(VALID, diag)
    validate(loaded, diag)
    assert diag.errors == [], "fixture should be structurally valid"
    check_physical(loaded, {catalog_name: FakeCatalog(catalog_name, tables)}, diag)
    return diag


def _messages(diag) -> str:
    return " | ".join(e.message for e in diag.errors)


def test_matching_tables_pass_clean():
    diag = _check()
    assert diag.errors == []
    assert diag.warnings == []


def test_undeclared_catalog_is_reported_with_a_suggestion():
    diag = _check(catalog_name="rest_mian")
    assert "is not declared in loom.yaml" in _messages(diag)
    assert any(e.hint == "did you mean 'rest_mian'?" for e in diag.errors)


def test_missing_table_is_one_error_not_an_exception():
    diag = _check(tables={"sales.orders": ORDERS})
    assert "table 'crm.customers' does not exist" in _messages(diag)
    # The other object type is still checked — the pass accumulates.
    assert not any("sales.orders" in e.message for e in diag.errors)


def test_missing_column_suggests_the_real_one():
    columns = dict(CUSTOMERS)
    columns["fullname"] = columns.pop("full_name")
    diag = _check({"crm.customers": columns, "sales.orders": ORDERS})
    assert "maps to column 'full_name', which does not exist" in _messages(diag)
    assert any(e.hint == "did you mean 'fullname'?" for e in diag.errors)


def test_incompatible_column_type_is_rejected():
    columns = dict(CUSTOMERS, lifetime_value=Column("lifetime_value", "string", required=False))
    diag = _check({"crm.customers": columns, "sales.orders": ORDERS})
    assert "column 'lifetime_value' is string, which does not promote to it" in _messages(diag)


@pytest.mark.parametrize("physical", ["int", "long", "float", "double"])
def test_widening_promotions_are_accepted(physical):
    """`ltv` is declared double; Iceberg promotes all of these to it."""
    columns = dict(CUSTOMERS, lifetime_value=Column("lifetime_value", physical, required=False))
    diag = _check({"crm.customers": columns, "sales.orders": ORDERS})
    assert diag.errors == []


def test_narrowing_is_not_a_promotion():
    """A double column cannot back a long property, even though the reverse is fine."""
    columns = dict(ORDERS, customer_id=Column("customer_id", "double", required=True))
    diag = _check({"crm.customers": CUSTOMERS, "sales.orders": columns})
    assert "does not promote to it" in _messages(diag)


def test_enum_property_needs_a_string_column():
    columns = dict(CUSTOMERS, tier=Column("tier", "int", required=True))
    diag = _check({"crm.customers": columns, "sales.orders": ORDERS})
    assert "property 'tier' declares 'enum' (Iceberg string)" in _messages(diag)


def test_decimal_precision_must_match_exactly():
    columns = dict(ORDERS, total_amount=Column("total_amount", "decimal(10,2)", required=True))
    diag = _check({"crm.customers": CUSTOMERS, "sales.orders": columns})
    assert "column 'total_amount' is decimal(10,2)" in _messages(diag)


def test_optional_primary_key_column_is_fatal():
    columns = dict(CUSTOMERS, id=Column("id", "string", required=False))
    diag = _check({"crm.customers": columns, "sales.orders": ORDERS})
    assert "a null key cannot be addressed" in _messages(diag)


def test_optional_non_key_column_only_warns():
    """Extremely common in existing lakes, so it must not block a read-only spec."""
    columns = dict(CUSTOMERS, full_name=Column("full_name", "string", required=False))
    diag = _check({"crm.customers": columns, "sales.orders": ORDERS})
    assert diag.errors == []
    assert any("declared non-nullable but column 'full_name' is optional" in w.message for w in diag.warnings)


def test_declared_nullable_over_an_optional_column_is_silent():
    diag = _check()  # `ltv` is nullable and its column is optional
    assert diag.warnings == []
