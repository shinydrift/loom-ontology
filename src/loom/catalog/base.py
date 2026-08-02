"""The `Catalog` port and its introspection value types.

`Column.iceberg_type` is a *string* rather than a pyiceberg type object on purpose: it's the same
canonical spelling `PropType.iceberg_type()` produces, so physical validation is a string
comparison against the type system's own output and nothing above this port needs pyiceberg
imported to reason about types.

**Three ports, two planes, and no supersets.** `Catalog` reads. `CatalogWriter` changes a table's
*shape*. `RowWriter` changes a table's *rows*. The read path — the resolver, the query engines,
`loom serve` — is handed a `Catalog` and cannot write at all. `loom apply` asks for a
`CatalogWriter` and therefore cannot delete a row: the port has no verb for it. The action runtime
asks for a `RowWriter` and therefore cannot alter a schema, for the same reason. Neither writer
extends the other, because the argument that separated reads from writes points both ways at once
one layer down — `apply` has no business touching rows and an action has no business touching DDL.

Asking is explicit in every case: `writer_for()` / `row_writer_for()` fail loudly, naming the
catalog, against a backend that doesn't implement what was asked for.
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

    def current_snapshot_id(self, table: str) -> int | None:
        """The id of the table's current Iceberg snapshot, or None for a table with no history.

        A *read* verb, on the read port, because it answers a question about what was read: which
        version of this table did I just see. The action runtime records it alongside every
        read-then-write so the concurrency slice has something to check the write against; nothing
        enforces it yet, and the runtime says so rather than implying the two halves are one
        transaction.

        Callers that record it must read it **before** the rows, not after. That order makes the
        recorded snapshot at-or-before the data, so a later check can report a conflict that wasn't
        one — but can never miss one that was."""
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
    """The *schema* half of a catalog: create what the ontology needs, evolve what already exists.

    Narrow on purpose. It cannot drop a table, drop a column, or narrow a type — not as a policy
    check inside an implementation, but because the port has no verb for it. For the same reason it
    cannot delete or replace a row: those verbs live on `RowWriter`, and `loom apply` never asks
    for one.
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
        implementation converts them to the table's own physical types.

        The one row verb on the schema port, and it stays here rather than moving to `RowWriter`
        because it is how `_loom_meta` records history: purely additive, incapable of destroying
        anything, the same shape as the DDL verbs beside it. `RowWriter` gets its own singular
        `insert_row` instead of borrowing this, so an action can never append a batch to somebody's
        history table."""
        ...


@runtime_checkable
class RowWriter(Protocol):
    """The *row* half of a catalog: the three things a single-object action can do to one row.

    Every verb is singular and keyed. There is no batch write, no predicate, and no "update where"
    — a multi-row write is not expressible through this port, which is how the spec's single-object
    boundary (§4) is enforced at the bottom of the stack as well as at spec-load. And there is no
    schema verb, so the action runtime cannot alter a table even by accident.

    `replace_row` takes the **complete** new row rather than the changed columns. A row-level
    modify is an equality-delete plus an append, which rewrites the whole row, so every column the
    ontology does not map has to be carried across by the caller or it is silently nulled. Making
    the port take the whole row keeps that carry-across visible in the runtime, where the policy
    is and where a fake catalog can prove it, instead of hiding it in an implementation.

    When the concurrency slice lands, `replace_row` and `delete_row` grow one optional argument —
    the snapshot the row was read at — and the implementation turns it into a compare-and-swap.
    Everything that argument will need is already captured (`Catalog.current_snapshot_id`); the
    check is the only thing missing.
    """

    name: str

    def insert_row(self, table: str, row: Mapping[str, Any]) -> None:
        """Append exactly one row, keyed by column name."""
        ...

    def replace_row(
        self, table: str, key_column: str, key_value: Any, row: Mapping[str, Any]
    ) -> None:
        """Delete the rows where `key_column` equals `key_value` and append `row`, in **one
        transaction**: a reader sees the old row or the new one, never neither and never both.

        `row` must be the whole row, including the columns no property maps."""
        ...

    def delete_row(self, table: str, key_column: str, key_value: Any) -> None:
        """Delete the rows where `key_column` equals `key_value`.

        A row, not a column and not a table: Loom's never-drop rule is about refusing to *infer* a
        destruction from silence in a spec, and this verb only ever runs because an action declared
        `operation: delete` and a caller named a key."""
        ...


def writer_for(catalog: Catalog) -> CatalogWriter:
    """Exchange a read handle for one that can change a table's shape. `loom apply` asks."""
    return _port_for(catalog, CatalogWriter, "schema writes", "'loom apply' to execute against")


def row_writer_for(catalog: Catalog) -> RowWriter:
    """Exchange a read handle for one that can change a table's rows. The action runtime asks.

    A sibling of `writer_for` rather than an argument to it: two named exchange points read better
    than one that takes a port object, and the plane you are asking for should be visible at the
    call site."""
    return _port_for(catalog, RowWriter, "row writes", "an action to execute against")


def _port_for(catalog: Catalog, port: type, plane: str, purpose: str):
    """The one place a read-only handle is exchanged for a writable one.

    Structural rather than a registry: an implementation is writable if it has the verbs. The error
    matters more than the check — a catalog backend that can only be read is a perfectly reasonable
    thing to exist, and the caller should be told which one refused, and what it was being asked
    for, rather than hit an AttributeError three frames down."""
    if isinstance(catalog, port):
        return catalog
    raise CatalogError(
        f"catalog '{getattr(catalog, 'name', '?')}' does not support {plane} — it implements no "
        f"'{port.__name__}' port, so there is nothing for {purpose}"
    )
