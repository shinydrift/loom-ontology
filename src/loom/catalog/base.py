"""The `Catalog` port and its introspection value types.

`Column.iceberg_type` is a *string* rather than a pyiceberg type object on purpose: it's the same
canonical spelling `PropType.iceberg_type()` produces, so physical validation is a string
comparison against the type system's own output and nothing above this port needs pyiceberg
imported to reason about types.

**Four ports, three planes, and no supersets.** `Catalog` reads. `CatalogWriter` changes a table's
*shape*. `RowWriter` changes a table's *rows*. `EditLogWriter` appends to *Loom's own record*. The
read path — the resolver, the query engines, `loom serve` — is handed a `Catalog` and cannot write
at all. `loom apply` asks for a `CatalogWriter` and therefore cannot delete a row: the port has no
verb for it. The action runtime asks for a `RowWriter` and therefore cannot alter a schema, for the
same reason. No writer extends another, because the argument that separated reads from writes points
every way at once one layer down — `apply` has no business touching rows and an action has no
business touching DDL.

It was three ports until the edit log, and the count is the honest thing to change. The third plane
is real rather than a convenience: `_loom_meta` is a table Loom created, whose schema Loom defines,
which no spec has ever named and which `plan` therefore never visits. Writing it is not a schema
change to somebody's table and not a row write to somebody's data, and giving the action runtime
either of the ports that *can* do those in order to record what it did would have handed it the
whole of that plane to get one append. What the fourth port costs is stated on `EditLogWriter`
itself; what it buys is that the runtime still holds no verb that can reach a table the spec names,
except the three singular keyed ones on `RowWriter`.

Asking is explicit in every case: `writer_for()` / `row_writer_for()` / `edit_log_writer_for()` fail
loudly, naming the catalog, against a backend that doesn't implement what was asked for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class CatalogError(RuntimeError):
    """A catalog-plane failure: missing table, unreachable metastore, bad credentials.

    Belongs to the port rather than any implementation, so callers can catch it without knowing
    which catalog backend they were handed."""


class ConcurrencyError(CatalogError):
    """A row write was refused because the table had moved since the caller read it.

    A subclass, so a caller that only knows about `CatalogError` still catches it and degrades to
    "the write failed" — which is true, if less useful. The action runtime catches this one first
    and turns it into a `conflict`, and it lives on the port for the same reason `CatalogError`
    does: telling a lost race from a broken metastore must not require pyiceberg imported one layer
    up.

    `found` is best-effort and advisory. It is read *after* the refusal, so it may already be newer
    than the snapshot that actually beat us; it is a diagnosis, never a thing to branch on."""

    def __init__(
        self, message: str, *, table: str, expected: int | None, found: int | None = None
    ) -> None:
        super().__init__(message)
        self.table = table
        self.expected = expected
        self.found = found


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
        read-then-write and hands it back as `RowWriter`'s `expect_snapshot_id`, which is what makes
        the read and the write behave as one decision.

        Callers must read it **before** the rows, not after. That order makes the recorded snapshot
        at-or-before the data, so the check reports a conflict that wasn't one — but can never miss
        one that was. Both halves of that are deliberate: the false conflicts are the price of the
        guarantee, not a defect in it, and they are absorbed by the runtime retrying rather than by
        loosening the order."""
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

    **Every verb takes `expect_snapshot_id`, and it is required.** The previous slice predicted an
    *optional* argument on two of the three; both halves of that turned out to be wrong.

    Required rather than optional, because an argument that can be omitted is a check that can be
    skipped by forgetting — the sibling of the rule that kept it out of the port until something
    passed it. There is no value meaning "don't check": `None` is a real expectation, namely "I read
    a table that had no snapshots", which asserts the branch still does not exist. A caller that
    genuinely has no expectation has not read anything, and has no business writing one row over
    another.

    All three rather than two, because `insert_row` follows a read as well — the primary-key
    existence check — and two concurrent creates on one key otherwise both pass it and both append,
    manufacturing exactly the duplicate row the runtime refuses as `ambiguous_key` every time it
    meets one afterwards.

    **The check must be atomic with the write.** Implementations lower it into the commit itself —
    for Iceberg, an `assert-ref-snapshot-id` requirement validated by the catalog against live
    metadata as the metadata pointer swaps. An implementation that re-reads, compares and then
    writes has not implemented this port: that narrows the race rather than closing it, and the
    word `expect` here promises closed. A backend that cannot express the assertion must raise
    rather than approximate one. Refusal is `ConcurrencyError`, and nothing is written.

    **Every verb also takes `commit_properties`, and it is required for the same reason.** They are
    recorded *with the commit the write produces* — for Iceberg, the snapshot summary — which is the
    only place a record of a write can be atomic with the write itself. Everything else, including
    the edit log, is a second commit that a crash can land on the wrong side of. So the identity of
    the edit travels inside the transaction that performs it, and the log table beside it is an index
    over facts the lake already carries rather than the only copy of them. A lost log row is then a
    stamped snapshot with no matching record — a detectable gap rather than silence.

    Required, not optional, and for the sibling of the `expect_snapshot_id` argument: a stamp that
    can be omitted is attribution that can be skipped by forgetting, and an unattributed commit is
    one nothing can reconcile afterwards. An empty mapping is a real value, meaning "this write is
    not being recorded" — a caller that has nothing to say about who is writing and why should have
    to say so.
    """

    name: str

    def insert_row(
        self,
        table: str,
        row: Mapping[str, Any],
        *,
        expect_snapshot_id: int | None,
        commit_properties: Mapping[str, str],
    ) -> None:
        """Append exactly one row, keyed by column name, if the table is still at
        `expect_snapshot_id`."""
        ...

    def replace_row(
        self,
        table: str,
        key_column: str,
        key_value: Any,
        row: Mapping[str, Any],
        *,
        expect_snapshot_id: int | None,
        commit_properties: Mapping[str, str],
    ) -> None:
        """Delete the rows where `key_column` equals `key_value` and append `row`, in **one
        transaction**: a reader sees the old row or the new one, never neither and never both.

        `row` must be the whole row, including the columns no property maps — which is also why
        this verb is checked. The carried columns come from a read, so committing over a table that
        has moved writes somebody else's newer value back to what it used to be."""
        ...

    def delete_row(
        self,
        table: str,
        key_column: str,
        key_value: Any,
        *,
        expect_snapshot_id: int | None,
        commit_properties: Mapping[str, str],
    ) -> None:
        """Delete the rows where `key_column` equals `key_value`.

        A row, not a column and not a table: Loom's never-drop rule is about refusing to *infer* a
        destruction from silence in a spec, and this verb only ever runs because an action declared
        `operation: delete` and a caller named a key."""
        ...


