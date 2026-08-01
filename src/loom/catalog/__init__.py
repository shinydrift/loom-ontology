"""The catalog layer — layer 1 of the architecture, behind a port.

Everything above this package (physical validation, the query engines, the resolver) talks to
`Catalog`, never to pyiceberg directly. That's what lets the same ontology run against a local
SQLite-backed warehouse in a test and a REST catalog in production, and it's where a
non-Iceberg backing store would eventually plug in.

The read port is deliberately narrow: introspect a table's columns, and scan it. Writes are a
*separate* port, `CatalogWriter`, so that holding a `Catalog` — which is all the resolver, the
engines and `loom serve` are ever given — carries no ability to execute DDL. `loom apply` reaches
for `writer_for()` explicitly; row-level writes join it with the action runtime (M3).
"""

from __future__ import annotations

from .base import Catalog, CatalogError, CatalogWriter, Column, SchemaEdit, TableSchema, writer_for
from .factory import open_catalog, open_catalogs

__all__ = [
    "Catalog",
    "CatalogError",
    "CatalogWriter",
    "Column",
    "SchemaEdit",
    "TableSchema",
    "open_catalog",
    "open_catalogs",
    "writer_for",
]
