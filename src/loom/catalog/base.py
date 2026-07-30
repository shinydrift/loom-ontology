"""The `Catalog` port and its introspection value types.

`Column.iceberg_type` is a *string* rather than a pyiceberg type object on purpose: it's the same
canonical spelling `PropType.iceberg_type()` produces, so physical validation is a string
comparison against the type system's own output and nothing above this port needs pyiceberg
imported to reason about types.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class CatalogError(RuntimeError):
    """A catalog-plane failure: missing table, unreachable metastore, bad credentials.

    Belongs to the port rather than any implementation, so callers can catch it without knowing
    which catalog backend they were handed."""


@dataclass(frozen=True)
class Column:
    name: str
    iceberg_type: str  # canonical spelling, matching PropType.iceberg_type()
    required: bool
    field_id: int | None = None


@dataclass(frozen=True)
class TableSchema:
    table: str
    columns: Mapping[str, Column]  # keyed by column name, schema order


@runtime_checkable
class Catalog(Protocol):
    """A bound, queryable catalog. Implementations are expected to be cheap to construct and to
    connect lazily, so `loom validate` on a spec that never touches a table stays offline."""

    name: str

    def table_exists(self, table: str) -> bool:
        """`table` is a dotted identifier as written in `backing.table`, e.g. `crm.customers`."""
        ...

    def describe(self, table: str) -> TableSchema:
        """Introspect columns. Raises CatalogError if the table does not exist."""
        ...

    def scan(
        self,
        table: str,
        columns: Sequence[str] | None = None,
        predicates: Sequence[tuple[str, Any]] = (),
        limit: int | None = None,
    ) -> Any:
        """Materialize rows as a pyarrow.Table.

        `columns` prunes projection and `predicates` is a conjunction of `(column, value)`
        equality pairs — both are pushdown *hints*: an implementation may ignore them, so callers
        must still apply their own filtering. They exist so the common case (fetch one row by
        primary key) doesn't read a whole table off disk.
        """
        ...
