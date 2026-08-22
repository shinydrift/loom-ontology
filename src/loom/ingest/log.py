"""`_loom_meta.loads` — what an ingest did to a table, recorded in the lake it did it to.

`_loom_meta.applied` records what `apply` did to schemas. `_loom_meta.edits` records what an action
did to one row. Nothing recorded what a *bulk* write did, and that absence is the whole reason ingest
exists as a Loom concern rather than as somebody's script: a deployment could run
`governance.edit_log: required`, answer "what did this actor do today" precisely for every
single-row agent write, and have nothing whatsoever to say about the four-million-row overwrite that
actually moved the numbers.

The decisions here are `action.log`'s, re-derived rather than assumed, and two of them come out
differently:

- **A port of its own, again.** `LoadLogWriter`, not `EditLogWriter` with a table argument. The
  property that makes either safe is that the table is not an argument, and one port used for two
  tables loses it. See `catalog.base.LoadLogWriter`.

- **A table of its own.** Not more rows in `_loom_meta.edits`. That table's columns are forever —
  `append_edit` only ever *creates* it — and they are action-shaped: `action`, `operation`,
  `object_key`, `before`, `after`, `attempts`. A load has none of those and four things that table
  can never grow a column for. See `catalog.base.LOAD_LOG_TABLE`.

- **One row per load, not per row loaded.** The obvious alternative is unaffordable rather than
  merely undesirable — a million-row load would write a million records into a table nothing prunes —
  but it is also *wrong*, in the same way one record per attempt would have been wrong for an action.
  A load is one decision and one commit. What varies per row is whether it was written or rejected,
  and that is three integers rather than a million rows.

- **`before` and `after` do not exist here, and that is a difference rather than an omission.** An
  action's record carries them because a caller supplied a handful of values and the question "what
  did this change" has an answer that fits in a row. A load's answer is the batch, and putting it in
  the log would make this table an unabridged second copy of somebody's nightly drop — the leak
  `EditRecord` refuses at one-row scale, at a scale where it is also a storage bill. What replaces
  them is `source` and `source_fingerprint`: not the data, but enough to identify the data, so an
  auditor holding the file can prove it is the one that landed.

- **The rows are forever, and nothing here removes one.** `_record` writes after the commit so a
  lost record is a *findable* gap — the write stamped `loom.load_id` into its own Iceberg snapshot —
  and an expiry that deleted rows would make an expired load and a lost one the same sight. Exactly
  `action.log`'s sixth decision, and a bigger batch does not weaken it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..catalog.base import LOAD_LOG_TABLE, CatalogError, Column, load_log_writer_for
from ..migrate.meta import loom_version

if TYPE_CHECKING:
    from ..catalog.base import Catalog, LoadLogWriter

UNKNOWN_ACTOR = "unknown"
"""What the log records when nobody named an actor. `action.log.UNKNOWN_ACTOR`'s spelling, and the
same argument: a confident wrong answer is worse than an admitted absence."""

# Every column optional but the two an empty value would make the row meaningless — `MetaStore`'s
# rule and `EDIT_COLUMNS`' rule, for the reason both state: this table is only ever *created*, so a
# column left out today can never reach a log that already exists. Generous on purpose.
LOAD_COLUMNS: tuple[Column, ...] = (
    Column("load_id", "string", required=True),
    Column("recorded_at", "timestamptz", required=True),
    Column("entry", "string", required=False),
    Column("actor", "string", required=False),
    Column("principal", "string", required=False),
    # Always null today, and here anyway. Ingest has no attested surface — it is a CLI command, and
    # `loom run` has carried a permanently-null `principal` for the same reason since M6. Adding the
    # column when a surface can fill it is the thing this table's schema rule forbids.
    Column("object_type", "string", required=False),
    Column("mode", "string", required=False),
    Column("catalog", "string", required=False),
    Column("table_name", "string", required=False),
    Column("source", "string", required=False),
    Column("source_fingerprint", "string", required=False),
    Column("status", "string", required=False),
    Column("rows_read", "long", required=False),
    Column("rows_written", "long", required=False),
    Column("rows_rejected", "long", required=False),
    Column("read_snapshot_id", "long", required=False),
    Column("failures", "string", required=False),
    Column("loom_version", "string", required=False),
)


def derive_load_id(entry: str, mode: str, fingerprint: str) -> str:
    """The identity of *this file, through this entry, in this mode*.

    Derived rather than random by default, and that choice is the whole retry story. A pipeline that
    times out and re-runs hands Loom the same file through the same entry, and the honest question is
    whether that is one load happening twice or two loads that happen to be identical. Loom answers
    *one*, because the alternative is silently doubling an append — and because an operator who meant
    the other thing can say so with `--load-id`, while an operator who did not mean it has no way to
    take back a duplicated batch.

    `mode` is in the hash because the same file appended and then merged are genuinely two different
    loads, and the second is a legitimate follow-up rather than a retry of the first.

    What this is *not* is a content-addressed store: two different files with the same bytes are the
    same load, which is correct, and one file loaded through two entries is two, which is also
    correct. The id names a decision, not a byte string."""
    payload = json.dumps([entry, mode, fingerprint], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def load_commit_properties(load_id: str, entry: str, actor: str) -> dict[str, str]:
    """What the bulk write stamps into its own Iceberg commit.

    `action.log.commit_properties`' twin, and the only record of a load that is atomic with the load
    — everything else, this module included, is a second commit a crash can land on the wrong side
    of. Three keys: who, which entry, and the id that ties the commit to the record beside it. A
    snapshot summary is metadata carried in every table-metadata read, so it is not the place for the
    counts, let alone the batch."""
    return {"loom.load_id": load_id, "loom.ingest": entry, "loom.actor": actor}


@dataclass(frozen=True)
class LoadRecord:
    """One run of one ingest entry, as the log holds it.

    `status` is the load's own status, with `previewed` excluded for `EditRecord`'s reason: a preview
    writes nothing, and `loom ingest` previews before every real load, so recording them would double
    the table. `applied`, `refused` and `failed` all reach here — including `refused`, because *who
    tried to replace this table* is as much an audit question as *who tried to delete this customer*,
    and including `failed`, which is the one case where nobody knows whether the rows landed and is
    therefore the case the record is most worth having.

    A refusal that never resolved an *entry* is not recorded, matching the edit log's gate: asking
    after an ingest that does not exist is a malformed command, not an attempted load, and belongs to
    whatever logs commands rather than to a table called `loads`."""

    load_id: str
    recorded_at: datetime
    entry: str
    actor: str
    object_type: str
    mode: str
    catalog: str
    table_name: str
    status: str
    source: str = ""
    source_fingerprint: str = ""
    rows_read: int = 0
    rows_written: int = 0
    rows_rejected: int = 0
    read_snapshot_id: int | None = None
    failures: Sequence[Mapping[str, Any]] = ()
    loom_version: str = ""
    principal: str | None = None

    def row(self) -> dict[str, Any]:
        return {
            "load_id": self.load_id,
            "recorded_at": self.recorded_at,
            "entry": self.entry,
            "actor": self.actor,
            "principal": self.principal,
            "object_type": self.object_type,
            "mode": self.mode,
            "catalog": self.catalog,
            "table_name": self.table_name,
            "source": self.source,
            "source_fingerprint": self.source_fingerprint,
            "status": self.status,
            "rows_read": self.rows_read,
            "rows_written": self.rows_written,
            "rows_rejected": self.rows_rejected,
            "read_snapshot_id": self.read_snapshot_id,
            "failures": json.dumps(list(self.failures), default=str) if self.failures else "",
            "loom_version": self.loom_version or loom_version(),
        }


@dataclass
class LoadLog:
    """The `_loom_meta.loads` table of one catalog, created on first write.

    Mirrors `EditLog`, which mirrors `MetaStore`: reads through the read port, writes through a write
    port, and the two references kept distinct so the read half stays usable against a catalog nobody
    can write to. `landed()` is the half `EditLog` has no equivalent of, and it exists because a load
    is the one write in Loom that a caller is expected to attempt twice."""

    catalog: Catalog
    writer: LoadLogWriter | None = None

    def history(self) -> tuple[dict[str, Any], ...]:
        """Every recorded load in this catalog, oldest first. Empty if the log was never created.

        Sorted by `recorded_at`, for `EditLog.history`'s reason: Iceberg promises no scan order and
        an audit trail read out of sequence is a trap."""
        if not self.catalog.table_exists(LOAD_LOG_TABLE):
            return ()
        rows = self.catalog.scan(LOAD_LOG_TABLE).to_pylist()
        return tuple(
            sorted(rows, key=lambda r: (r.get("recorded_at") or datetime.min, r.get("load_id") or ""))
        )

    def landed(self, load_id: str) -> dict[str, Any] | None:
        """The record of a load with this id that *changed the table*, or None.

        **`applied` only, and that is the decision this method is.** A refused load wrote nothing, so
        re-running its id is not a duplicate — it is the retry the operator was supposed to make
        after fixing the file, and refusing it would make the first typo permanent. A `failed` load
        is the hard case and it counts as landed: nobody knows whether the rows committed, and
        between silently doubling an append and making an operator pass `--load-id` to say "I
        checked, do it anyway", the second is the one that cannot lose data.

        This reads the *log*, not the table's snapshot history, and the gap that leaves is stated
        rather than hidden: a crash between the commit and the record leaves a load that landed with
        no row here, so a re-run would not be caught. That is the same asymmetry `_record` chose and
        it is why the commit carries `loom.load_id` in its own snapshot summary — the ground truth is
        in the lake, and this table is an index over it. Reading snapshot summaries instead would
        have meant a new verb on the read port, on every catalog, to close a window that leaves
        evidence."""
        for row in self.history():
            if row.get("load_id") == load_id and row.get("status") in ("applied", "failed"):
                return row
        return None

    def ensure(self) -> None:
        """Make sure this catalog can hold a load log, by making one. Idempotent."""
        if self.writer is None:  # pragma: no cover - the caller resolves a writer first
            raise RuntimeError("LoadLog has no writer — nothing can be created")
        self.writer.ensure_load_log(LOAD_COLUMNS)

    def record(self, entry: LoadRecord) -> None:
        """Append one record, creating the log table if this catalog has never held one."""
        if self.writer is None:  # pragma: no cover - the runtime resolves a writer first
            raise RuntimeError("LoadLog has no writer — nothing can be recorded")
        self.writer.append_load(LOAD_COLUMNS, entry.row())


def require_load_log(catalogs: Mapping[str, Catalog], demanded: Sequence[str]) -> None:
    """`governance.edit_log: required` — refuse a deployment that cannot record what it loads.

    **The posture is not extended so much as finally applied to everything it always said.** Its own
    words are *a deployment that cannot log does not run*, and it was written when the only thing
    Loom could write was one row through a declared action. A deployment that demanded it and then
    bulk-loaded unrecorded would satisfy the letter of a check while contradicting the sentence, and
    that gap is precisely the one this milestone exists to close — it would be the *edit log is a
    half-truth* argument, reproduced inside the fix for it.

    What it can honestly promise is unchanged and worth restating, because a bigger write does not
    buy a stronger claim: Iceberg has no transaction spanning a table and `_loom_meta.loads`, so
    *every applied load is logged* is not available at any price. This says the thing that is true,
    about a deployment rather than about a load.

    `demanded` is the catalogs the declared entries write to, and the check *creates* the table
    rather than probing for it — `require_edit_log`'s argument exactly: `table_exists` asks the wrong
    question, since `False` is the ordinary state of a catalog whose first load has not happened, and
    creating a table records nothing that might not have happened. An empty log is a permission, not
    a table of intentions.

    Raises `PolicyError`, collecting every catalog rather than the first."""
    from ..governance import PolicyError

    problems: list[str] = []
    for name in sorted(set(demanded)):
        catalog = catalogs.get(name)
        if catalog is None:
            # A catalog the config never declared already fails every load against it, naming
            # itself. That is an unloadable deployment rather than an unrecordable one, and
            # restating it here would report one fault as two.
            continue
        try:
            LoadLog(catalog=catalog, writer=load_log_writer_for(catalog)).ensure()
        except CatalogError as e:
            problems.append(f"  - catalog '{name}': {e}")

    if problems:
        raise PolicyError(
            "'governance.edit_log' is 'required', but this deployment cannot record what it loads:\n"
            + "\n".join(problems)
            + f"\n  every declared ingest writes rows, and a load that cannot be recorded in "
            f"'{LOAD_LOG_TABLE}' is the write this posture exists to refuse"
        )


def now() -> datetime:
    return datetime.now(UTC)
