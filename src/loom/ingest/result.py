"""What a load says happened — typed, never an opaque string.

`action.result`'s counterpart on the bulk plane, and it borrows that module's `Failure` rather than
defining a second one. That is deliberate and worth stating, because a duplicate would have been the
easy thing: `Failure` is `code` + `message` + `detail` + `retryable`, which is not an action concept,
and two copies of it would be two answers to "is a conflict retryable" that agree right up until
somebody edits one. What is *not* shared is the code vocabulary — the sets overlap where the meaning
is identical (`type_error` is a value that could not be read as its declared type, here as there) and
diverge where it is not, because a load has failures a single-row action cannot have and vice versa.

The alternative was to lift `Failure` into a module of its own. That is probably where it belongs and
it is deliberately not done here: it would touch every import of `action.result` and every test that
names one, to move a class that is not moving anywhere. A later slice that needs a third plane can
pay for it.

Two rules carry across unchanged, because they are about how a result is *shaped* rather than about
what produced it:

- **Nothing an operator, a file or the data can cause is an exception.** A missing column, a value
  that will not coerce, a duplicate key, a table that moved — all come back as a `Failure` on a
  refused `IngestResult`. Exceptions are for programming errors and for asking after an entry that
  does not exist, which is a call that never named a load at all.
- **Every failure is reported, not just the first.** An operator fixing one bad column per run is as
  miserable as an author fixing one typo per run — and worse here, because each attempt costs a file
  read of somebody's nightly drop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..action.result import APPLIED, FAILED, PREVIEWED, REFUSED, Failure

__all__ = [
    "APPLIED",
    "FAILED",
    "PREVIEWED",
    "REFUSED",
    "AMBIGUOUS_KEY",
    "CONFLICT",
    "DUPLICATE_KEY",
    "DUPLICATE_LOAD",
    "DEPLOYMENT_REFUSED",
    "LOG_FAILED",
    "MISSING_COLUMN",
    "NULL_KEY",
    "SOURCE_ERROR",
    "TABLE_MISSING",
    "TYPE_ERROR",
    "UNMAPPED_COLUMN",
    "WRITE_FAILED",
    "Failure",
    "IngestResult",
]

# --- failure codes --------------------------------------------------------------
#
# Spelled the same as `action.result`'s where they mean the same thing, so an operator who has read
# one vocabulary has read both. `type_error`, `conflict`, `write_failed` and `log_failed` are
# imported spellings restated as literals rather than re-exported names, because their *detail*
# shapes differ here and a reader following the name to `action.result` would find the wrong
# documentation for the payload.

DEPLOYMENT_REFUSED = "deployment_refused"
"""This deployment does not bulk-load. `governance.ingest` is `refused`, which is its default.

Not an error about the entry, the file or the data — all three may be perfectly good. It is the
deployment declining to be the kind of thing that does this, and it is reported as a failure rather
than raised so that `--dry-run` against a refused deployment still tells an operator whether the load
*would* have worked."""

MASKED_PROPERTY = "masked_property"
"""A `governance.policies` mask withholds a property of the object type this entry loads.

The bulk-plane spelling of the refusal `bind_policies` makes about an action: *withhold the property
or perform the write, not both*. A mask is a statement that nobody reading this deployment may see a
value, and a load is a write of that value — `append` sets it, `replace` sets it for every row in the
table, and `merge` reads it to carry it across. A deployment doing both says two things about one
column, and the one that would go unnoticed is the write: a masked column is invisible in every tool,
every `loom query` and every action's `before`/`after`, so nobody reading this deployment could ever
see what the load put there.

**Per entry, not per deployment**, which is where it differs from the action refusal. An action names
the properties it writes in its own effects, so the pairing of a spec and a policy is decidable
whole and a deployment that cannot stand refuses to start. An entry names an object type and a file
supplies the columns, so the decidable unit is *this entry loading that type* — and refusing the
deployment would take down loads of every other type with it, including the ones a governed
deployment exists to keep running (the retail dashboard masks `Customer.ltv` and refreshes
`DailySalesPerformance` on a timer).

Reported rather than raised, for `DEPLOYMENT_REFUSED`'s reason: `--dry-run` against a load this
deployment will not perform should still say so, and say it before the file is opened."""

SOURCE_ERROR = "source_error"
"""The file could not be read as the format the entry declares."""

UNMAPPED_COLUMN = "unmapped_column"
"""The batch carries a column no property of the target object type claims.

Refused rather than dropped, and this is the load-path twin of §2 rule 7's *unmanaged column*, with
the sign flipped. There, a column the spec does not map is somebody else's data and Loom leaves it
alone. Here, the column is arriving *from* the caller — so ignoring it would mean silently discarding
data an operator believed they were loading, which is the failure mode a `columns:` typo produces
every time."""

MISSING_COLUMN = "missing_column"
"""A property the target requires has no column in the batch, or the table requires a column no
property fills.

