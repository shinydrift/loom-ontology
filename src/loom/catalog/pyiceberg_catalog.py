"""pyiceberg-backed implementation of the `Catalog` port.

One class serves both `iceberg-rest` and `iceberg-sql`: pyiceberg already abstracts the metastore
difference, so the only thing that varies is construction (see factory.py). Anything genuinely
catalog-specific belongs there, not here.

pyiceberg is imported lazily, inside methods, so that `import loom` and a structural
`loom validate` stay dependency-free — the spec module has no business requiring an Iceberg stack.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import EDIT_LOG_TABLE, CatalogError, Column, ConcurrencyError, SchemaEdit, TableSchema

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


def iceberg_type(canonical: str):
    """The inverse: the type system's spelling -> a pyiceberg type object, for DDL.

    Only the spellings `PropType.iceberg_type()` can produce are here, plus the two the physical
    side already tolerates on a column Loom didn't create (`float`, naive `timestamp`). Anything
    else is a bug above this line rather than a user error, but it still gets a message that names
    the spelling instead of a KeyError."""
    from pyiceberg import types as t

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
    """Adapts a constructed pyiceberg catalog to the `Catalog`, `CatalogWriter` and `RowWriter`
    ports.

    One class implements all three because pyiceberg can do all three; that is not the same as the
    ports being one port. What each *caller* is handed is decided by which of `writer_for` /
    `row_writer_for` it asked, and the type it holds is what bounds what it can do."""

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
        schema = Schema(
            *(
                NestedField(
                    field_id=i,
                    name=col.name,
                    field_type=iceberg_type(col.iceberg_type),
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

    # --- EditLogWriter -------------------------------------------------------------------

    def append_edit(self, columns: Sequence[Column], row: Mapping[str, Any]) -> None:
        """One record into `EDIT_LOG_TABLE`, which this method creates if it is not there.

        Deliberately *not* routed through `_guarded`: there is no snapshot to assert, because the
        caller read nothing and this appends over nothing. Routing it there to reuse the plumbing
        would have manufactured an expectation nobody holds, and made the log table's own traffic
        able to refuse a write.

        The create is the one piece of DDL reachable from the action runtime, and it is bounded by
        the port rather than by a check in here: the method takes no table name, so `EDIT_LOG_TABLE`
        is the only thing it can ever create."""
        if not self.table_exists(EDIT_LOG_TABLE):
            self.ensure_namespace(EDIT_LOG_TABLE)
            self.create_table(EDIT_LOG_TABLE, columns, properties={"loom.managed": "true"})
        tbl = self._load(EDIT_LOG_TABLE)
        try:
            tbl.append(self._batch(tbl, [row]))
        except Exception as e:
            raise CatalogError(
                f"could not record an edit in '{EDIT_LOG_TABLE}' in catalog '{self.name}': {e}"
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
