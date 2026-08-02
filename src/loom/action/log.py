"""`_loom_meta.edits` — what an action did to a row, recorded in the lake it did it to.

`_loom_meta.applied` records what `apply` did to schemas. Nothing recorded what an action did to
data, and this is that: an append-only Iceberg table per catalog, sitting in the namespace Loom
already owns, holding one row per run.

Five decisions shape it, and each had an obvious-looking alternative:

- **It is written through a port of its own.** Not `RowWriter.insert_row` (which requires a snapshot
  expectation nobody holds, and would let a busy log table refuse the write it exists to describe),
  not a `CatalogWriter` beside the row writer (which carries `alter_table`). `EditLogWriter` takes no
  table name, so the runtime cannot point it anywhere. The full argument, and what the fourth port
  costs, is on the port itself.

- **The first append creates it, per catalog.** `apply` does not, and does not know it exists — the
  spec never names this table, so `plan` cannot propose it and `validate --physical` cannot check it.
  Making `apply` the creator would give the log a precondition the write does not have, and Loom
  writes to lakes it never migrated. Per catalog rather than per backing table for `_loom_meta`'s own
  reason (the record sits beside the data it describes) plus one of its own: "what did this actor do
  today" is a cross-table question, and a per-table sidecar cannot answer it.

- **A refused run is recorded, once it named a row.** See `EditRecord.status`. A run that never got
  as far as resolving a key is a malformed call rather than an attempted edit, and belongs to a
  request log at the serve boundary, not to a table called `edits`.

- **`before` and `after` carry declared properties only** — the same projection `ActionResult` uses,
  extended here rather than contradicted. See `EditRecord.before`.

- **The columns are forever.** `append_edit` only ever *creates* the table, so a column left out
  today can never reach a log table that already exists — the same trap `_loom_meta.applied` names.
  So the schema below is deliberately generous, everything but two columns is optional, and anything
  still unsettled goes inside a JSON column rather than waiting for a column that can never arrive.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..catalog.base import EDIT_LOG_TABLE, Column
from ..migrate.meta import loom_version

if TYPE_CHECKING:
    from ..catalog.base import Catalog, EditLogWriter

UNKNOWN_ACTOR = "unknown"
"""Recorded when no caller supplied an actor.

