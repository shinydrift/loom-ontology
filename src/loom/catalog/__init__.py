"""The catalog layer — layer 1 of the architecture, behind a port.

Everything above this package (physical validation, the query engines, the resolver) talks to
`Catalog`, never to pyiceberg directly. That's what lets the same ontology run against a local
SQLite-backed warehouse in a test and a REST catalog in production, and it's where a
non-Iceberg backing store would eventually plug in.

The read port is deliberately narrow: introspect a table's columns, scan it, and ask which snapshot
you just read. Writes are two *separate* ports, so holding a `Catalog` — which is all the resolver,
the engines and `loom serve` are ever given — carries no ability to write at all, and holding one
writer carries no ability to do the other's job. `loom apply` reaches for `writer_for()` and gets
schema verbs only; the action runtime reaches for `row_writer_for()` and gets row verbs only.
"""

from __future__ import annotations

from .base import (
    Catalog,
    CatalogError,
    CatalogWriter,
    Column,
    RowWriter,
    SchemaEdit,
    TableSchema,
    row_writer_for,
    writer_for,
)
from .factory import open_catalog, open_catalogs

__all__ = [
    "Catalog",
    "CatalogError",
    "CatalogWriter",
    "Column",
    "RowWriter",
    "SchemaEdit",
    "TableSchema",
    "open_catalog",
    "open_catalogs",
    "row_writer_for",
    "writer_for",
]
