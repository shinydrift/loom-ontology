"""The `Catalog` port and its introspection value types.

`Column.iceberg_type` is a *string* rather than a pyiceberg type object on purpose: it's the same
canonical spelling `PropType.iceberg_type()` produces, so physical validation is a string
comparison against the type system's own output and nothing above this port needs pyiceberg
imported to reason about types.

**Eight ports, five planes, and no supersets.** `Catalog` reads. `CatalogWriter` changes a table's
*shape*. `RowWriter` changes one of a table's *rows*, keyed. `BulkWriter` changes *many* of them at
once. `EditLogWriter`, `LoadLogWriter` and `SequenceLogWriter` append to *Loom's own record* — three
ports and three tables, because each of those tables is only ever created and so its columns are
forever. `VectorWriter` maintains *Loom's own derived data*. The read path — the
resolver, the query engines, `loom serve` — is handed a `Catalog` and cannot write at all. `loom
apply` asks for a `CatalogWriter` and therefore cannot delete a row: the port has no verb for it. The
action runtime asks for a `RowWriter` and therefore cannot alter a schema, for the same reason. No
writer extends another, because the argument that separated reads from writes points every way at
once one layer down — `apply` has no business touching rows and an action has no business touching
DDL.

It was three ports until the edit log and five until ingest, and the count is the honest thing to
change each time. The record plane was real rather than a convenience: `_loom_meta` is a namespace
Loom created, whose schemas Loom defines, which no spec has ever named and which `plan` therefore
never visits. Writing it is not a schema change to somebody's table and not a row write to somebody's
data, and giving a runtime either of the ports that *can* do those in order to record what it did
would have handed it a whole plane to get one append.

**`BulkWriter` is the fifth port and it opens no new plane** — it writes rows, which `RowWriter`
already did. It is separate because of what `RowWriter` *is*: every verb there is singular and keyed
on purpose, which is how spec §4's single-object boundary is enforced at the bottom of the stack and
not merely at spec-load. A batch verb added there would hand the action runtime a multi-row write and
dissolve exactly the guarantee the port exists to make. So the two sit side by side, and the thing
that decides which one a caller holds is which one it asked for. Neither can reach the other's verbs,
and `BulkWriter` has no DDL verb — **ingest never migrates**; a batch that does not fit the table is
refused, and the fix is `loom plan` / `loom apply`.

**`LoadLogWriter` is the sixth and it is `EditLogWriter` again, for a different table**, because the
guarantee those two make is *the table name is not an argument*. One port with a table parameter
would be one a caller could point at the other log — or at anything else in `_loom_meta` — which is
the whole of what the shape buys. Two ports whose verbs are named differently are two capabilities a
structural check can tell apart; one port used twice is not.

**`VectorWriter` is the seventh, and it is the first that opens a plane rather than re-cutting one.**
The two logs write *records of what happened*: append-only, never read back by Loom, and permanently
without a delete verb because an expired record and a lost one are the same sight. A vector is not a
record. It is **derived data about a row that exists now** — it goes stale when the row's text
changes, it is meaningless once the row is gone, and keeping it correct therefore needs the two verbs
the log ports refuse: an upsert and a delete. Handing that plane to `BulkWriter` was the alternative,
and it fails on the property `_loom_meta` has always had: `BulkWriter` takes a table name, so a
runtime holding one to maintain a sidecar could point it at the ontology's own tables.

So this port keeps the guarantee the log ports make — *the table is not an argument* — while writing
many tables. Its verbs take an **object type name** and derive the table themselves, which is why
`vector_table()` lives here beside `EDIT_LOG_TABLE`: a reader of this module is looking at the whole
of what a `VectorWriter` can reach, and it is `_loom_meta.vectors__*` and nothing else. The sidecar is
one table per type rather than one global table because the `key` column is a *join* column, and a
type whose primary key is a `long` should not have it string-encoded and cast back on every ranked
query.

Asking is explicit in every case: `writer_for()` / `row_writer_for()` / `bulk_writer_for()` /
`edit_log_writer_for()` / `load_log_writer_for()` / `vector_writer_for()` fail loudly, naming the
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


@runtime_checkable
class BulkWriter(Protocol):
    """The *many rows at once* half of a catalog: what an ingest declares and a pipeline hands over.

    Deliberately **not** `RowWriter` with a batch argument. Every verb there is singular and keyed
    because that is where §4's single-object boundary is enforced — a runtime holding a batch verb
    could write a hundred rows from an action that declared one. And deliberately **not**
    `CatalogWriter`, whose `append_rows` is the right shape and the wrong neighbours: it sits beside
    `alter_table`, so a loader holding it could migrate the table it is loading into.

    **There is no schema verb here, permanently.** Ingest never migrates. A batch whose columns do
    not fit the table is refused naming the column, and the fix is `loom plan` / `loom apply`. This
    is the never-drop rule pointed at a new plane: Loom will not infer a schema change from the shape
    of somebody's file, any more than it infers a column drop from the silence of a spec.

    **The three verbs are the three things a declared load can mean**, and each is a different
    promise about the rows that were already there — `append` adds to them, `merge` replaces the ones
    it names, `replace` is the whole table. There is no `delete_where`: a bulk delete is a
    destruction nothing in a spec declares, and the two verbs here that destroy do it as the
    documented consequence of a mode an operator wrote down.

    **`append_batch` takes no `expect_snapshot_id`, and the other two require one.** That asymmetry
    is the point rather than an inconsistency with `RowWriter`, where all three verbs are checked:

    - An append **follows no read and puts no row over another**, which is `EditLogWriter`'s
      argument for the same absence, said about a bigger batch. There is no honest value to pass:
      the caller read nothing, so it holds no expectation, and *a caller that genuinely has no
      expectation has not read anything* is the rule that argument exists to state. Requiring one
      would also make every concurrent append to a busy table refuse the others — a table that
      cannot be loaded twice at once, to check something no append can get wrong.
    - A **merge** reads the rows it is about to rewrite, so that the columns the ontology does not
      map are carried across rather than nulled. That read is exactly `RowWriter.replace_row`'s, and
      committing over a table that has moved writes somebody else's newer value back to what it used
      to be — one row at a time there, a batch at a time here.
    - A **replace** reads nothing and destroys everything, which sounds like the append case and is
      the opposite of it. What it must not do is destroy a write nobody saw: the expectation is the
      snapshot the loader observed when it decided the table's whole contents were this batch, and a
      commit that landed since is precisely the thing that decision did not account for.

    The check is atomic with the write on the two verbs that take it, under `RowWriter`'s rule and
    with no softening: an implementation that re-reads, compares and then writes has not implemented
    this port, and a backend that cannot express the assertion must raise rather than approximate
    one. Refusal is `ConcurrencyError`, and nothing is written.

    **Every verb takes `commit_properties`, and it is required on all three** — including the
    append, which takes no snapshot. The two arguments answer different questions and only one of
    them is about a race: a stamp is how the write says which load it was, and that is as true of an
    append as of anything else. It is recorded with the commit the write produces, which is the only
    place a record of a write is atomic with the write itself; the log table beside it is a second
    commit, and `ingest.log` is honest about which of the two can be lost.

    **Whole-batch or nothing.** Each verb is one commit. A partial load — some rows landed, some
    refused — is the state nobody declared and nothing can describe afterwards, so implementations
    must not chunk a batch into several commits to make a large one fit. A batch too large to commit
    is a `CatalogError`, not a series of smaller successes.
    """

    name: str

    def append_batch(
        self,
        table: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        commit_properties: Mapping[str, str],
    ) -> None:
        """Append every row, each keyed by column name, in one commit.

        Adds to what is there and reads none of it. Nothing here checks whether a key already
        exists: that question needs the table, and the verb that asks it is `merge_batch`."""
        ...

    def merge_batch(
        self,
        table: str,
        key_column: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        expect_snapshot_id: int | None,
        commit_properties: Mapping[str, str],
    ) -> None:
        """Delete every row whose `key_column` is among the batch's keys and append the batch, in
        **one transaction**: a reader sees the whole old set or the whole new one.

        Each row must be **complete**, including the columns no property maps — the same requirement
        `replace_row` makes and for the same reason. A merge is an equality-delete plus an append,
        so a column the caller leaves out is not preserved, it is nulled. The carry-across is the
        caller's job because that is where a fake catalog can prove it happened."""
        ...

    def replace_table(
        self,
        table: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        expect_snapshot_id: int | None,
        commit_properties: Mapping[str, str],
    ) -> None:
        """Make the table's contents exactly `rows`, in one transaction.

        The only verb in Loom that destroys rows it never read, which is why it is reachable solely
        from a declared `mode: replace` an operator wrote in `loom.yaml` and never from a default.
        An empty `rows` empties the table, and that is a real value rather than a no-op — a
        materialization whose source went empty says so."""
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

    Hence **no `table` argument at all**, on any verb. There is nothing to point at the wrong table
    with. `columns` comes from the caller because the log's schema is a policy decision and belongs
    above the port; the location does not, and stays here as `EDIT_LOG_TABLE`.

    **The second verb widens nothing, which is why there is one.** `ensure_log` is `append_edit`
    with the row taken out: same single table, same absent table argument, same DDL that was already
    reachable. It buys `governance.edit_log: required` the only exact answer to *can this deployment
    record what it writes* — see `action.log.require_edit_log` — and it adds no reach a caller of
    this port did not already have. `test_action_log.py` asserts the pair rather than trusting it.

    **And there is no third verb, permanently.** Nothing here removes a record. An expired edit and
    a lost one would be the same sight to a reader holding a stamped snapshot with no matching row,
    and that distinction is the entire reason `ActionRuntime._record` writes after the commit rather
    than before it. A retention window is therefore a command Loom has not built, holding a port
    that is not this one, and never a verb an action can reach.

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

    def ensure_log(self, columns: Sequence[Column]) -> None:
        """Create `EDIT_LOG_TABLE` with `columns` if it is not there, and append nothing.

        Idempotent, and the half of `append_edit` that can be done in advance. It exists because
        *can this catalog hold an edit log* has no honest answer short of making one: `table_exists`
        asks the wrong question, since `False` is the ordinary state of a catalog whose first append
        has not happened, and a probe answers about an instant rather than a permission.

        Creating a table records **nothing that might not have happened**, which is what keeps this
        clear of the log-then-write ordering `ActionRuntime._record` rejected. An empty log is not a
        table of intentions; it is a permission, checked once, somewhere a deployment can still
        refuse to start."""
        ...


