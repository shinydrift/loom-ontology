"""The plan, as a human reads it.

Deliberately terraform-shaped, because the job is the same one terraform's plan output does: let
someone scan a diff and decide whether to run it. The symbols carry the classification —
`+` adds, `~` changes something in place, `!` is a change that existing data won't survive — so
the severity of a plan is visible from the left margin before any of the text is read.

Kept apart from `diff.py` so the classification has no opinion about presentation: `apply` will
consume the same `MigrationPlan`, and a future `--json` is a second renderer, not a rewrite.
"""

from __future__ import annotations

from .diff import LABELS, ColumnChange, MigrationPlan, Severity, TableChange

_COLUMN_SYMBOL = {"add": "+", "promote": "~", "loosen": "~", "retype": "!", "tighten": "!"}
_BREAKING_SYMBOL = "!"
_TABLE_SYMBOL = {"create": "+", "alter": "~"}

NO_CHANGES = "No changes — the catalog already matches the ontology."
DRY_RUN_NOTE = "`loom apply` is not implemented yet — `plan` is a dry run (M2)."


def render_plan(plan: MigrationPlan, title: str | None = None) -> str:
    """The full plan as one printable block, with no trailing newline."""
    if plan.is_empty:
        return "\n".join([NO_CHANGES, *_render_unmanaged(plan)])

    name_width, detail_width = _widths(plan)
    lines: list[str] = []
    if title:
        lines += [f"Loom plan — {title}", ""]
    for table in plan.changes:
        lines += _render_table(table, name_width, detail_width)
        lines.append("")
    lines += [_summary(plan), DRY_RUN_NOTE, *_render_unmanaged(plan)]
    return "\n".join(lines)


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
