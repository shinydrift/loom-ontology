"""The plan, executed.

`plan` classifies; this module is the half that is allowed to change something. It takes the very
`MigrationPlan` that was printed — not a freshly derived desired state — so what runs is what was
read.

Three rules it enforces, in this order:

1. **A breaking plan runs nothing.** Not "the safe parts of it": nothing. See `_refusal`.
2. **One table, one transaction.** Iceberg's unit of atomicity is a table, so that is the unit
   here. Across tables the run is sequential and stops at the first failure, and the result says
   exactly which tables landed — an honest partial beats a pretend-atomic one.
3. **Nothing is applied twice.** Re-running is safe because the plan is re-derived from the live
   catalog each time, so the second run simply has nothing to do. `_loom_meta` adds the cheaper
   answer on top: same content hash, empty plan, already recorded → don't even write a row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..catalog.base import CatalogError, Column, SchemaEdit, writer_for
from .diff import ColumnChange, MigrationPlan, Severity, TableChange
from .meta import (
    STATUS_APPLIED,
    STATUS_PARTIAL,
    MetaStore,
    SpecSnapshot,
    table_properties,
)

if TYPE_CHECKING:
    from ..catalog.base import Catalog, CatalogWriter

APPLIED = "applied"
"""DDL ran (or there was none) and every catalog recorded it."""

UP_TO_DATE = "up-to-date"
"""Nothing to do and nothing to record — this exact spec is already the live one."""

REFUSED = "refused"
"""The plan contains a breaking change. Nothing was executed and nothing was recorded."""

FAILED = "failed"
"""Something went wrong mid-run. `ApplyResult.tables` says what had already landed."""

# The safe/physical-safe column kinds, mapped to the write port's vocabulary. A kind that isn't
# here is breaking by construction (`retype`, `tighten`) and is refused before this is consulted.
_OPS = {"add": "add", "promote": "promote", "loosen": "relax"}


@dataclass(frozen=True)
class TableOutcome:
    catalog: str
    table: str
    action: str  # create | alter
    columns: tuple[str, ...] = ()  # "column: detail", as the plan rendered them
    namespace_created: str = ""  # the namespace, if this run had to create it
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def as_json(self) -> dict[str, Any]:
        """The shape that lands in `_loom_meta.summary`."""
        entry: dict[str, Any] = {"table": f"{self.catalog}.{self.table}", "action": self.action}
        if self.columns:
            entry["columns"] = list(self.columns)
        if self.namespace_created:
            entry["namespace_created"] = self.namespace_created
        if self.error:
            entry["error"] = self.error
        return entry


@dataclass(frozen=True)
class ApplyResult:
    status: str
    tables: tuple[TableOutcome, ...] = ()
    versions: Mapping[str, int] = field(default_factory=dict)  # catalog -> recorded version
    blocked: tuple[TableChange, ...] = ()  # the breaking changes, when status is REFUSED
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (APPLIED, UP_TO_DATE)

    @property
    def applied(self) -> tuple[TableOutcome, ...]:
        return tuple(t for t in self.tables if t.ok)


def apply_plan(
    plan: MigrationPlan,
    catalogs: Mapping[str, Catalog],
    snapshot: SpecSnapshot,
    *,
    actor: str | None = None,
    now: datetime | None = None,
) -> ApplyResult:
    """Execute `plan` and record it. The only entry point; `loom apply` is a thin shell over it."""
    breaking = tuple(t for t in plan.changes if t.severity is Severity.BREAKING)
    if breaking:
        return ApplyResult(status=REFUSED, blocked=breaking, error=_refusal(breaking))

    # Every writer up front, before a single statement runs: a read-only catalog is a property of
    # the deployment, not of a table, and finding out halfway through would leave a run that can
    # never be completed as planned.
    try:
        stores = _stores(plan, catalogs)
    except CatalogError as e:
        return ApplyResult(status=REFUSED, error=str(e))

    # One version number for the whole apply, not one per catalog: "version 7" has to name the
    # same apply in every lake the spec touches. Nothing holds that counter centrally, so it is
    # derived — one past the highest any bound catalog has recorded.
    current = max((store.current_version() for store in stores.values()), default=0)
    version = current + 1
    pending = _to_record(stores, snapshot, plan)
    if plan.is_empty and not pending:
        return ApplyResult(status=UP_TO_DATE, versions={name: current for name in stores})

    outcomes: list[TableOutcome] = []
    failure = ""
    properties = table_properties(snapshot, version)
    for change in plan.changes:
        writer = stores[change.catalog].writer
        assert writer is not None  # _stores resolved one for every catalog in the plan
        outcome = _execute(change, writer, properties)
        outcomes.append(outcome)
        if not outcome.ok:
            # Stop rather than push on: the remaining tables were planned against a lake that no
            # longer looks the way the plan assumed, and a second `loom plan` costs nothing.
            failure = outcome.error
            break

    # Recorded even when a table failed — a partial apply is exactly the run whose history someone
    # will want to read — but marked as such, so the "already applied" check never trusts it.
    row_status = STATUS_PARTIAL if failure else STATUS_APPLIED
    recorded, record_error = _record(stores, pending, snapshot, outcomes, version, row_status, actor, now)
    # A recording failure after committed DDL is reported, not raised: the schema change cannot be
    # taken back, and the next run re-plans against the live catalog, finds nothing to do, and
    # records the spec then.
    return ApplyResult(
        status=FAILED if (failure or record_error) else APPLIED,
        tables=tuple(outcomes),
        versions=recorded,
        error=failure or record_error,
    )


def _stores(plan: MigrationPlan, catalogs: Mapping[str, Catalog]) -> dict[str, MetaStore]:
    """One `_loom_meta` handle per catalog the spec binds — including the ones with no changes,
    whose history still has to be read to decide whether this spec is already recorded there."""
    stores: dict[str, MetaStore] = {}
    for name in plan.catalogs:
        catalog = catalogs.get(name)
        if catalog is None:  # pragma: no cover - diff_ontology errors on this first
            raise CatalogError(f"catalog '{name}' is not declared in loom.yaml")
        stores[name] = MetaStore(catalog=catalog, writer=writer_for(catalog))
    return stores


def _to_record(
    stores: Mapping[str, MetaStore], snapshot: SpecSnapshot, plan: MigrationPlan
) -> tuple[str, ...]:
    """Which catalogs need a new history row.

    A catalog whose latest row already names this exact spec — and recorded it as fully applied —
    needs nothing. A catalog where the hash differs does, even when the plan is empty: an edit that
    changes no column still changes the file a rollback would restore, and a history that skipped
    it would restore the wrong text."""
    changed = {c.catalog for c in plan.changes}
    out = []
    for name, store in stores.items():
        latest = store.latest()
        stale = latest is None or latest.content_hash != snapshot.content_hash or latest.status != STATUS_APPLIED
        if stale or name in changed:
            out.append(name)
    return tuple(out)


def _execute(change: TableChange, writer: CatalogWriter, properties: Mapping[str, str]) -> TableOutcome:
    columns = tuple(f"{c.column}: {c.detail}" for c in change.columns)
    created_namespace = ""
    try:
        if change.action == "create":
            if writer.ensure_namespace(change.table):
                created_namespace = change.table.rpartition(".")[0]
            writer.create_table(change.table, [_column(c) for c in change.columns], properties)
        else:
            writer.alter_table(change.table, [_edit(c) for c in change.columns], properties)
    except CatalogError as e:
        return TableOutcome(change.catalog, change.table, change.action, columns, created_namespace, str(e))
    return TableOutcome(change.catalog, change.table, change.action, columns, created_namespace)


def _column(change: ColumnChange) -> Column:
    return Column(name=change.column, iceberg_type=change.iceberg_type, required=change.required)


def _edit(change: ColumnChange) -> SchemaEdit:
    return SchemaEdit(op=_OPS[change.kind], column=_column(change))


def _record(
    stores: Mapping[str, MetaStore],
    pending: Sequence[str],
    snapshot: SpecSnapshot,
    outcomes: Sequence[TableOutcome],
    version: int,
    row_status: str,
    actor: str | None,
    now: datetime | None,
) -> tuple[dict[str, int], str]:
    """Append the history rows. Returns the versions actually written and the first error, if any.

    A catalog only ever records the tables *it* holds, so a two-catalog spec produces two rows that
    each describe their own half — but both carry the whole spec and the same version, because
    that is what was applied.
    """
    recorded: dict[str, int] = {}
    error = ""
    for name in pending:
        summary = [o.as_json() for o in outcomes if o.catalog == name]
        try:
            entry = stores[name].record(
                snapshot, summary, version=version, status=row_status, actor=actor, now=now
            )
        except CatalogError as e:
            error = error or f"changes were applied but could not be recorded in '{name}': {e}"
            continue
        recorded[name] = entry.version
    return recorded, error


def _refusal(breaking: Sequence[TableChange]) -> str:
    """Why the run stopped, in the plan's own words.

    Deliberately not a flag away from running anyway. Every breaking change either drops data or
    leaves existing rows violating the constraint that was just declared, and the fix is a data
    migration — backfill, or write the new column and move the values across — which Loom has no
    verb for until the action runtime lands. A `--force` here would not make the change safe; it
    would make Loom the thing that broke the table."""
    lines = ["refusing to apply: the plan contains breaking changes"]
    for table in breaking:
        for column in table.columns:
            if column.severity is not Severity.BREAKING:
                continue
            lines.append(f"  ! {table.catalog}.{table.table}.{column.column}: {column.detail}")
            if column.reason:
                lines.append(f"      {column.reason}")
    lines.append("  nothing was applied — no table is left half-migrated")
    return "\n".join(lines)
