"""The migration engine — the ontology as the desired state of a set of Iceberg tables.

`plan` is pure read: it derives the tables the spec *wants* (`schema.py`), compares them against
what the catalog actually holds (`diff.py`), and prints the classified result (`render.py`).
`apply` executes that same plan (`executor.py`) and records it (`meta.py`). `rollback` restores a
spec out of that record and re-plans it (`rollback.py`) — the same diff, the same executor, the
same refusal, run against an older spec.

Five rules shape the whole package:

- **The live catalog is the baseline, not a state file.** `Catalog.describe()` already returns
  column types and Iceberg field ids, which is everything a diff needs. `_loom_meta` records what
  `apply` did; it is not required to work out what `apply` *should* do, and diffing against it
  instead would make `plan` lie whenever someone changed a table out of band. It is also what
  makes `apply` idempotent for free: a second run re-derives the diff and finds nothing to do.
- **Loom never proposes a drop.** An objectType maps a *subset* of a table's columns, so a column
  no property mentions is not evidence of a deleted property — it's someone else's data. Those
  columns are reported as unmanaged and left alone.
- **A rename is a remap, not an add.** `renamedFrom` makes a moved column keep its field id, so no
  data file is rewritten and nothing is stranded. Because the baseline is the live catalog, the
  key stays in the spec after its migration lands and plans as a clean no-op — see `diff._renamed`
  for the three live shapes, and `diff._unmergeable` for the fourth, which Loom refuses.
- **A breaking plan is refused whole.** Not partially applied. See `executor._refusal`.
- **Writes go through a separate port.** `apply` asks the catalog layer for a `CatalogWriter`;
  everything else in Loom holds a read-only `Catalog` and could not execute DDL if it tried.

The never-drop rule is the one that shapes `rollback` most: it means a rollback undoes renames and
nothing else. Adds stay live and unmanaged, and reversing a promotion or a loosening is itself
breaking, so it is refused whole. See `rollback.py`.
"""

from __future__ import annotations

from .diff import (
    ColumnChange,
    MigrationPlan,
    Severity,
    TableChange,
    Unmanaged,
    diff_ontology,
)
from .executor import APPLIED, FAILED, REFUSED, UP_TO_DATE, ApplyResult, TableOutcome, apply_plan
from .meta import META_TABLE, AppliedRecord, MetaStore, SpecSnapshot, snapshot_spec
from .render import render_apply, render_plan, render_rollback
from .rollback import (
    FileChanges,
    LeftBehind,
    RollbackError,
    RollbackTarget,
    file_changes,
    latest_version,
    left_behind,
    materialize,
    resolve_target,
    restore_files,
)
from .schema import DesiredColumn, DesiredTable, desired_tables

__all__ = [
    "APPLIED",
    "FAILED",
    "REFUSED",
    "UP_TO_DATE",
    "AppliedRecord",
    "ApplyResult",
    "ColumnChange",
    "DesiredColumn",
    "DesiredTable",
    "FileChanges",
    "LeftBehind",
    "META_TABLE",
    "MetaStore",
    "MigrationPlan",
    "RollbackError",
    "RollbackTarget",
    "Severity",
    "SpecSnapshot",
    "TableChange",
    "TableOutcome",
    "Unmanaged",
    "apply_plan",
    "desired_tables",
    "diff_ontology",
    "file_changes",
    "latest_version",
    "left_behind",
    "materialize",
    "render_apply",
    "render_plan",
    "render_rollback",
    "resolve_target",
    "restore_files",
    "snapshot_spec",
]
