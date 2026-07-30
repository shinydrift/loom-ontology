"""The catalog layer — layer 1 of the architecture, behind a port.

Everything above this package (physical validation, the query engines, the resolver) talks to
`Catalog`, never to pyiceberg directly. That's what lets the same ontology run against a local
SQLite-backed warehouse in a test and a REST catalog in production, and it's where a
non-Iceberg backing store would eventually plug in.

The port is deliberately narrow: introspect a table's columns, and scan it. Writes are not here
— they arrive with the action runtime (M3) and go through the catalog rather than the engine.
"""

from __future__ import annotations

from .base import Catalog, CatalogError, Column, TableSchema
from .factory import open_catalog, open_catalogs

__all__ = [
    "Catalog",
    "CatalogError",
    "Column",
    "TableSchema",
    "open_catalog",
    "open_catalogs",
]
