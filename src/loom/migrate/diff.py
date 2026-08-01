"""Desired tables vs. the live catalog, classified by how much a change can hurt.

The classification is the whole point of `plan`. Iceberg will happily let you make a change that
is free (add an optional column), one that is free *because of how Iceberg stores schemas* (widen
an int to a long — the field id survives, so no data file is rewritten), and one that quietly
invalidates existing rows (declare an existing nullable column required). Those look identical in
a YAML diff. They are not the same change, and `plan` exists to say which is which before
`apply` runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING

from ..errors import Diagnostics
from ..model import Ontology
from ..types import promotable
from .schema import DesiredColumn, DesiredTable, desired_tables

if TYPE_CHECKING:  # type-only, as in the validator: planning a spec needs no catalog imported
    from ..catalog.base import Catalog, TableSchema


class Severity(IntEnum):
    """Ordered, so a table's severity is `max()` over its columns and a plan's over its tables."""

    SAFE = 1
    """Additive or constraint-loosening. No existing row or file is invalidated."""

    PHYSICAL_SAFE = 2
    """Changes the stored schema but not the stored data: an Iceberg type promotion, applied by
    field id, which leaves existing data files untouched and readable."""

    BREAKING = 3
    """Cannot be applied without either losing data or leaving existing rows in violation."""


LABELS = {Severity.SAFE: "safe", Severity.PHYSICAL_SAFE: "physical-safe", Severity.BREAKING: "breaking"}


@dataclass(frozen=True)
class ColumnChange:
    kind: str  # add | promote | retype | loosen | tighten
    column: str
    severity: Severity
    detail: str  # the change itself, e.g. "int -> long" or "optional -> required"
    reason: str = ""  # why it carries that severity; rendered for anything not plainly safe
    source: str = ""  # the declaration that wants it, e.g. "Customer.lifetimeValue"
    # The end state the change produces. `detail` is prose for a human and says where the column
    # came *from*; these two say what it must become, so the executor builds DDL from the plan it
    # was shown rather than re-deriving a desired state that could differ from the printed one.
    iceberg_type: str = ""
    required: bool = False


@dataclass(frozen=True)
class TableChange:
    catalog: str
    table: str
    action: str  # "create" | "alter"
    columns: tuple[ColumnChange, ...]
    sources: tuple[str, ...] = ()

    @property
    def severity(self) -> Severity:
        return max((c.severity for c in self.columns), default=Severity.SAFE)


@dataclass(frozen=True)
class Unmanaged:
    """Live columns on a table that no property maps.

    Deliberately *not* a change: Loom never proposes a drop, so there is nothing here for `apply`
    to do. They're carried so `plan` can say what it saw and chose not to touch — but a table
    that has only these is a table with no plan, which is why they don't count toward `is_empty`.
    An existing lake table almost always has some, and a `plan` that called them changes would
    never once report a clean run."""

    catalog: str
    table: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class MigrationPlan:
    changes: tuple[TableChange, ...]
    unmanaged: tuple[Unmanaged, ...] = ()
    # Every (catalog, table) the spec binds, changed or not. `changes` alone can't answer "which
    # catalogs is this ontology deployed to?" — a spec that already matches produces no changes,
    # and that is precisely the run where `apply` still has to find its `_loom_meta` history.
    targets: tuple[tuple[str, str], ...] = ()

    @property
    def catalogs(self) -> tuple[str, ...]:
        """The catalogs this plan concerns, in first-mention order."""
        return tuple(dict.fromkeys(catalog for catalog, _ in self.targets))

    @property
    def is_empty(self) -> bool:
        return not self.changes

    @property
    def severity(self) -> Severity:
        return max((c.severity for c in self.changes), default=Severity.SAFE)

    @property
    def creates(self) -> tuple[TableChange, ...]:
        return tuple(c for c in self.changes if c.action == "create")

    @property
    def alters(self) -> tuple[TableChange, ...]:
        return tuple(c for c in self.changes if c.action == "alter")

    def by_severity(self) -> dict[Severity, int]:
        """Column-change counts per severity — the numbers in the plan's summary line."""
        counts = dict.fromkeys(Severity, 0)
        for table in self.changes:
            for column in table.columns:
                counts[column.severity] += 1
        return counts


def diff_ontology(
    ontology: Ontology, catalogs: Mapping[str, Catalog], diag: Diagnostics
) -> MigrationPlan:
    """Classify every difference between what `ontology` wants and what `catalogs` hold.

    Follows the same accumulate-everything contract as validation: an undeclared catalog or an
    un-introspectable table is recorded and the remaining tables are still planned, so one broken
    binding doesn't hide the rest of the diff. A plan is only trustworthy once `diag` is clean.
    """
    changes: list[TableChange] = []
    unmanaged: list[Unmanaged] = []
    targets: list[tuple[str, str]] = []
    for desired in desired_tables(ontology, diag).values():
        targets.append(desired.key)
        catalog = catalogs.get(desired.catalog)
        if catalog is None:
            diag.error(
                f"table '{desired.table}' is backed by catalog '{desired.catalog}', which is not "
                f"declared in loom.yaml",
                hint=f"declared by {', '.join(desired.sources)}",
            )
            continue
        change, extra = _diff_table(desired, catalog, diag)
        if change is not None:
            changes.append(change)
        if extra is not None:
            unmanaged.append(extra)
    return MigrationPlan(tuple(changes), tuple(unmanaged), tuple(targets))


