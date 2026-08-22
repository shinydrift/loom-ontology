"""The catalog layer — layer 1 of the architecture, behind a port.

Everything above this package (physical validation, the query engines, the resolver) talks to
`Catalog`, never to pyiceberg directly. That's what lets the same ontology run against a local
SQLite-backed warehouse in a test and a REST catalog in production, and it's where a
non-Iceberg backing store would eventually plug in.

The read port is deliberately narrow: introspect a table's columns, scan it, and ask which snapshot
you just read. Writes are five *separate* ports, so holding a `Catalog` — which is all the resolver,
the engines and `loom serve` are ever given — carries no ability to write at all, and holding one
writer carries no ability to do another's job. `loom apply` reaches for `writer_for()` and gets
schema verbs only; the action runtime reaches for `row_writer_for()` and gets one-row verbs only,
plus `edit_log_writer_for()`, which is one append to one table the port itself names; a declared
ingest reaches for `bulk_writer_for()` and gets batch verbs and no DDL, plus `load_log_writer_for()`
on the same terms.

Every `RowWriter` verb takes the snapshot its caller read and asserts it *inside* the commit, so a
read-then-write behaves as one decision without the port ever growing a lock, a session, or a verb
that spans two tables. `BulkWriter` carries the same assertion on the two verbs that follow a read
and deliberately omits it from the one that does not. A backend that cannot express that assertion
must refuse rather than approximate one — the ports promise a closed race, not a narrowed one — and a
write that loses comes back as `ConcurrencyError`, which the action runtime turns into a `conflict`.
Every write verb also carries `commit_properties` into that same commit, which is the only way a
record of a write can be atomic with it; both logs are second commits, and say so.
"""

from __future__ import annotations

from .base import (
    EDIT_LOG_TABLE,
    LOAD_LOG_TABLE,
    BulkWriter,
    Catalog,
    CatalogError,
    CatalogWriter,
    Column,
    ConcurrencyError,
    EditLogWriter,
    LoadLogWriter,
    RowWriter,
    SchemaEdit,
    TableSchema,
    bulk_writer_for,
    edit_log_writer_for,
    load_log_writer_for,
    row_writer_for,
    writer_for,
)
from .factory import open_catalog, open_catalogs

__all__ = [
    "EDIT_LOG_TABLE",
    "LOAD_LOG_TABLE",
    "BulkWriter",
    "Catalog",
    "CatalogError",
    "CatalogWriter",
    "Column",
    "ConcurrencyError",
    "EditLogWriter",
    "LoadLogWriter",
    "RowWriter",
    "SchemaEdit",
    "TableSchema",
    "bulk_writer_for",
    "edit_log_writer_for",
    "load_log_writer_for",
    "open_catalog",
    "open_catalogs",
    "row_writer_for",
    "writer_for",
]
