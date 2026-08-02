"""The `Catalog` port and its introspection value types.

`Column.iceberg_type` is a *string* rather than a pyiceberg type object on purpose: it's the same
canonical spelling `PropType.iceberg_type()` produces, so physical validation is a string
comparison against the type system's own output and nothing above this port needs pyiceberg
imported to reason about types.

Writes live in a *second* port, `CatalogWriter`, rather than as extra methods on `Catalog`. The
read path — the resolver, the query engines, `loom serve` — is handed a `Catalog`, so it cannot
execute DDL even by accident; only `loom apply` asks for a writer, and asking is an explicit
`writer_for()` call that fails loudly against a backend that doesn't have one.
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


@dataclass(frozen=True)
class SchemaEdit:
    """One column-level change to an existing table, in the port's own vocabulary.

    `column` is the *end state* the edit produces, not the current one — the implementation needs
    nothing else to make the edit, and the caller has already classified it. Deliberately not
    `migrate.ColumnChange`: that type carries a severity and a human-readable reason, which are
    the planner's concerns, and importing it here would point the catalog layer at the layer
    above it.

    `rename` is the one op that needs a second name: `column.name` is what the column becomes and
    `renamed_from` is what it is called right now. It is a remap rather than an add-and-abandon —
    the field id survives, so every existing data file keeps being read under the new name.

    The four ops are exactly the ones that are safe to execute (see `migrate.Severity`); there is
    no `drop` and no incompatible retype, because Loom never proposes either.
    """

    op: str  # add | rename | promote | relax
    column: Column
    renamed_from: str = ""  # `rename` only


@runtime_checkable
class CatalogWriter(Protocol):
    """The write half of a catalog: create what the ontology needs, evolve what already exists.

    Narrow on purpose. It cannot drop a table, drop a column, or narrow a type — not as a policy
    check inside an implementation, but because the port has no verb for it.
    """

    name: str

    def ensure_namespace(self, table: str) -> bool:
        """Create the namespace `table` lives in if it is missing. Returns True if it was created.

        Namespaces are the one piece of physical structure Loom will conjure without being asked:
        a spec backed by `crm.customers` on an empty warehouse can't be applied otherwise, and an
        empty namespace holds no data anyone can lose."""
        ...

    def create_table(
        self, table: str, columns: Sequence[Column], properties: Mapping[str, str] = {}
    ) -> None:
        """Create `table` with exactly `columns`, in order. Raises CatalogError if it exists."""
        ...

    def alter_table(
        self, table: str, edits: Sequence[SchemaEdit], properties: Mapping[str, str] = {}
    ) -> None:
        """Apply every edit to `table` in a **single transaction**: all of them commit, or none.

        `edits` is **ordered**, and a `rename` precedes any other edit to the column it renames.
        Implementations may rely on that: it is what lets an implementation whose schema-update API
        resolves names against the pre-transaction schema translate the later edits back to the old
        name. Callers must not reorder.

        `properties` are set in that same transaction, so a table's recorded provenance can never
        drift from the schema it describes."""
        ...

    def append_rows(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """Append rows, each keyed by column name. Values are plain Python objects; the
        implementation converts them to the table's own physical types."""
        ...


def writer_for(catalog: Catalog) -> CatalogWriter:
    """The one place a read-only handle is exchanged for a writable one.

    Structural rather than a registry: an implementation is writable if it has the verbs. The
    error matters more than the check — a catalog backend that can only be read is a perfectly
    reasonable thing to exist, and `apply` should say which one refused rather than raise an
    AttributeError three frames down."""
    if isinstance(catalog, CatalogWriter):
        return catalog
    raise CatalogError(
        f"catalog '{getattr(catalog, 'name', '?')}' is read-only — it implements no write port, "
        f"so there is nothing for 'loom apply' to execute against"
    )