def _diff_table(
    desired: DesiredTable, catalog: Catalog, diag: Diagnostics
) -> tuple[TableChange | None, Unmanaged | None]:
    if not catalog.table_exists(desired.table):
        return _creation(desired), None
    try:
        live = catalog.describe(desired.table)
    except Exception as e:
        diag.error(f"could not introspect '{desired.table}': {e}")
        return None, None
    return _alteration(desired, live)


def _creation(desired: DesiredTable) -> TableChange:
    """A table that doesn't exist yet. Every column is safe regardless of its constraints —
    there are no existing rows for a required column or a narrow type to invalidate."""
    columns = tuple(
        ColumnChange(
            kind="add",
            column=col.name,
            severity=Severity.SAFE,
            detail=f"{col.iceberg_type} {_nullability(col.required)}",
            source=col.source,
            iceberg_type=col.iceberg_type,
            required=col.required,
        )
        for col in desired.columns.values()
    )
    return TableChange(desired.catalog, desired.table, "create", columns, desired.sources)


def _alteration(
    desired: DesiredTable, live: TableSchema
) -> tuple[TableChange | None, Unmanaged | None]:
    columns: list[ColumnChange] = []
    for col in desired.columns.values():
        current = live.columns.get(col.name)
        if current is None:
            columns.append(_added(col))
            continue
        # A column can differ in both type and nullability, and those are two operations with two
        # severities — collapsing them into one line would hide a breaking half behind a safe one.
        if current.iceberg_type != col.iceberg_type:
            columns.append(_retyped(col, current.iceberg_type, current.field_id))
        if current.required != col.required:
            columns.append(_renullabled(col, currently_required=current.required))

    extra = tuple(name for name in live.columns if name not in desired.columns)
    return (
        TableChange(desired.catalog, desired.table, "alter", tuple(columns), desired.sources)
        if columns
        else None,
        Unmanaged(desired.catalog, desired.table, extra) if extra else None,
    )


def _added(col: DesiredColumn) -> ColumnChange:
    """A required column cannot be added to a populated table: Iceberg has no value to put in the
    existing rows, and no default to fall back on in v0."""
    breaking = col.required
    return ColumnChange(
        kind="add",
        column=col.name,
        severity=Severity.BREAKING if breaking else Severity.SAFE,
        detail=f"{col.iceberg_type} {_nullability(col.required)}",
        reason=(
            "existing rows have no value for a required column — add it nullable, backfill, "
            "then tighten"
            if breaking
            else ""
        ),
        source=col.source,
        iceberg_type=col.iceberg_type,
        required=col.required,
    )


def _retyped(col: DesiredColumn, current: str, field_id: int | None) -> ColumnChange:
    """Iceberg's own promotion rules decide this one, so `types.promotable` is the single
    authority — the same function physical validation uses to accept a column it didn't create."""
    if promotable(current, col.iceberg_type):
        held = f"field id {field_id}" if field_id is not None else "the column's field id"
        return ColumnChange(
            kind="promote",
            column=col.name,
            severity=Severity.PHYSICAL_SAFE,
            detail=f"{current} -> {col.iceberg_type}",
            reason=f"widening promotion applied by {held}; existing data files are not rewritten",
            source=col.source,
            iceberg_type=col.iceberg_type,
            required=col.required,
        )
    return ColumnChange(
        kind="retype",
        column=col.name,
        severity=Severity.BREAKING,
        detail=f"{current} -> {col.iceberg_type}",
        reason=f"{current} does not promote to {col.iceberg_type} — values would have to be rewritten",
        source=col.source,
        iceberg_type=col.iceberg_type,
        required=col.required,
    )


def _renullabled(col: DesiredColumn, currently_required: bool) -> ColumnChange:
    """Loosening drops a constraint every existing row already satisfies. Tightening asserts one
    they were never checked against."""
    if currently_required:
        return ColumnChange(
            kind="loosen",
            column=col.name,
            severity=Severity.SAFE,
            detail="required -> optional",
            source=col.source,
            iceberg_type=col.iceberg_type,
            required=False,
        )
    return ColumnChange(
        kind="tighten",
        column=col.name,
        severity=Severity.BREAKING,
        detail="optional -> required",
        reason="existing rows may already hold nulls, which the new constraint would not admit",
        source=col.source,
        iceberg_type=col.iceberg_type,
        required=True,
    )


def _nullability(required: bool) -> str:
    return "required" if required else "optional"