The first is only ever about a property that is **not nullable**: one that is may legitimately be
absent, and lands as null. The second is only ever about `append` and `replace`, which write whole
rows from the batch — a `merge` reads the row that is already there and carries such a column
across."""

TABLE_MISSING = "table_missing"
"""The target's backing table is not in the catalog.

Its own code rather than a `write_failed`, because it is the one failure whose fix is a different
command. **Ingest never creates or alters a table** — the `BulkWriter` port has no DDL verb — so a
missing table is `loom apply`'s to make and this says so, instead of surfacing a storage error three
layers down."""

AMBIGUOUS_KEY = "ambiguous_key"
"""A key in the batch matches more than one row already in the table.

`action.result.AMBIGUOUS_KEY`'s spelling and its meaning exactly — the backing table violates the
uniqueness the spec declares — reached here only by `merge`, where an equality-delete over a
duplicated key would remove both rows and append one. Loom cannot repair it; it can only decline to
make it worse."""

TYPE_ERROR = "type_error"
"""A value could not be read as its property's declared type. Row-level, and therefore the one code
`--reject-to` can quarantine rather than refuse the load over."""

NULL_KEY = "null_key"
"""A row's primary key is null.

Refused in every mode, including the ones where the storage layer would accept it. A null key names
an object no surface can address: `get_<type>` cannot ask for it, and M7 refused `{"prop": null}` as
a filter permanently, so a row loaded under one is a row the ontology can describe and never
retrieve. Loading it would be Loom manufacturing exactly the state it refuses to let a caller
express."""

DUPLICATE_KEY = "duplicate_key"
"""Two rows of the batch share a primary key.

Refused in every mode, and it is the one key check that is free: it needs the batch and not the
table. In `merge` and `replace` it is fatal outright — one equality-delete plus two appends is a
duplicate row. In `append` it manufactures the `ambiguous_key` state `_Run._read` refuses forever
afterwards and which Loom can never repair, so it is refused there too rather than left to become
somebody's unfixable table."""

DUPLICATE_LOAD = "duplicate_load"
"""A load with this id already landed in this catalog.

The retry guard, and the reason a load has an id at all. See `ingest.log.derive_load_id` for what the
id is when nobody supplies one, and why re-running the same file through the same entry is a refusal
rather than a second load."""

EMPTY_REPLACE = "empty_replace"
"""A `replace` whose every row was quarantined, so the batch it would write is empty.

**`--reject-to` can manufacture the zero-row batch `source.Batch` refuses to read off disk**, and
this is the guard on the other side of it. That docstring's argument is that a truncated upload and a
deliberate empty batch are the same zero bytes, so an empty NDJSON fails the ordinary column check
rather than emptying a table. A quarantine reaches the same state by a different road: the columns
were all there, every row failed its own type check, and what is left to write is nothing. Under
`append` and `merge` that is a no-op; under `replace` it is a truncate, and it was reported
`applied`.

Refused **before** the quarantine file is written, which is the ordering `_Run.execute` already
argues for: a file describing a subset of a batch that was then declined whole is "the one reading of
`--reject-to` that is not true", and a batch every row of which was set aside is declined whole.

It is not the same failure as a batch that legitimately shrinks. A `replace` that quarantines two of
ten rows really does mean *this table is now those eight*, which is what the mode says it does.
Nothing survives is the case where the flag stops absorbing rows and starts deciding the table."""

QUARANTINABLE = frozenset({TYPE_ERROR, NULL_KEY})
"""The codes `--reject-to` may set a row aside over instead of refusing the whole batch.

Row-level, both of them: the failure is a fact about one row and about no other, which is what makes
absorbing it the operator's call rather than a partial load Loom decided on. Everything else in this
module is a fact about the batch, the entry or the table, and no per-row file can answer it.

Named here rather than spelled out at each of its three uses — the runtime gates the two refusal
branches on it, and the CLI needs the same set to tell a *quarantined* row from a refused load. When
those two disagreed, a load that applied printed its set-aside rows as `error:` under a preview that
said `nothing was written`."""

CONFLICT = "conflict"
"""The table moved between the read and the write, and the write was declined.

Reachable from `merge` and `replace` only — an `append` asserts nothing, because it reads nothing.
Retryable, and deliberately *not* retried in-process the way an action's conflict is: an action
re-reads one row and re-evaluates rules against it in milliseconds, while a load re-reads a table and
rewrites a batch. Absorbing that silently would turn one operator-visible refusal into an unbounded
amount of work nobody asked for. The `detail` carries `expectedSnapshotId` and `foundSnapshotId`, and
the answer is to run it again."""

