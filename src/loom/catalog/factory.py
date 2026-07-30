"""Config -> live catalogs.

The one place that knows which `CatalogConfig.type` maps to which implementation. Kept apart from
the implementation so adding a non-Iceberg backing store is a change here plus a new module,
with nothing above the port touched.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..config import CatalogConfig, LoomConfig
from .base import Catalog, CatalogError

__all__ = ["CatalogError", "open_catalog", "open_catalogs"]


def open_catalog(cfg: CatalogConfig) -> Catalog:
    """Open a single catalog.

    Raises CatalogError if the catalog can't be opened. Note that how much work this does varies by
    type: pyiceberg's REST catalog fetches the server's config during construction, so an
    unreachable or misconfigured metastore fails *here*, while the SQL catalog only touches its
    metastore on first use. Callers should therefore treat opening as a fallible operation."""
    from . import pyiceberg_catalog

    if cfg.type in ("iceberg-sql", "iceberg-rest"):
        return pyiceberg_catalog.build(cfg.name, cfg.type, cfg.uri, cfg.warehouse, cfg.properties)
    raise CatalogError(f"no catalog implementation for type '{cfg.type}'")


def open_catalogs(config: LoomConfig) -> Mapping[str, Catalog]:
    """Open every catalog named in a project config, keyed by the name the ontology's
    `backing.catalog` refers to."""
    return {name: open_catalog(cfg) for name, cfg in config.catalogs.items()}
