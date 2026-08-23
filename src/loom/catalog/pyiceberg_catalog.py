"""pyiceberg-backed implementation of the `Catalog` port.

One class serves both `iceberg-rest` and `iceberg-sql`: pyiceberg already abstracts the metastore
difference, so the only thing that varies is construction (see factory.py). Anything genuinely
catalog-specific belongs there, not here.

pyiceberg is imported lazily, inside methods, so that `import loom` and a structural
`loom validate` stay dependency-free — the spec module has no business requiring an Iceberg stack.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import (
    EDIT_LOG_TABLE,
    LOAD_LOG_TABLE,
    VECTOR_KEY_COLUMN,
    CatalogError,
    Column,
    ConcurrencyError,
    SchemaEdit,
    TableSchema,
    vector_table,
)

_DECIMAL = re.compile(r"^decimal\((\d+),\s*(\d+)\)$")

_MAIN = "main"
"""The branch every Loom write targets. Loom has no branch or tag vocabulary — `scan` reads the
table, so the ref a write must be asserted against is the one a read would have seen."""


def canonical_iceberg_type(t: object) -> str:
    """pyiceberg's spelling -> the spelling `PropType.iceberg_type()` produces.

    Only `decimal` actually differs (pyiceberg renders `decimal(12, 2)`), but normalizing all of
    them through one function keeps the comparison in physical validation a plain string equality
    and gives a single place to fix the next divergence."""
    return str(t).replace(" ", "")


def iceberg_type(canonical: str, element_id: int | None = None):
    """The inverse: the type system's spelling -> a pyiceberg type object, for DDL.

    Only the spellings `PropType.iceberg_type()` can produce are here, plus the two the physical
    side already tolerates on a column Loom didn't create (`float`, naive `timestamp`), plus
    `list<float>`, which `PropType` cannot produce at all. Anything else is a bug above this line
    rather than a user error, but it still gets a message that names the spelling instead of a
    KeyError.

    **`list<float>` is here without being in `ALL_KINDS`, and that gap is deliberate.** No property
    may declare it — spec §2's kinds are unchanged, and a spec that could say `type: list<float>`
    would be a spec that can hand Loom a vector, which is the whole thing `semantic:` exists not to
    be. It is reachable only from `embed.store`'s column list, for a table in `_loom_meta` no spec
    names. The type system stays the set of things an *ontology* can say; this function is the set of
    things *Loom* can create, and this milestone is the first time those differ.

    `element_id` is required for the list and refused for everything else. Iceberg gives every
    nested field an id of its own out of the same space as the top-level ones, so the caller
    allocating ids is the only thing that can know which one is free — see `create_table`."""
    from pyiceberg import types as t

    if canonical == "list<float>":
        if element_id is None:  # pragma: no cover - every caller allocates one
            raise CatalogError(
                "'list<float>' needs an element field id — Iceberg numbers nested fields out of the "
                "same space as top-level ones, so only the caller assigning ids knows which is free"
            )
        # `element_required=True`: a null *inside* a vector is not a sparse embedding, it is a
        # corrupt one, and `array_cosine_similarity` has no answer for it. A row with no vector at
        # all is spelled by the column being null, which the column being optional already allows.
        return t.ListType(element_id=element_id, element_type=t.FloatType(), element_required=True)

    simple = {
        "string": t.StringType,
        "boolean": t.BooleanType,
        "int": t.IntegerType,
        "long": t.LongType,
        "float": t.FloatType,
        "double": t.DoubleType,
        "date": t.DateType,
        "time": t.TimeType,
        "timestamp": t.TimestampType,
        "timestamptz": t.TimestamptzType,
    }
    if canonical in simple:
        return simple[canonical]()
    m = _DECIMAL.match(canonical)
    if m:
        return t.DecimalType(int(m.group(1)), int(m.group(2)))
    raise CatalogError(f"no Iceberg type for '{canonical}'")


def _namespace_of(table: str) -> str:
    """`crm.customers` -> `crm`; `a.b.c` -> `a.b`. Everything but the last segment, because
    Iceberg namespaces nest and only the final element names the table."""
    head, _, _ = table.rpartition(".")
    return head


@dataclass
class PyIcebergCatalog:
    """Adapts a constructed pyiceberg catalog to every port in `base.py`.

    One class implements all seven because pyiceberg can do all seven; that is not the same as the
    ports being one port. What each *caller* is handed is decided by which of `writer_for` /
    `row_writer_for` / `bulk_writer_for` / `edit_log_writer_for` / `load_log_writer_for` /
    `vector_writer_for` it asked, and the type it holds is what bounds what it can do."""

    name: str
    _impl: Any
    _schema_cache: dict[str, TableSchema] = field(default_factory=dict, repr=False)

    def table_exists(self, table: str) -> bool:
        try:
            return bool(self._impl.table_exists(table))
        except Exception:
            # Some catalog implementations raise rather than returning False for a missing
            # namespace. For an existence check that is the same answer.
            return False

    def describe(self, table: str) -> TableSchema:
        cached = self._schema_cache.get(table)
        if cached is not None:
            return cached
        tbl = self._load(table)
        columns = {
            f.name: Column(
                name=f.name,
                iceberg_type=canonical_iceberg_type(f.field_type),
                required=bool(f.required),
                field_id=f.field_id,
            )
            for f in tbl.schema().fields
        }
        schema = TableSchema(table=table, columns=columns)
        self._schema_cache[table] = schema
        return schema

    def scan(
        self,
        table: str,
        columns: Sequence[str] | None = None,
        predicates: Sequence[tuple[str, Any]] = (),
        limit: int | None = None,
    ) -> Any:
        tbl = self._load(table)
        kwargs: dict[str, Any] = {}
        if columns:
            # The primary key column may not be in the projection but can still be needed to
            # join or to filter, so callers pass everything they reference — we just honor it.
            kwargs["selected_fields"] = tuple(columns)
        row_filter = self._row_filter(predicates)
        if row_filter is not None:
            kwargs["row_filter"] = row_filter
        if limit is not None:
            kwargs["limit"] = limit
        try:
            return tbl.scan(**kwargs).to_arrow()
        except Exception as e:  # pragma: no cover - depends on live storage
            raise CatalogError(f"scan of '{table}' in catalog '{self.name}' failed: {e}") from e

    def current_snapshot_id(self, table: str) -> int | None:
        snapshot = self._load(table).current_snapshot()
        return snapshot.snapshot_id if snapshot is not None else None

    def _row_filter(self, predicates: Sequence[tuple[str, Any]]):
        """Lower equality pairs to a pyiceberg expression for file/row-group pruning.

        `IsNull` rather than `EqualTo` for None: Iceberg equality against null never matches,
        which would silently return no rows instead of the null-valued ones."""
        if not predicates:
            return None
        from pyiceberg.expressions import And, EqualTo, IsNull

        terms = [IsNull(col) if val is None else EqualTo(col, val) for col, val in predicates]
        expr = terms[0]
        for t in terms[1:]:
            expr = And(expr, t)
        return expr

    def _load(self, table: str):
        try:
            return self._impl.load_table(table)
        except Exception as e:
            raise CatalogError(f"table '{table}' not found in catalog '{self.name}': {e}") from e

    # --- CatalogWriter -------------------------------------------------------------------
    # Every method below invalidates the introspection cache for the table it touched. A cache
    # that outlives the DDL that stales it is how an `apply` reports a column it never added.

    def ensure_namespace(self, table: str) -> bool:
        namespace = _namespace_of(table)
        if not namespace:
            return False
        try:
            if self._impl.namespace_exists(namespace):
                return False
            self._impl.create_namespace(namespace)
        except Exception as e:
            raise CatalogError(
                f"could not create namespace '{namespace}' in catalog '{self.name}': {e}"
            ) from e
        return True

    def create_table(
        self, table: str, columns: Sequence[Column], properties: Mapping[str, str] = {}
    ) -> None:
        from pyiceberg.schema import Schema
        from pyiceberg.types import NestedField

        # Field ids are assigned here, densely and in declaration order, because this table has no
        # history for them to be compatible with yet. Every later change goes through
        # `alter_table`, where pyiceberg assigns the next id itself — Loom must never reuse one.
        #
        # Nested ids continue the same run, above every top-level one, rather than interleaving with
        # them. That keeps this loop's "the id is the position" property true for the columns a
        # reader can see, and it is why the element ids are allocated from a counter that starts past
        # the last column instead of from `i`.
        nested = itertools.count(len(columns) + 1)
        schema = Schema(
            *(
                NestedField(
                    field_id=i,
                    name=col.name,
                    field_type=iceberg_type(
                        col.iceberg_type,
                        element_id=next(nested) if col.iceberg_type.startswith("list<") else None,
                    ),
                    required=col.required,
                )
                for i, col in enumerate(columns, start=1)
            )
        )
        try:
            self._impl.create_table(table, schema=schema, properties=dict(properties))
        except Exception as e:
            raise CatalogError(f"could not create table '{table}' in catalog '{self.name}': {e}") from e
        self._schema_cache.pop(table, None)

    def alter_table(
        self, table: str, edits: Sequence[SchemaEdit], properties: Mapping[str, str] = {}
    ) -> None:
        if not edits and not properties:
            return
        tbl = self._load(table)
        try:
            # One transaction for the schema edits *and* the properties: Iceberg commits a single
            # new metadata version, so a reader either sees the whole migration or none of it.
            # This is as atomic as apply gets — Iceberg has no cross-table transaction, which is
            # why the executor above sequences tables and reports what landed.
            with tbl.transaction() as txn:
                if properties:
                    txn.set_properties(**dict(properties))
                with txn.update_schema() as update:
                    # `UpdateSchema` resolves every `path=` against the schema the transaction
                    # opened with, not against the edits accumulated so far — so after a rename,
                    # a promotion of that same column must *still* name the old column, and asking
                    # for the new one raises "Could not find field with name ...". This map is that
                    # translation, and it is why `alter_table` requires renames to arrive first.
                    renamed: dict[str, str] = {}
                    for edit in edits:
                        self._edit(update, edit, renamed)
        except Exception as e:
            raise CatalogError(f"could not alter '{table}' in catalog '{self.name}': {e}") from e
        self._schema_cache.pop(table, None)

    def _edit(self, update: Any, edit: SchemaEdit, renamed: dict[str, str]) -> None:
        col = edit.column
        # Everything but `add` addresses a column that already exists, so it goes through the
        # pre-rename name when this batch has renamed it. `add` never does — the whole point of an
        # add is that no field is there yet.
        path = renamed.get(col.name, col.name)
        if edit.op == "add":
            # Never `required=True`: pyiceberg rejects it without allow_incompatible_changes, and
            # so does Loom — an added required column is classified breaking and never reaches here.
            update.add_column(path=col.name, field_type=iceberg_type(col.iceberg_type))
        elif edit.op == "rename":
            update.rename_column(edit.renamed_from, col.name)
            renamed[col.name] = edit.renamed_from
        elif edit.op == "promote":
            update.update_column(path=path, field_type=iceberg_type(col.iceberg_type))
        elif edit.op == "relax":
            update.make_column_optional(path)
        else:  # pragma: no cover - the executor builds these, so this is a programming error
            raise CatalogError(f"unsupported schema edit '{edit.op}' on column '{col.name}'")

    def append_rows(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        tbl = self._load(table)
        try:
            tbl.append(self._batch(tbl, rows))
        except Exception as e:
            raise CatalogError(f"could not append to '{table}' in catalog '{self.name}': {e}") from e

    @staticmethod
    def _batch(tbl: Any, rows: Sequence[Mapping[str, Any]]):
        """Rows to an Arrow table, against the table's *own* schema.

        Never one inferred from the values: inference would turn an all-null column into a
        null-typed one and an int into an int64, and the write would be rejected for a mismatch
        that says nothing about what the caller got wrong. It is also what carries a column whose
        type Loom has no name for — a `list`, a `struct` — straight back out again, because the
        conversion is driven by the physical schema rather than by anything the ontology knows."""
        import pyarrow as pa
        from pyiceberg.io.pyarrow import schema_to_pyarrow

        return pa.Table.from_pylist([dict(r) for r in rows], schema=schema_to_pyarrow(tbl.schema()))

    # --- RowWriter -----------------------------------------------------------------------
    # One row, addressed by key. No batch verb and no predicate: the spec's single-object boundary
    # is enforced here by the absence of a way to express anything wider. Every verb goes through
    # `_guarded`, so there is no path through this class that writes a row without asserting the
    # snapshot its caller read — and every verb passes `commit_properties` down to pyiceberg as
    # `snapshot_properties`, so the record of *who* wrote and *why* lands in the summary of the very
    # snapshot the write produces. That is the only attribution here that is atomic with the write;
    # the edit-log row below is a separate commit and is honest about it.

    def insert_row(
        self,
        table: str,
        row: Mapping[str, Any],
        *,
        expect_snapshot_id: int | None,
        commit_properties: Mapping[str, str],
    ) -> None:
        with self._guarded(table, expect_snapshot_id, "insert into") as (tbl, txn):
            txn.append(self._batch(tbl, [row]), snapshot_properties=dict(commit_properties))

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
        with self._guarded(table, expect_snapshot_id, "replace a row in") as (tbl, txn):
            # `overwrite` inside a transaction *is* the equality-delete plus append: pyiceberg
            # drops or rewrites the files matching the filter and adds the new row, and the whole
            # thing lands as one Iceberg commit. A reader sees the old row or the new one — never
            # neither, and never both.
            txn.overwrite(
                self._batch(tbl, [row]),
                overwrite_filter=self._key_filter(key_column, key_value),
                snapshot_properties=dict(commit_properties),
            )

    def delete_row(
        self,
        table: str,
        key_column: str,
        key_value: Any,
        *,
        expect_snapshot_id: int | None,
        commit_properties: Mapping[str, str],
    ) -> None:
        with self._guarded(table, expect_snapshot_id, "delete a row from") as (_tbl, txn):
            txn.delete(
                delete_filter=self._key_filter(key_column, key_value),
                snapshot_properties=dict(commit_properties),
            )

    @contextmanager
    def _guarded(self, table: str, expect_snapshot_id: int | None, doing: str):
        """One row write, as one Iceberg commit that asserts the snapshot its caller read.

        The assertion is a `TableRequirement` on the transaction, not a comparison in this process,
        and that distinction is the whole of the port's promise. pyiceberg hands the requirements to
        the catalog with the updates; the catalog validates them against metadata it re-reads
        itself and then swaps the metadata pointer conditionally on the location it validated
        against. A commit that lands in between loses. There is no window here to narrow.

        Two orderings are load-bearing:

        - The requirement is staged **before** the caller's write op. pyiceberg keeps at most one
          requirement per type, first one in winning, and every snapshot-producing update stages an
          `AssertRefSnapshotId` of its own carrying the snapshot the *transaction* opened at. Going
          first is what replaces that with the snapshot the *read* saw — the difference between
          asserting against a table we loaded a microsecond ago and asserting against the row we
          actually evaluated the rules on.
        - The staged requirement is re-checked before the commit. That guarantee rests on a
          library's deduplication rule, so if a pyiceberg release ever changes it the write must
          fail loudly rather than quietly commit under the weaker assertion.
        """
        from pyiceberg.exceptions import CommitFailedException
        from pyiceberg.table.update import AssertRefSnapshotId

        tbl = self._load(table)
        # `tbl.transaction()` bare rather than `with tbl.transaction()`: the context manager commits
        # on the way out, including on the way out of a failure, and the one thing every refusal here
        # has to promise is that nothing was written.
        txn = tbl.transaction()
        if not hasattr(txn, "_stage"):  # pragma: no cover - guards a pyiceberg API change
            raise CatalogError(
                f"refusing to write '{table}' in catalog '{self.name}': this pyiceberg cannot be "
                f"asked to assert a snapshot on a transaction, so the write would commit without a "
                f"concurrency check"
            )
        try:
            txn._stage((), (AssertRefSnapshotId(ref=_MAIN, snapshot_id=expect_snapshot_id),))
            yield tbl, txn
            self._still_asserted(txn, table, expect_snapshot_id)
            txn.commit_transaction()
        except CommitFailedException as e:
            # Every requirement on this transaction is about the table having moved — ours, and the
            # table-uuid one `commit_transaction` adds, which fails when the table was replaced
            # wholesale. The metastore's own "updated by another process" arrives here too.
            raise self._conflict(table, expect_snapshot_id, e) from e
        except CatalogError:
            raise
        except Exception as e:
            raise CatalogError(f"could not {doing} '{table}' in catalog '{self.name}': {e}") from e

    @staticmethod
    def _still_asserted(txn: Any, table: str, expected: int | None) -> None:
        from pyiceberg.table.update import AssertRefSnapshotId

        staged = [r for r in txn._requirements if isinstance(r, AssertRefSnapshotId)]
        if len(staged) == 1 and staged[0].ref == _MAIN and staged[0].snapshot_id == expected:
            return
        raise CatalogError(  # pragma: no cover - guards a pyiceberg API change
            f"refusing to write '{table}': the snapshot assertion for {expected!r} did not survive "
            f"onto the transaction (found {[(r.ref, r.snapshot_id) for r in staged]!r}). "
            f"pyiceberg's requirement handling has changed and this write would have committed "
            f"under a weaker check, or none"
        )

    def _conflict(self, table: str, expected: int | None, cause: Exception) -> ConcurrencyError:
        """The refusal, with the snapshot the table is at *now* attached as a diagnosis.

        Best-effort and deliberately not authoritative: it is read after the refusal, so on a busy
        table it may already be newer than the commit that actually won. It is there to tell an
        agent 'the table moved' apart from 'the table is moving constantly', not to be branched on.
        """
        try:
            found = self.current_snapshot_id(table)
        except CatalogError:  # pragma: no cover - the table was there a moment ago
            found = None
        return ConcurrencyError(
            f"'{table}' in catalog '{self.name}' moved between the read and the write "
            f"(expected snapshot {expected}, found {found}): {cause}",
            table=table,
            expected=expected,
            found=found,
        )

    # --- BulkWriter ----------------------------------------------------------------------
    # Many rows, one commit each. No DDL verb reaches this section — a batch that does not fit the
    # table's schema fails in `_batch`, which builds against the table's *own* schema, so a column
    # the table does not have is an error rather than a column quietly added.

    def append_batch(
        self,
        table: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        commit_properties: Mapping[str, str],
    ) -> None:
        """Every row, one commit, no assertion — `append_edit`'s shape at batch scale.

        Deliberately not routed through `_guarded`: there is no snapshot to assert because the
        caller read nothing and this appends over nothing, and manufacturing an expectation here
        would make two pipelines loading the same table refuse each other for a race neither can
        lose."""
        if not rows:
            return
        tbl = self._load(table)
        try:
            tbl.append(self._batch(tbl, rows), snapshot_properties=dict(commit_properties))
        except Exception as e:
            raise CatalogError(
                f"could not append {len(rows)} row(s) to '{table}' in catalog '{self.name}': {e}"
            ) from e

    def merge_batch(
        self,
        table: str,
        key_column: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        expect_snapshot_id: int | None,
        commit_properties: Mapping[str, str],
    ) -> None:
        """Equality-delete on the batch's keys plus an append of the batch, as one commit.

        The same thing `replace_row` does to one row, over a set of them: `overwrite` inside a
        transaction drops or rewrites the files matching the filter and adds the new rows, and the
        whole thing lands as one Iceberg commit. A reader sees the whole old set or the whole new
        one — never a mixture, and never a key twice."""
        if not rows:
            return
        with self._guarded(table, expect_snapshot_id, "merge rows into") as (tbl, txn):
            txn.overwrite(
                self._batch(tbl, rows),
                overwrite_filter=self._keys_filter(key_column, [r.get(key_column) for r in rows]),
                snapshot_properties=dict(commit_properties),
            )

    def replace_table(
        self,
        table: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        expect_snapshot_id: int | None,
        commit_properties: Mapping[str, str],
    ) -> None:
        """The table's whole contents become `rows`, as one commit.

        An empty batch takes the `delete` branch rather than the `overwrite` one, because an
        overwrite with nothing to write has no rows to derive an Arrow schema from and would either
        raise or quietly do nothing — and *quietly nothing* is the worst available answer to "make
        this table empty"."""
        from pyiceberg.expressions import AlwaysTrue

        with self._guarded(table, expect_snapshot_id, "replace the contents of") as (tbl, txn):
            if rows:
                txn.overwrite(
                    self._batch(tbl, rows),
                    overwrite_filter=AlwaysTrue(),
                    snapshot_properties=dict(commit_properties),
                )
            else:
                txn.delete(
                    delete_filter=AlwaysTrue(), snapshot_properties=dict(commit_properties)
                )

    @staticmethod
    def _keys_filter(key_column: str, values: Sequence[Any]):
        """`key_column IN (…)`, with nulls disjoined in rather than dropped.

        `_key_filter`'s rule at batch scale, and it matters more here: Iceberg's `In` never matches a
        null, exactly as SQL's `IN` does not, so a null key folded into the list would delete nothing
        while the append beside it added the row — turning a merge into a duplicate. The ingest
        runtime refuses a null key before it ever gets here; this stays correct anyway, because a
        port that is only safe when its caller is careful is not safe."""
        from pyiceberg.expressions import AlwaysFalse, In, IsNull, Or

        present = [v for v in values if v is not None]
        expr: Any = In(key_column, present) if present else AlwaysFalse()
        if len(present) != len(values):
            expr = IsNull(key_column) if isinstance(expr, AlwaysFalse) else Or(expr, IsNull(key_column))
        return expr

    # --- EditLogWriter -------------------------------------------------------------------

    def ensure_log(self, columns: Sequence[Column]) -> None:
        """The create half of `append_edit`, callable on its own so a deployment can prove it works.

        The one piece of DDL reachable from the action runtime, bounded by the port rather than by a
        check in here: neither method takes a table name, so `EDIT_LOG_TABLE` is the only thing
        either can ever create."""
        self._ensure_meta_table(EDIT_LOG_TABLE, columns)

    def append_edit(self, columns: Sequence[Column], row: Mapping[str, Any]) -> None:
        """One record into `EDIT_LOG_TABLE`, which this method creates if it is not there.

        Deliberately *not* routed through `_guarded`: there is no snapshot to assert, because the
        caller read nothing and this appends over nothing. Routing it there to reuse the plumbing
        would have manufactured an expectation nobody holds, and made the log table's own traffic
        able to refuse a write."""
        self._append_meta_row(EDIT_LOG_TABLE, columns, row, "record an edit in")

    # --- LoadLogWriter -------------------------------------------------------------------

    def ensure_load_log(self, columns: Sequence[Column]) -> None:
        """`ensure_log` for the other log. Bounded by the port rather than by a check in here:
        neither verb takes a table name, so `LOAD_LOG_TABLE` is the only thing either can create."""
        self._ensure_meta_table(LOAD_LOG_TABLE, columns)

    def append_load(self, columns: Sequence[Column], row: Mapping[str, Any]) -> None:
        """One record into `LOAD_LOG_TABLE`, which this method creates if it is not there.

        Unguarded for `append_edit`'s reason: nothing was read, nothing is being written over, and
        routing it through `_guarded` would let the log table's own traffic refuse the record of a
        load that already committed."""
        self._append_meta_row(LOAD_LOG_TABLE, columns, row, "record a load in")

    # --- VectorWriter --------------------------------------------------------------------
    # Many rows, keyed, in a table this class derives from an object type name. The only section
    # here that both writes over existing rows *and* removes them — see `base.VectorWriter` for why
    # a plane of derived data gets verbs the two record planes are permanently denied.

    def ensure_vectors(self, object_type: str, columns: Sequence[Column]) -> None:
        self._ensure_meta_table(vector_table(object_type), columns)

    def merge_vectors(
        self,
        object_type: str,
        columns: Sequence[Column],
        rows: Sequence[Mapping[str, Any]],
        *,
        expect_snapshot_id: int | None,
    ) -> None:
        """`merge_batch` against a table nobody named, keyed on the column this port defines.

        The commit carries no properties, and that is the difference between derived data and a
        write. `merge_batch` stamps `loom.load_id` and an actor into its snapshot because somebody
        decided to load that batch and the record of who has to be atomic with it. Nothing was
        decided here: a vector is a function of text that is already in the lake, and the honest
        answer to *who wrote this* is the same reconcile that would write it again from scratch."""
        if not rows:
            return
        table = vector_table(object_type)
        self._ensure_meta_table(table, columns)
        with self._guarded(table, expect_snapshot_id, "merge vectors into") as (tbl, txn):
            txn.overwrite(
                self._batch(tbl, rows),
                overwrite_filter=self._keys_filter(VECTOR_KEY_COLUMN, [r.get(VECTOR_KEY_COLUMN) for r in rows]),
            )

    def delete_vectors(self, object_type: str, keys: Sequence[Any]) -> None:
        """The equality-delete half of a merge, with nothing appended after it.

        Deliberately not routed through `_guarded` — `append_batch`'s posture, arrived at from the
        other direction. There the caller read nothing; here the caller read nothing *its correctness
        depends on*, and an assertion would let a concurrent reconcile refuse an erasure.

        An empty `keys` returns without opening a transaction rather than deleting nothing, because
        `_keys_filter` renders it as `AlwaysFalse` and a commit that provably changes no row is a
        snapshot in the history saying an erasure happened. There was no erasure."""
        if not keys:
            return
        table = vector_table(object_type)
        try:
            # `self._impl` rather than `self.table_exists`, which is the read port's *existence
            # check* and swallows every exception to answer False. That is right for a probe and
            # catastrophic here: an unreachable metastore would read as "no sidecar", this would
            # return quietly, and the delete action that called it would report `applied` over a
            # vector that still exists. The one verb whose failure must be loud cannot be built on
            # the one method that cannot fail.
            present = bool(self._impl.table_exists(table))
        except Exception as e:
            raise CatalogError(
                f"could not determine whether '{table}' exists in catalog '{self.name}', so the "
                f"vectors it may hold cannot be shown to be gone: {e}"
            ) from e
        if not present:
            # Nothing was ever embedded for this type, so there is nothing to prune. Creating the
            # sidecar in order to delete from it would make a read-only reconcile write DDL.
            return
        tbl = self._load(table)
        try:
            tbl.delete(delete_filter=self._keys_filter(VECTOR_KEY_COLUMN, list(keys)))
        except Exception as e:
            raise CatalogError(
                f"could not delete {len(keys)} vector(s) from '{table}' in catalog "
                f"'{self.name}': {e}"
            ) from e

    # --- shared by both log ports --------------------------------------------------------

    def _ensure_meta_table(self, table: str, columns: Sequence[Column]) -> None:
        if self.table_exists(table):
            return
        self.ensure_namespace(table)
        self.create_table(table, columns, properties={"loom.managed": "true"})

    def _append_meta_row(
        self, table: str, columns: Sequence[Column], row: Mapping[str, Any], doing: str
    ) -> None:
        self._ensure_meta_table(table, columns)
        tbl = self._load(table)
        try:
            tbl.append(self._batch(tbl, [row]))
        except Exception as e:
            raise CatalogError(
                f"could not {doing} '{table}' in catalog '{self.name}': {e}"
            ) from e

    @staticmethod
    def _key_filter(key_column: str, key_value: Any):
        """`IsNull` rather than `EqualTo` for None, for the same reason `_row_filter` does it: an
        Iceberg equality against null never matches, so a null key would delete nothing and the
        append beside it would then duplicate the row instead of replacing it."""
        from pyiceberg.expressions import EqualTo, IsNull

        return IsNull(key_column) if key_value is None else EqualTo(key_column, key_value)