LOAD_LOG_TABLE = "_loom_meta.loads"
"""The one table `LoadLogWriter` writes, named here for `EDIT_LOG_TABLE`'s reason.

A **sibling** of `_loom_meta.edits` rather than more rows in it, and the argument is that table's own
"the columns are forever". `edits` holds one row per run of a declared action, and its schema says
so: `action`, `operation`, `object_key`, `before`, `after`, `attempts`. A load has none of those and
has four things `edits` has no column for and can never grow one — how many rows landed, how many
were rejected, which mode, and which load. Writing loads into `edits` would mean every row carrying
half a schema that does not apply to it, and every reader of the table learning to tell two record
kinds apart by which columns are null.

The precedent is one level up and points the same way: `_loom_meta.applied` is what `apply` did to
schemas, and it was not folded into anything either. What varies is the plane being recorded, and a
plane gets a table."""


@runtime_checkable
class LoadLogWriter(Protocol):
    """Loom's own record of what an ingest did to a table — append-only, to one table, named here.

    `EditLogWriter` again, verb for verb, and the repetition is deliberate: the property that makes
    that port safe to hand a runtime is that **the table is not an argument**, and a single port
    parameterized by table name would lose exactly that. A caller holding this can reach
    `LOAD_LOG_TABLE` and nothing else, in a namespace Loom owns, to a schema the caller supplies.

    The verbs are named apart from `EditLogWriter`'s (`append_load`, not `append_edit`) because
    `runtime_checkable` structural checks look at verb *names*: two ports sharing a signature would
    be one capability wearing two labels, and `_port_for` could not tell an implementation of one
    from an implementation of the other.

    **There is no delete verb here either, and for the same permanent reason.** An expired record and
    a lost one are the same sight to a reader holding a stamped snapshot with no matching row, and
    that distinction is the whole return on writing the record after the commit rather than before
    it. See `EditLogWriter` for the full argument; nothing about a bulk write weakens it.
    """

    name: str

    def append_load(self, columns: Sequence[Column], row: Mapping[str, Any]) -> None:
        """Append one record to `LOAD_LOG_TABLE`, creating it with `columns` if it is not there.

        Unconditional and purely additive: the caller read nothing and is overwriting nothing, so
        there is no snapshot to assert. `columns` is the table's full schema in order, supplied on
        every call and consulted only on the first."""
        ...

    def ensure_load_log(self, columns: Sequence[Column]) -> None:
        """Create `LOAD_LOG_TABLE` with `columns` if it is not there, and append nothing.

        `EditLogWriter.ensure_log`'s twin, and it exists because `governance.edit_log: required` now
        has two logs to prove rather than one. A deployment that demands it can record what it writes
        is demanding it about **writes**, and a bulk load is a write — so a posture that proved only
        the edit log would leave the deployment able to load unrecorded while believing otherwise,
        which is the exact half-truth ingest was built to close."""
        ...