Deliberately not `default_actor()`, which is honest for `loom apply` and for `loom run` — commands a
person runs — and a lie for `run_<action>` over MCP, where it would name whoever started
`loom serve` and stamp every caller in the deployment with the same string. A log that says it does
not know beats one that confidently names the wrong principal. The runtime never calls
`default_actor()`; `loom run` passes it in explicitly, at the one call site where it is true."""

# Everything optional but `edit_id` and `recorded_at`: this table is written by exactly one writer
# and read by anything with an Iceberg client, and a required column is one a future Loom can never
# add. The two that stay required are the two an empty value would make the row unciteable.
EDIT_COLUMNS: tuple[Column, ...] = (
    Column("edit_id", "string", required=True),
    Column("recorded_at", "timestamptz", required=True),
    Column("actor", "string", required=False),
    Column("action", "string", required=False),
    Column("object_type", "string", required=False),
    Column("operation", "string", required=False),
    Column("catalog", "string", required=False),
    # `table_name` and `object_key` rather than `table` and `key`: this table is meant to be read
    # from any SQL engine someone points at the lake, and both of the shorter spellings are reserved
    # words in dialects Loom already targets.
    Column("table_name", "string", required=False),
    Column("object_key", "string", required=False),
    Column("status", "string", required=False),
    Column("attempts", "long", required=False),
    Column("read_snapshot_id", "long", required=False),
    Column("parameters", "string", required=False),
    Column("before", "string", required=False),
    Column("after", "string", required=False),
    Column("failures", "string", required=False),
    Column("loom_version", "string", required=False),
)


def new_edit_id() -> str:
    """The identity of one run, minted before the write so the write can carry it.

    A run has exactly one, whatever it takes: three attempts and one commit share an id, because
    they are one edit that took three tries rather than three edits."""
    return uuid.uuid4().hex


def commit_properties(edit_id: str, action: str, actor: str) -> dict[str, str]:
    """What the row write stamps into its own Iceberg commit.

    This is the only record of an edit that is atomic with the edit — everything else, this module
    included, is a second commit that a crash can land on the wrong side of. Kept to three keys: who,
    what, and the id that ties the commit to the record beside it. A snapshot summary is metadata
    carried in every table-metadata read, so it is not the place for the payload.

    The same duplication `table_properties()` makes one plane up, for the same reason: someone
    looking at the table's history in any Iceberg client should be able to see that Loom wrote a
    snapshot, and which edit it was, without knowing that a log table exists."""
    return {"loom.edit_id": edit_id, "loom.action": action, "loom.actor": actor}


@dataclass(frozen=True)
class EditRecord:
    """One run of one action, as the log holds it.

    `status` is the run's own status, with one boundary: `previewed` never reaches here (a preview
    writes nothing, and `loom run` previews before every real run, so logging them would double every
    record), and neither does a refusal that never resolved a key. `applied`, `refused` and `failed`
    all do — including `failed`, which is the one case where nobody knows whether the write landed,
    and therefore the case the record is most worth having.

    `before` and `after` are the object **as the ontology sees it** — declared properties, by
    property name, through the same projection `ActionResult` uses. That rule is extended here rather
    than repeated, because the reader is different: an auditor, not an agent. The physical row was
    the alternative and it is worse than the leak the rule was written to prevent. It would make this
    table an unabridged second copy of the data, in a table nothing governs, retained forever — and
    the copy that *outlives* the row, so a `forget-customer` action would erase a customer into a
    permanent record of them.

    The objection that answers is real: a modify rewrites the whole row, so declared properties are
    an incomplete account of the write. The answer is that the rest of the write is a *guarantee*
    rather than a gap. Every unmapped column was carried across unchanged, and since the concurrency
    slice the commit asserted the snapshot the read saw, so nothing moved under it. What this record
    does not name, the run did not change.

    What that does not fix: declared properties are still somebody's data and still outlive a delete.
    That is the same question §6's `governance.policies` will face, and it is deferred to it
    deliberately (spec-v0 "Open edges") rather than answered by accident here.

    `parameters` is the bound call — the caller's own arguments, coerced to their declared types. It
    is here because a refused modify has no `after`, and without it the log records that somebody
    tried without recording what they tried. They are declared parameters of a declared action, so
    they sit in the same vocabulary as declared properties and under the same rule.
    """

    edit_id: str
    recorded_at: datetime
    actor: str
    action: str
    object_type: str
    operation: str
    catalog: str
    table_name: str
    object_key: str
    status: str
    attempts: int
    read_snapshot_id: int | None = None
    parameters: Any = None
    before: Mapping[str, Any] | None = None
    after: Mapping[str, Any] | None = None
    failures: Sequence[Mapping[str, Any]] = ()
    loom_version: str = ""

    def row(self) -> dict[str, Any]:
        return {
            "edit_id": self.edit_id,
            "recorded_at": self.recorded_at,
            "actor": self.actor,
            "action": self.action,
            "object_type": self.object_type,
            "operation": self.operation,
            "catalog": self.catalog,
            "table_name": self.table_name,
            "object_key": self.object_key,
            "status": self.status,
            "attempts": self.attempts,
            "read_snapshot_id": self.read_snapshot_id,
            "parameters": _json(self.parameters),
            "before": _json(self.before),
            "after": _json(self.after),
            "failures": _json(list(self.failures)) if self.failures else "",
            "loom_version": self.loom_version or loom_version(),
        }


@dataclass
class EditLog:
    """The `_loom_meta.edits` table of one catalog, created on first write.

    Mirrors `MetaStore`: reads go through the read port and writes through a write port, and the two
    references stay distinct so the read half is usable against a catalog nobody can write to. The
    difference is which write port — `MetaStore` holds a `CatalogWriter` because `apply` already has
    one, and this holds an `EditLogWriter` because an action must not."""

    catalog: Catalog
    writer: EditLogWriter | None = None

    def history(self) -> tuple[dict[str, Any], ...]:
        """Every recorded run in this catalog, oldest first. Empty if the log was never created.

        Rows as they are stored rather than as `EditRecord`s: the JSON columns are strings on disk
        and a reader deciding what to parse is better served by the raw row than by a dataclass that
        guessed. Sorted by `recorded_at`, which is the order they were appended in — Iceberg does not
        promise scan order, and an audit trail read out of sequence is a trap."""
        if not self.catalog.table_exists(EDIT_LOG_TABLE):
            return ()
        rows = self.catalog.scan(EDIT_LOG_TABLE).to_pylist()
        return tuple(sorted(rows, key=lambda r: (r.get("recorded_at") or datetime.min, r.get("edit_id") or "")))

    def record(self, entry: EditRecord) -> None:
        """Append one record, creating the log table if this catalog has never held one.

        Raises `CatalogError` on failure and does not swallow it — but the caller does not treat that
        as the action failing, because by the time this runs the row write has already committed. See
        `_Run`'s caller in `runtime.py`."""
        if self.writer is None:  # pragma: no cover - the runtime resolves a writer before calling
            raise RuntimeError("EditLog has no writer — nothing can be recorded")
        self.writer.append_edit(EDIT_COLUMNS, entry.row())


def render_key(key: Any) -> str:
    """The primary key as one string column.

    A string rather than the key's declared type, because one log table holds edits to every object
    type in the spec and their keys are not one type. Nothing is lost: `object_type` names the type,
    and the spec says what its primary key is declared as, so the value is recoverable. `None` — a
    key a nullable column can genuinely hold — renders as the empty string, which is why
    `object_key` is not one of the required columns."""
    return "" if key is None else str(key)


def _json(value: Any) -> str:
    """A JSON column, or the empty string for absent.

    Empty rather than SQL null, so that "there was no prior row" and "the column was never written"
    do not have to be told apart by a reader who cannot. `default=str` catches the value types JSON
    has no spelling for — a `Decimal`, a `datetime`, a `date` — the same coercion the MCP layer's
    `json_safe` performs, done here rather than imported because a table in the lake must not depend
    on a transport layer being installed."""
    if value is None:
        return ""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def now() -> datetime:
    return datetime.now(UTC)
