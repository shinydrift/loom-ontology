"""pyiceberg-backed implementation of the `Catalog` port.

One class serves both `iceberg-rest` and `iceberg-sql`: pyiceberg already abstracts the metastore
difference, so the only thing that varies is construction (see factory.py). Anything genuinely
catalog-specific belongs there, not here.

pyiceberg is imported lazily, inside methods, so that `import loom` and a structural
`loom validate` stay dependency-free — the spec module has no business requiring an Iceberg stack.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import CatalogError, Column, TableSchema


def canonical_iceberg_type(t: object) -> str:
    """pyiceberg's spelling -> the spelling `PropType.iceberg_type()` produces.

    Only `decimal` actually differs (pyiceberg renders `decimal(12, 2)`), but normalizing all of
    them through one function keeps the comparison in physical validation a plain string equality
    and gives a single place to fix the next divergence."""
    return str(t).replace(" ", "")


@dataclass
class PyIcebergCatalog:
    """Adapts a constructed pyiceberg catalog to the `Catalog` port."""

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