SEQUENCE_LOG_TABLE = "_loom_meta.sequences"
"""The one table `SequenceLogWriter` writes. The third time this argument has been made.

`LOAD_LOG_TABLE` exists because `edits`' columns are forever and a load has four things that table
can never grow a column for. **A sequence is now in exactly that position with respect to `loads`.**
The obvious cheaper move — a `sequence_id` column beside `load_id` — is the one thing `LOAD_COLUMNS`
already forbids in writing: that table is only ever *created*, so a column added today can never
reach a log that already exists, and every deployment that has run `loom ingest` once has one.

So a sequence gets a table, and the split turns out to be right on its own terms rather than only
forced. A load's record answers *what did this file do to this table*. A sequence's answers *which
loads were one run, in what order, and where did it stop* — three of which are properties of the run
and of no load in it. The one that would have fitted in a column is the id, and an id alone would not
have made the run readable.

The precedent above it points the same way twice: `_loom_meta.applied` for schemas, `edits` for rows,
`loads` for batches. What varies is the plane being recorded, and a plane gets a table."""


@runtime_checkable
class SequenceLogWriter(Protocol):
    """Loom's own record of an ordered run of loads — append-only, to one table, named here.

    `LoadLogWriter` again, verb for verb, and the third instance of a rule worth restating because
    the cost of breaking it is invisible: **the table is not an argument**. A caller holding this can
    reach `SEQUENCE_LOG_TABLE` and nothing else.

    The verbs are named apart from the other two logs' for `LoadLogWriter`'s reason — structural
    `runtime_checkable` checks look at verb names, so two ports sharing a signature would be one
    capability wearing two labels and `_port_for` could not tell them apart.

    **No delete verb, for the third time and the same permanent reason.** An expired record and a
    lost one are the same sight to a reader.
    """

    name: str

    def append_sequence(self, columns: Sequence[Column], row: Mapping[str, Any]) -> None:
        """Append one record to `SEQUENCE_LOG_TABLE`, creating it with `columns` if it is not there.

        Unconditional and purely additive, for `append_load`'s reason: the caller read nothing and is
        overwriting nothing, so there is no snapshot to assert."""
        ...

    def ensure_sequence_log(self, columns: Sequence[Column]) -> None:
        """Create `SEQUENCE_LOG_TABLE` with `columns` if it is not there, and append nothing.

        `ensure_load_log`'s twin, and it answers the same demand: `governance.edit_log: required` is
        a demand about *writes*, and a sequence run is how a deployment does several at once. A
        posture that proved two logs and not the third would leave a deployment able to run an
        unrecorded sequence while believing it could not."""
        ...


