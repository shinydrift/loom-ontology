"""The plan and its execution, as a human reads them.

Deliberately terraform-shaped, because the job is the same one terraform's plan output does: let
someone scan a diff and decide whether to run it. The symbols carry the classification —
`+` adds, `~` changes something in place, `!` is a change that existing data won't survive — so
the severity of a plan is visible from the left margin before any of the text is read.

Kept apart from `diff.py` so the classification has no opinion about presentation: `apply` runs
off the same `MigrationPlan` this prints, and a future `--json` is a second renderer, not a
rewrite.
"""

from __future__ import annotations

from collections.abc import Sequence

from .diff import LABELS, ColumnChange, MigrationPlan, Severity, TableChange
from .executor import APPLIED, REFUSED, UP_TO_DATE, ApplyResult, TableOutcome
from .meta import STATUS_APPLIED
from .rollback import FileChanges, LeftBehind, RollbackTarget

_COLUMN_SYMBOL = {"add": "+", "rename": "~", "promote": "~", "loosen": "~", "retype": "!", "tighten": "!"}
_BREAKING_SYMBOL = "!"
_TABLE_SYMBOL = {"create": "+", "alter": "~"}

NO_CHANGES = "No changes — the catalog already matches the ontology."
DRY_RUN_NOTE = "This is a dry run — nothing was changed. Run `loom apply` to execute it."
BREAKING_NOTE = "`loom apply` will refuse this plan: a breaking change cannot be executed safely."


def render_plan(
    plan: MigrationPlan, title: str | None = None, *, executing: bool = False, unmanaged: bool = True
) -> str:
    """The full plan as one printable block, with no trailing newline.

    `executing` drops the closing note: `apply` prints the plan too — the same plan, so the reader
    can check what ran against what was proposed — and telling them there that nothing was changed
    would be a lie a second before it changes something.

    `unmanaged` drops the footer, for `render_rollback`, which has more to say about those columns
    than this does and says all of it in one place."""
    footer = _render_unmanaged(plan) if unmanaged else []
    if plan.is_empty:
        return "\n".join([NO_CHANGES, *footer])

    name_width, detail_width = _widths(plan)
    lines: list[str] = []
    if title:
        lines += [f"Loom plan — {title}", ""]
    for table in plan.changes:
        lines += _render_table(table, name_width, detail_width)
        lines.append("")
    lines.append(_summary(plan))
    if not executing:
        lines.append(BREAKING_NOTE if plan.severity is Severity.BREAKING else DRY_RUN_NOTE)
    lines += footer
    return "\n".join(lines)


def render_rollback(
    target: RollbackTarget,
    plan: MigrationPlan,
    left: Sequence[LeftBehind],
    changes: FileChanges,
    title: str | None = None,
) -> str:
    """A rollback, before it runs: which version is being restored, what that does to the physical
    schema, what it will *not* take back, and which files it will write.

    The middle of it is `render_plan` unchanged, because the middle of a rollback is an ordinary
    plan. The two blocks around it are the parts a plan has no way to say."""
    lines = []
    if title:
        lines.append(f"Loom rollback — {title}")
    lines.append(
        f"Restoring the spec recorded at version {target.version} "
        f"(from {', '.join(target.held_by)})."
    )
    if target.status != STATUS_APPLIED:
        lines.append(
            f"  note: version {target.version} is recorded as '{target.status}' — that apply did "
            f"not fully land."
        )
    for name in target.absent_from:
        # Not an error and not a skip: a version selects a spec, and that spec either binds this
        # catalog or does not. Either way it is planned like every other one — this says only that
        # its own history has nothing from that far back to compare against.
        lines.append(
            f"  note: '{name}' has no `_loom_meta` history at or before version {target.version}."
        )
    # The roadmap line this closes said "point the physical schema at an earlier snapshot", which
    # reads like it could discard rows. It cannot, and the place to say so is here.
    lines.append("Rows are untouched — `apply` only ever ran DDL, so this only reverses DDL.")
    lines += ["", render_plan(plan, executing=True, unmanaged=False)]
    lines += _render_left_behind(plan, left, target.version)
    lines += _render_files(changes)
    return "\n".join(lines)


def _render_left_behind(
    plan: MigrationPlan, left: Sequence[LeftBehind], version: int
) -> list[str]:
    """Everything the rollback is leaving live, in one block and split by how it got there.

    Merged with the plan's ordinary unmanaged columns because from the lake's point of view they
    are now the same thing — and split, because "Loom added this on a version you are undoing" and
    "this was never yours to begin with" call for different decisions."""
    stranded = {(e.catalog, e.table): set(e.columns) for e in left if not e.whole_table}
    lines: list[str] = []
    for entry in plan.unmanaged:
        mine = stranded.get((entry.catalog, entry.table), set())
        for columns, why in (
            (tuple(c for c in entry.columns if c in mine), f"added after version {version}"),
            (tuple(c for c in entry.columns if c not in mine), "never mapped by this ontology"),
        ):
            if columns:
                lines.append(f"  · {entry.catalog}.{entry.table}: {', '.join(columns)} — {why}")
    for entry in left:
        if entry.whole_table:
            lines.append(
                f"  · {entry.catalog}.{entry.table} — the whole table, created after version {version}"
            )
    if not lines:
        return []
    return ["", "Left in place — a rollback never drops, so these stay live and unmanaged:", *lines]