WRITE_FAILED = "write_failed"
"""The write itself failed after every check had passed."""

LOG_FAILED = "log_failed"
"""The load happened; recording it in `_loom_meta.loads` did not.

Never changes the status beside it, for `action.result.LOG_FAILED`'s reason exactly: by the time the
record is written the rows have committed, and reporting `failed` would tell an operator to re-run a
load that already landed — which, for `append`, doubles it. The commit stamped `loom.load_id` into
its own Iceberg snapshot, so what a lost record leaves is a findable gap rather than silence."""

@dataclass(frozen=True)
class IngestResult:
    """The whole outcome of one `loom ingest`.

    **The row counts are three numbers and not two**, and the third is what makes the pair honest.
    `rows_read` is what the source held, `rows_written` is what committed, `rows_rejected` is what
    `--reject-to` actually wrote somewhere. Without the third, a load that quietly dropped half a
    file would report a smaller `rows_written` and nothing else, and the two numbers would have to be
    *compared* to notice — which is the arithmetic nobody does at 3am. With it, an applied load
    always satisfies `rows_read == rows_written + rows_rejected`, so a mismatch is a bug in Loom
    rather than something an operator has to check for.

    `rows_rejected` counts **quarantined** rows and not merely unacceptable ones, which is why a
    refused load reports zero of them however many rows were bad. The whole batch was declined and
    nothing was set aside; saying otherwise would describe a partial load that did not happen, and
    would break the identity above on exactly the results that get read most carefully.

    **`rows_written` is zero on every refusal**, and that is the promise this result exists to make:
    a refused load changes nothing it was asked to change. Every check runs before the single write
    call, and a conflict is declined inside the commit rather than undone after it — the same
    sentence `action.result.REFUSED` makes, and it is even simpler here because there is no partial
    write to reason about. One load is one commit.

    `load_id` is the load's identity, minted or derived before the write so the write can stamp it
    into its own Iceberg commit. Empty only when the load was refused before it acquired one.

    `read_snapshot_id` is the snapshot the pre-write read saw and the write asserted — `None` for an
    `append`, which reads nothing and asserts nothing, and that `None` is a fact about the mode
    rather than a missing value. `concurrency` beside it says which."""

    entry: str
    object_type: str
    mode: str
    catalog: str
    table: str
    status: str
    load_id: str = ""
    source: str = ""
    rows_read: int = 0
    rows_written: int = 0
    rows_rejected: int = 0
    read_snapshot_id: int | None = None
    failures: tuple[Failure, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in (APPLIED, PREVIEWED)

    @property
    def retryable(self) -> bool:
        """Asked of the `Failure` rather than re-derived from a set of codes here.

        A second `RETRYABLE` frozenset in this module would be the duplicate the module docstring
        argues against — and it would agree with `action.result`'s exactly until the day somebody
        added a code to one of them."""
        return any(f.retryable for f in self.failures)

    @property
    def concurrency(self) -> str:
        """What the snapshot id beside it does and does not claim.

        Mode-dependent as well as status-dependent, which is one more axis than an action's needs.
        An append genuinely asserts nothing and says so in words rather than by showing a `null` an
        operator would have to interpret — *no check* and *the check passed* must not look alike.

        The mode axis was here from the start and the status axis was not, so this claimed the one
        thing `ActionResult.concurrency` had already been corrected for saying: a `replace` refused
        before it opened its source file came back `status: refused`, `readSnapshotId: null` and
        `concurrency: enforced`. Nothing was read and nothing was written, and the sentence named
        the check that makes `replace` — the mode that empties a table — safe to run at all. Status
        is asked **first** for the same reason it is on the action plane: a refusal that never
        reached a write has no mode-dependent story to tell, and *no check* and *the check passed*
        must not look alike there either. A **conflict** keeps "enforced" and is why this is not a
        plain `status != APPLIED` — it refused precisely because the check was carried in."""
        if self.status == PREVIEWED:
            return "not checked — a preview writes nothing, and holds nothing"
        if self.status == REFUSED and not self.retryable:
            return "not reached — this load refused before the write, so nothing was asserted"
        if self.mode == "append":
            return "not asserted — an append reads nothing and puts no row over another"
        return "enforced — the write asserts the snapshot the read saw"

    def as_json(self) -> dict[str, Any]:
        return {
            "entry": self.entry,
            "objectType": self.object_type,
            "mode": self.mode,
            "catalog": self.catalog,
            "table": self.table,
            "status": self.status,
            "loadId": self.load_id,
            "source": self.source,
            "rowsRead": self.rows_read,
            "rowsWritten": self.rows_written,
            "rowsRejected": self.rows_rejected,
            "readSnapshotId": self.read_snapshot_id,
            "concurrency": self.concurrency,
            "failures": [f.as_json() for f in self.failures],
        }