VECTOR_KEY_COLUMN = "key"
"""The column every sidecar is keyed and merged on, defined by the port rather than by its caller.

Unlike `columns`, which is the caller's decision because the schema varies per type, this cannot be:
the two writing verbs below take no key argument, so a caller that could rename this could point a
merge at a column that is not the key and turn an upsert into a duplicate."""


VECTOR_TABLE_PREFIX = "_loom_meta.vectors__"
"""The namespace and name-stem of every table a `VectorWriter` can reach.

Named here for `EDIT_LOG_TABLE`'s reason, and it is doing more work than that constant is: `edits` is
one table, so naming it here makes the port's reach *visible*. This is a family, so naming the stem
here is what makes the reach **bounded** — no verb below takes a table, and `vector_table` is the only
function that produces one."""


def vector_table(object_type: str) -> str:
    """`Order` -> `_loom_meta.vectors__Order`. The sidecar of one object type.

    A `__` separator rather than a `.` because the second would put every type in a namespace of its
    own, and `_loom_meta` is one namespace Loom creates and owns. It is also why this is a function
    rather than an f-string at three call sites: the name is a fact about the port, and a caller that
    can spell it itself is a caller that can spell something else."""
    return f"{VECTOR_TABLE_PREFIX}{object_type}"


@runtime_checkable
class VectorWriter(Protocol):
    """Loom's own derived data — one sidecar per object type, none of them named by the caller.

    **The two verbs the log ports refuse, and why they are safe here.** `EditLogWriter` has no delete
    verb permanently, because a record that can be removed makes an expired edit and a lost one the
    same sight. Nothing about that argument reaches a vector: a vector asserts nothing about the past,
    it *describes a row that exists*, and a stale one is not evidence of anything — it is a wrong
    answer waiting to be returned. So `merge_vectors` overwrites and `delete_vectors` removes, and the
    absence of either would be the defect rather than the safeguard.

    `delete_vectors` is additionally the one path by which text Loom derived from a row stops being
    recoverable, which is a heavier job than pruning an orphan. See `embed.store` for the lag that
    leaves and who is on the hook for it.

    **Every verb takes an object type, never a table.** That is `EditLogWriter`'s guarantee held
    across a family of tables — see the module docstring — and it is the reason this is a port rather
    than a `BulkWriter` the embed runtime happens to point at `_loom_meta`.

    **`columns` comes from the caller**, as it does for both logs: the sidecar's schema is a decision
    that belongs above the port, and it varies per type anyway, since `key` takes the type's own
    primary-key type. The port stays stateless and never learns what a Loom vector row contains.

    **`merge_vectors` asserts a snapshot and `delete_vectors` has no parameter for one**, which is
    `BulkWriter`'s split — `merge_batch` checks, `append_batch` has no argument to check with — and
    it lands here for the same reason plus a sharper one.

    M3's rule is *a caller that genuinely has no expectation has not read anything*. A merge follows
    a read: the embed runtime decided **which** vectors to write by diffing this table against the
    object's, so it holds a real expectation and two concurrent reconciles must not interleave. A
    sidecar that does not exist yet is `None`, which asserts *there is no snapshot* and is the honest
    expectation for a table created a moment ago.

    A delete follows no such read. Removing a key is idempotent and commutes with every change to
    every other key, so an assertion would protect nothing — and it would do active harm in the one
    caller that matters most: an action deleting a row prunes that row's vector and **fails if it
    cannot**, so a check here would let a concurrent `loom embed` refuse an erasure. That is exactly
    backwards. The parameter is therefore absent rather than optional, so no caller can supply one
    and no implementation can pretend to honour it.
    """

    name: str

    def ensure_vectors(self, object_type: str, columns: Sequence[Column]) -> None:
        """Create this type's sidecar with `columns` if it is not there. Idempotent.

        `ensure_log`'s twin, and it exists for a second reason beyond proving a permission: the
        writing verbs below assert a snapshot, and there is no snapshot to read from a table that
        does not exist. Creating it first is what makes `None` mean *empty* rather than *absent*."""
        ...

    def merge_vectors(
        self,
        object_type: str,
        columns: Sequence[Column],
        rows: Sequence[Mapping[str, Any]],
        *,
        expect_snapshot_id: int | None,
    ) -> None:
        """Upsert `rows` into this type's sidecar on `key`, as one commit.

        Each row must be complete — `merge_batch`'s requirement, for its reason: a merge is an
        equality-delete plus an append, so a column left out is nulled rather than preserved. Here
        the caller always has every column, because every one of them is derived in the same breath
        as the vector."""
        ...

    def delete_vectors(self, object_type: str, keys: Sequence[Any]) -> None:
        """Remove the rows of this type's sidecar with these keys, as one commit.

        Keyed and enumerated rather than predicated: there is no filter argument, so the widest thing
        this can express is *these keys*, and emptying a sidecar means naming every row in it. The
        one verb in Loom that removes derived data, and the erasure slice's entry point.

        Unconditional — see the class docstring for why this one has no snapshot to assert."""
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


