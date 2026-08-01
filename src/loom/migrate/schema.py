"""Ontology Model -> the physical tables it wants to exist.

This is the "desired state" half of `loom plan`. It reads nothing and connects to nothing: given
a validated `Ontology`, it works out which `(catalog, table)` pairs the spec implies and what
columns each needs. `diff.py` then holds that up against the live catalog.

Two things contribute a table: an objectType's `backing`, and a linkType's `through` mapping
table. Several declarations can land on the *same* table — two objectTypes over `crm.customers`
is a normal way to model a subtype — so columns are merged rather than overwritten.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from ..errors import Diagnostics
from ..model import LinkType, ObjectType, Ontology, check_renames, physical_type

TableKey = tuple[str, str]  # (catalog name, dotted table identifier)


@dataclass(frozen=True)
class DesiredColumn:
    """One column the spec needs. `source` names the declaration that asked for it, so a diff
    line can say *why* a column is wanted without the reader going back to the YAML."""

    name: str
    iceberg_type: str
    required: bool
    source: str
    renamed_from: str | None = None
    """The column this one used to be, if the declaration said so. Carried down here rather than
    resolved in the loader because whether it means anything is a question about the *live* table,
    which only the diff can answer."""


@dataclass(frozen=True)
class DesiredTable:
    catalog: str
    table: str
    columns: Mapping[str, DesiredColumn]  # keyed by column name, declaration order
    sources: tuple[str, ...]  # the declarations backing this table, in declaration order

    @property
    def key(self) -> TableKey:
        return (self.catalog, self.table)


def desired_tables(ontology: Ontology, diag: Diagnostics) -> dict[TableKey, DesiredTable]:
    """Every table the ontology implies, keyed by `(catalog, table)` in declaration order.

    Determinism matters here: the plan is printed for a human to read and diffed by CI, so the
    same spec must always produce the same table and column ordering. Dicts preserve insertion
    order and the Ontology Model is itself insertion-ordered, so iterating it is enough.
    """
    tables: dict[TableKey, DesiredTable] = {}
    for obj in ontology.object_types.values():
        _add_object(obj, ontology, tables, diag)
    for link in ontology.link_types.values():
        _add_through(link, ontology, tables, diag)
    for table in tables.values():
        # The rename rules again, now at the scope they are really about. The validator applied
        # them per declaration, which catches the everyday case offline; only here is the whole
        # table in scope, so only here can two declarations sharing a table be caught disagreeing.
        check_renames(
            {c.name: (c.renamed_from, f"'{c.source}'") for c in table.columns.values()},
            diag,
            ctx=f"table '{table.table}'",
        )
    return tables


def _add_object(
    obj: ObjectType, ontology: Ontology, tables: dict[TableKey, DesiredTable], diag: Diagnostics
) -> None:
    for prop in obj.properties.values():
        stored = physical_type(prop.type, ontology.object_types)
        if stored is None:
            # An objectRef whose target doesn't exist. The referential pass already reported it;
            # planning a column whose type we can't name would only add noise.
            continue
        _merge(
            tables,
            obj.backing_catalog,
            obj.backing_table,
            DesiredColumn(
                prop.column,
                stored,
                required=not prop.nullable,
                source=f"{obj.api_name}.{prop.name}",
                renamed_from=prop.renamed_from,
            ),
            owner=obj.api_name,
            diag=diag,
        )


def _add_through(
    link: LinkType, ontology: Ontology, tables: dict[TableKey, DesiredTable], diag: Diagnostics
) -> None:
    """A many-to-many `through` table needs one column per side, typed as the property it joins to.

    Both sides are required: a mapping row with a null end joins to nothing, so it is never the
    shape anyone wants — unlike an ordinary optional property, where nullable is the norm."""
    if link.through is None:
        return
    sides = (
        (link.through.from_column, link.frm, link.through.from_renamed_from),
        (link.through.to_column, link.to, link.through.to_renamed_from),
    )
    for column, end, renamed_from in sides:
        obj = ontology.object_types.get(end.object_type)
        if obj is None:  # unresolvable end — reported by the referential pass
            continue
        prop = obj.properties.get(end.property)
        if prop is None:
            continue
        stored = physical_type(prop.type, ontology.object_types)
        if stored is None:
            continue
        _merge(
            tables,
            link.through.catalog,
            link.through.table,
            DesiredColumn(
                column,
                stored,
                required=True,
                source=f"{link.api_name}.{end.object_type}",
                renamed_from=renamed_from,
            ),
            owner=link.api_name,
            diag=diag,
        )


def _merge(
    tables: dict[TableKey, DesiredTable],
    catalog: str,
    table: str,
    column: DesiredColumn,
    owner: str,
    diag: Diagnostics,
) -> None:
    key = (catalog, table)
    existing = tables.get(key)
    if existing is None:
        tables[key] = DesiredTable(catalog, table, {column.name: column}, (owner,))
        return

    columns = dict(existing.columns)
    sources = existing.sources if owner in existing.sources else (*existing.sources, owner)
    prior = columns.get(column.name)
    columns[column.name] = column if prior is None else _reconcile(prior, column, table, diag)
    tables[key] = replace(existing, columns=columns, sources=sources)


def _reconcile(prior: DesiredColumn, new: DesiredColumn, table: str, diag: Diagnostics) -> DesiredColumn:
    """Two declarations want the same physical column.

    Nullability reconciles cleanly: only a column that *every* contributor treats as non-nullable
    can be required, so the weaker constraint wins. A type disagreement doesn't reconcile at all —
    no single column can store both — so it is reported and the first declaration is kept, which
    keeps the rest of the plan readable instead of aborting the whole run over one column.

    `renamedFrom` reconciles by *assertion*: silence is no opinion, not "there was no rename". A
    subtype modelled as a second objectType over the same table is normally written well after the
    rename shipped, and making it repeat the scaffolding to agree would be scaffolding that spreads.
    Two declarations naming two different old columns is the one case that can't be reconciled —
    the column came from one place — so it is reported like a type disagreement."""
    if prior.iceberg_type != new.iceberg_type:
        diag.error(
            f"'{prior.source}' and '{new.source}' both map column '{new.name}' of '{table}' but "
            f"disagree on its type ({prior.iceberg_type} vs {new.iceberg_type})",
            hint="one column stores one type — split the table or align the property types",
        )
        return prior
    renamed_from = prior.renamed_from or new.renamed_from
    if prior.renamed_from and new.renamed_from and prior.renamed_from != new.renamed_from:
        diag.error(
            f"'{prior.source}' and '{new.source}' both map column '{new.name}' of '{table}' but "
            f"disagree on where it was renamed from ('{prior.renamed_from}' vs '{new.renamed_from}')",
            hint="a column was renamed from one place — drop the 'renamedFrom' that is wrong",
        )
        renamed_from = prior.renamed_from
    return replace(prior, required=prior.required and new.required, renamed_from=renamed_from)