EDIT_LOG_TABLE = "_loom_meta.edits"
"""The one table `EditLogWriter` writes.

Named here rather than beside its schema, which is the inverse of where `_loom_meta.applied` keeps
its name — and the difference is the point. `applied` is written through verbs that take a table
argument, so the name has to live with the caller that supplies it. `edits` is written through a verb
that takes **no** table argument, so the name is part of what the port guarantees and belongs where
that guarantee is made. A reader of this constant is looking at the whole of what an `EditLogWriter`
can reach."""


@runtime_checkable
class EditLogWriter(Protocol):
    """Loom's own record of what an action did — append-only, to one table, named by the port.

    The action runtime holds this alongside its `RowWriter`, and the shape is chosen to make that
    safe rather than merely conventional. Three alternatives were available and each broke something
    already argued for:

    - `RowWriter.insert_row`. The log is rows, so this looks like the answer. But `insert_row`
      requires `expect_snapshot_id` and there is no honest value to pass: the append follows no read
      and puts no row over another, and "a caller that genuinely has no expectation has not read
      anything" is the rule that argument exists to state. Passing the log table's current snapshot
      to satisfy the signature would also subject every action to a check against the hottest table
      in the system — a busy log conflicting a write it exists to describe.
    - A `CatalogWriter` beside the `RowWriter`. It carries `alter_table`, so it reopens the whole of
      "an action cannot touch DDL, because the port has no verb for it" to buy one append.
    - `append_rows` here. It takes a table name and a batch, which is exactly the pair the runtime
      must not hold: with it, an action can append rows into any table the spec names.

    Hence one verb, singular, with **no `table` argument at all**. There is nothing to point at the
    wrong table with. `columns` comes from the caller because the log's schema is a policy decision
    and belongs above the port; the location does not, and stays here as `EDIT_LOG_TABLE`.

    **What it costs.** The port creates its table on first append, so an action can cause a `CREATE
    TABLE` — DDL from the write path, which the three-ports rule was written to prevent. The cost is
    bounded by the same absence: the table it can create is the one table it can name, in a namespace
    Loom owns, to a schema the caller hands it. The alternative was `apply` creating the table up
    front, which would have given the log a precondition the write does not have — Loom writes to
    lakes it never migrated, and an audit trail that switches itself off in exactly those deployments
    is worse than a create verb that can only reach `_loom_meta`.
    """

    name: str

    def append_edit(self, columns: Sequence[Column], row: Mapping[str, Any]) -> None:
        """Append one record to `EDIT_LOG_TABLE`, creating it with `columns` if it is not there.

        Purely additive and unconditional: there is no snapshot to assert, because the caller read
        nothing and is overwriting nothing. `columns` is the table's full schema in order, supplied
        on every call and consulted only on the first — the port stays stateless and never has to
        know what a Loom edit record contains."""
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


def edit_log_writer_for(catalog: Catalog) -> EditLogWriter:
    """Exchange a read handle for one that can append to Loom's own record. The action runtime asks.

    A third named exchange point rather than a flag on either of the others, for the reason the
    second one exists: the plane being asked for should be visible at the call site."""
    return _port_for(
        catalog, EditLogWriter, "edit-log writes", "an action to record what it did in"
    )


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
