"""The migration engine — the ontology as the desired state of a set of Iceberg tables.

`plan` is pure read: it derives the tables the spec *wants* (`schema.py`), compares them against
what the catalog actually holds (`diff.py`), and prints the classified result (`render.py`).
Nothing here executes DDL — `apply` lands in the next slice, and with it the `_loom_meta` state
store, `renamedFrom` field-id remapping, and rollback.

Two rules shape the whole package:

- **The live catalog is the baseline, not a state file.** `Catalog.describe()` already returns
  column types and Iceberg field ids, which is everything a diff needs. `_loom_meta` records what
  `apply` did; it is not required to work out what `apply` *should* do, and diffing against it
  instead would make `plan` lie whenever someone changed a table out of band.
- **Loom never proposes a drop.** An objectType maps a *subset* of a table's columns, so a column
  no property mentions is not evidence of a deleted property — it's someone else's data. Those
  columns are reported as unmanaged and left alone.
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
from .render import render_plan
from .schema import DesiredColumn, DesiredTable, desired_tables

__all__ = [
    "ColumnChange",
    "DesiredColumn",
    "DesiredTable",
    "MigrationPlan",
    "Severity",
    "TableChange",
    "Unmanaged",
    "desired_tables",
    "diff_ontology",
    "render_plan",
]