def _render_files(changes: FileChanges) -> list[str]:
    """Named before the prompt, deletions included: nobody should be asked to approve a write to a
    file they have open without being shown which files."""
    if not changes.any:
        return ["", "Spec files: already exactly what that version recorded."]
    lines = ["", "Spec files:"]
    lines += [f"  ~ {name} — restored" for name in changes.written]
    lines += [f"  - {name} — deleted; it did not exist at that version" for name in changes.deleted]
    return lines


def render_apply(result: ApplyResult) -> str:
    """What `apply` did, once it has done it.

    Shaped like the plan it executed — same symbols, same order — so the two can be read against
    each other. A refusal prints the executor's own message rather than a rephrasing of it: the
    reasons are the plan's, and there is exactly one place they should be written."""
    if result.status == REFUSED:
        return result.error
    if result.status == UP_TO_DATE:
        return f"Already applied — nothing to do.{_versions(result, ' Recorded as')}"

    lines = [_render_outcome(o) for o in result.tables]
    if result.status == APPLIED:
        landed = len(result.applied)
        lines.append(f"Applied {landed} table change(s).{_versions(result, ' Recorded as')}")
    else:
        lines.append(f"Failed: {result.error}")
        # The distinction that matters after a failed run: Iceberg commits per table, so what
        # already landed is still there and a re-run picks up from it.
        lines.append(f"{len(result.applied)} table change(s) had already been committed and remain applied.")
        lines.append("Re-run `loom plan` — the diff is taken from the live catalog, so it shows what is left.")
    return "\n".join(lines)


def _render_outcome(outcome: TableOutcome) -> str:
    symbol = "!" if not outcome.ok else _TABLE_SYMBOL[outcome.action]
    what = "created" if outcome.action == "create" else f"{len(outcome.columns)} change(s) applied"
    namespace = f" · namespace '{outcome.namespace_created}' created" if outcome.namespace_created else ""
    tail = f" — FAILED: {outcome.error}" if not outcome.ok else ""
    return f"  {symbol} {outcome.catalog}.{outcome.table} — {what}{namespace}{tail}"


def _versions(result: ApplyResult, prefix: str) -> str:
    if not result.versions:
        return ""
    # One number for the whole apply; the catalogs are named because that is where the row landed.
    version = max(result.versions.values())
    return f"{prefix} version {version} in `_loom_meta` ({', '.join(sorted(result.versions))})."


def _render_unmanaged(plan: MigrationPlan) -> list[str]:
    """Its own footer rather than a line under each table: these are the columns `apply` will
    *not* touch, and mixing them into the change list is what makes a reader think they're queued
    up to be dropped."""
    if not plan.unmanaged:
        return []
    lines = ["", "Unmanaged — columns no property maps, left untouched:"]
    for entry in plan.unmanaged:
        lines.append(f"  · {entry.catalog}.{entry.table}: {', '.join(entry.columns)}")
    return lines


def _render_table(table: TableChange, name_width: int, detail_width: int) -> list[str]:
    symbol = "!" if table.severity is Severity.BREAKING else _TABLE_SYMBOL[table.action]
    headline = "create table" if table.action == "create" else f"{len(table.columns)} change(s)"
    sources = f" · {', '.join(table.sources)}" if table.sources else ""
    lines = [f"  {symbol} {table.catalog}.{table.table} — {headline}{sources}"]
    for change in table.columns:
        lines += _render_column(change, table.action, name_width, detail_width)
    return lines


def _render_column(
    change: ColumnChange, action: str, name_width: int, detail_width: int
) -> list[str]:
    # Severity outranks kind: adding a required column to a populated table is still an add, but
    # showing it as `+` would put the one marker that means "free" next to the one change on the
    # page that isn't.
    symbol = _BREAKING_SYMBOL if change.severity is Severity.BREAKING else _COLUMN_SYMBOL[change.kind]
    # On a fresh table every column is trivially safe, so labelling each one adds a column of
    # noise that says nothing the header hasn't already said.
    label = "" if action == "create" else LABELS[change.severity]
    line = f"      {symbol} {change.column:<{name_width}}  {change.detail:<{detail_width}}  {label}"
    lines = [line.rstrip()]
    if change.reason:
        lines.append(f"          {change.reason}")
    return lines


def _summary(plan: MigrationPlan) -> str:
    counts = plan.by_severity()
    tallies = ", ".join(f"{counts[s]} {LABELS[s]}" for s in Severity if counts[s])
    return (
        f"Plan: {len(plan.creates)} to create, {len(plan.alters)} to change · {tallies}"
    )


def _widths(plan: MigrationPlan) -> tuple[int, int]:
    """One set of column widths for the whole plan, so the detail column lines up across tables
    rather than jumping per table."""
    changes = [c for t in plan.changes for c in t.columns]
    return (
        max((len(c.column) for c in changes), default=0),
        max((len(c.detail) for c in changes), default=0),
    )