def build(name: str, ctype: str, uri: str, warehouse: str | None, properties: Mapping[str, object]):
    """Construct a pyiceberg catalog for a `CatalogConfig`. Called only by the factory."""
    props: dict[str, str] = {str(k): str(v) for k, v in properties.items()}
    props["uri"] = uri
    if warehouse:
        props["warehouse"] = warehouse

    try:
        if ctype == "iceberg-sql":
            from pyiceberg.catalog.sql import SqlCatalog

            impl: Any = SqlCatalog(name, **props)
        elif ctype == "iceberg-rest":
            from pyiceberg.catalog.rest import RestCatalog

            impl = RestCatalog(name, **props)
        else:  # pragma: no cover - config validation rejects unknown types first
            raise CatalogError(f"unsupported catalog type '{ctype}'")
    except ImportError as e:
        raise CatalogError(
            f"catalog '{name}' needs pyiceberg — install the extra: pip install 'loom-ontology[iceberg]' ({e})"
        ) from e
    except CatalogError:
        raise
    except Exception as e:
        raise CatalogError(f"could not open catalog '{name}' ({ctype}): {e}{_local_hint(ctype, uri)}") from e

    return PyIcebergCatalog(name=name, _impl=impl)


def _local_hint(ctype: str, uri: str) -> str:
    """Turn SQLite's "unable to open database file" into something actionable.

    A local `iceberg-sql` catalog is the first thing anyone runs, and the underlying error names
    neither the path nor the reason — an absent warehouse directory looks identical to a permissions
    problem. Loom won't create the directory itself (a warehouse is the user's data, not ours), so
    the least it can do is say which one is missing."""
    prefix = "sqlite:///"
    if ctype != "iceberg-sql" or not uri.startswith(prefix):
        return ""
    parent = Path(uri[len(prefix):]).parent
    if parent.parts and not parent.is_dir():
        return f"\n  hint: the warehouse directory '{parent}' does not exist — create it, or run your seed step first"
    return ""