def bulk_writer_for(catalog: Catalog) -> BulkWriter:
    """Exchange a read handle for one that can change many of a table's rows. Ingest asks.

    A sibling of `row_writer_for` and not a mode of it, for the reason the two ports are separate:
    the plane *and the scope* you are asking for should be visible at the call site, and a runtime
    that can write one row must not be able to reach this by passing a flag."""
    return _port_for(catalog, BulkWriter, "bulk row writes", "a declared ingest to load through")


def edit_log_writer_for(catalog: Catalog) -> EditLogWriter:
    """Exchange a read handle for one that can append to Loom's own record. The action runtime asks.

    A third named exchange point rather than a flag on either of the others, for the reason the
    second one exists: the plane being asked for should be visible at the call site."""
    return _port_for(
        catalog, EditLogWriter, "edit-log writes", "an action to record what it did in"
    )


def load_log_writer_for(catalog: Catalog) -> LoadLogWriter:
    """Exchange a read handle for one that can append to Loom's record of ingests. Ingest asks."""
    return _port_for(
        catalog, LoadLogWriter, "load-log writes", "an ingest to record what it loaded in"
    )


def sequence_log_writer_for(catalog: Catalog) -> SequenceLogWriter:
    """Exchange a read handle for one that can append to Loom's record of sequence runs.

    Asked for by `loom sequence` and by nothing else — including `loom ingest`, which records a load
    and has no run to record. That is the whole reason this is a separate exchange point rather than
    a second verb pair on `load_log_writer_for`'s port: a single load must not be able to reach the
    table that says several of them were one run."""
    return _port_for(
        catalog, SequenceLogWriter, "sequence-log writes", "a sequence to record its run in"
    )


def vector_writer_for(catalog: Catalog) -> VectorWriter:
    """Exchange a read handle for one that can maintain Loom's vector sidecars. `loom embed` asks."""
    return _port_for(
        catalog, VectorWriter, "vector sidecar writes", "'loom embed' to reconcile vectors in"
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
