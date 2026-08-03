"""`_loom_meta.edits` — what an action did to a row, recorded in the lake it did it to.

`_loom_meta.applied` records what `apply` did to schemas. Nothing recorded what an action did to
data, and this is that: an append-only Iceberg table per catalog, sitting in the namespace Loom
already owns, holding one row per run.

Six decisions shape it, and each had an obvious-looking alternative:

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

- **The rows are forever too, and that is a sixth decision rather than an omission.** Nothing here
  or on the port removes a record. `_record` writes after the commit so that a lost record is a
  *findable* gap — the row write stamped `loom.edit_id` into its own Iceberg snapshot, so a stamp
  with no matching row means one thing — and an expiry that deleted rows would make an expired edit
  and a lost edit the same sight. So the erasure this table genuinely owes (§9.2: declared
  properties outlive the row they describe) can only ever be a **redaction in place** — the row
  kept, its payload emptied — by a command Loom has not built, holding a port that is not the
  runtime's. `require_edit_log` below is the half of that question this milestone did answer.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..catalog.base import EDIT_LOG_TABLE, CatalogError, Column, edit_log_writer_for
from ..migrate.meta import loom_version

if TYPE_CHECKING:
    from ..catalog.base import Catalog, EditLogWriter
    from ..model import Ontology

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
    # Beside `actor`, never instead of it. `actor` is true about a *deployment* and `principal` is
    # true about a *caller*, and both are true at once — a run arriving through a bot's credentials
    # at a deployment declared `agent:support-bot` has an honest answer to "which deployment" and an
    # honest answer to "who", and they are different answers. Collapsing them would make the log
    # unable to distinguish two callers of one deployment, which is the whole thing this milestone
    # added. Issuer-qualified (`auth.Principal.label`), because a `sub` is unique only per issuer and
    # a bare one silently merges two people the day a second issuer is trusted.
    Column("principal", "string", required=False),
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

    `principal` is the attested caller, and it is `None` on every surface that cannot attest one —
    `loom run`, `loom query`, and a stdio server, permanently and by construction. Its absence is
    therefore not a gap in the record but a fact about the deployment that produced it: a log whose
    rows all carry `None` was written by a deployment where nobody could be named, which is exactly
    what `actor` beside it already said.
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
    principal: str | None = None

    def row(self) -> dict[str, Any]:
        return {
            "edit_id": self.edit_id,
            "recorded_at": self.recorded_at,
            "actor": self.actor,
            "principal": self.principal,
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

    def ensure(self) -> None:
        """Make sure this catalog can hold a log, by making one. Idempotent.

        Called before any run rather than during one — see `require_edit_log`, which is the only
        caller and carries the argument for why a deployment proves this at startup instead of
        probing it per write."""
        if self.writer is None:  # pragma: no cover - the caller resolves a writer first
            raise RuntimeError("EditLog has no writer — nothing can be created")
        self.writer.ensure_log(EDIT_COLUMNS)

    def record(self, entry: EditRecord) -> None:
        """Append one record, creating the log table if this catalog has never held one.

        Raises `CatalogError` on failure and does not swallow it — but the caller does not treat that
        as the action failing, because by the time this runs the row write has already committed. See
        `_Run`'s caller in `runtime.py`."""
        if self.writer is None:  # pragma: no cover - the runtime resolves a writer before calling
            raise RuntimeError("EditLog has no writer — nothing can be recorded")
        self.writer.append_edit(EDIT_COLUMNS, entry.row())


def require_edit_log(ontology: Ontology, catalogs: Mapping[str, Catalog]) -> None:
    """`governance.edit_log: required` — refuse a deployment that cannot record what it writes.

    **What this can promise, and what nothing can.** Iceberg has no transaction spanning a row's
    table and `_loom_meta.edits`, so *every applied run is logged* is not available at any price and
    this check does not claim it. What it claims is exact and is about a deployment rather than a
    run: **one that cannot log does not start.** Unloggability comes in two kinds, and both are
    knowable *before* any row is written, which is what makes this a startup question rather than a
    per-run one:

    - **Structural**, and permanent — the catalog implements no `EditLogWriter`, so every run
      against it writes its row and reports `log_failed` afterwards for as long as the deployment
      lives. A fact about the pairing of a spec and a deployment, checked where every other such
      fact is checked.
    - **Physical**, and provable only by doing it — the `_loom_meta` namespace cannot be created, or
      the table cannot be. So this *creates the table* rather than probing for it. Creating one
      records nothing that might not have happened, so it does not reopen the ordering `_record`
      chose: an empty log is a permission, not an intention.

    **The per-run probe was refused, and not only because it narrows the window rather than closing
    it.** The deeper objection is that it is nearly blind. The log lives in the *same catalog* as
    the row it describes, so a catalog nobody can reach already fails the row write itself, with
    nothing written and nothing to record. The failures worth catching are specific to the log
    table, and the only probe that sees those is an append — which is log-then-write, a table of
    intentions that may never have happened. So the whole of this posture is spent at startup, and
    no round trip is added to the path of every action.

    **Nothing after the write changes, under either posture.** `_record` still runs after the commit,
    and an append that fails there still comes back as `log_failed` beside an unchanged status,
    because *the row committed, so `failed` would tell a caller to retry a delete that already
    happened* is not an argument a policy weakens. What survives that window is what always survived
    it: the row write stamped `loom.edit_id` into its own Iceberg commit, so a lost record is a
    stamped snapshot with no matching row — a gap a reader can find. Which is also why no Loom
    command deletes from this table: an expired record and a lost one would be the same sight.

    Raises `PolicyError`, and collects every catalog rather than the first, for `check_capabilities`'
    reason: an operator reconciling a posture with a deployment should learn the whole of what
    disagrees in one reading."""
    from ..governance import PolicyError

    demanded = _catalogs_actions_write_to(ontology)
    problems: list[str] = []
    for name in sorted(demanded):
        catalog = catalogs.get(name)
        if catalog is None:
            # A catalog the config never declared already fails every run of those actions, with
            # `ActionRuntime.catalog_for`'s message. That is an unwritable deployment rather than an
            # unloggable one, and restating it here would report one fault as two.
            continue
        by = ", ".join(f"'{a}'" for a in sorted(demanded[name]))
        try:
            EditLog(catalog=catalog, writer=edit_log_writer_for(catalog)).ensure()
        except CatalogError as e:
            problems.append(f"catalog '{name}' (written by action(s) {by}) — {e}")

    if not problems:
        return
    lines = ["governance.edit_log is 'required' and this deployment cannot record what it writes:"]
    lines += [f"  - {p}" for p in problems]
    lines.append("Every catalog an action writes to has to be able to hold its own")
    lines.append(f"'{EDIT_LOG_TABLE}'. Point those catalogs at a backend Loom can append to, or set")
    lines.append("'governance.edit_log: optional' — under which a run whose record cannot be")
    lines.append("written still happens and reports 'log_failed' afterwards.")
    raise PolicyError("\n".join(lines))


def require_principal_column(ontology: Ontology, catalogs: Mapping[str, Catalog]) -> None:
    """Refuse a deployment that would attest a caller and then fail to record one.

    **This exists because the drop would otherwise be silent, which is the worst available
    outcome.** `append_edit` builds its Arrow batch against the *table's own* schema, and
    `pa.Table.from_pylist` ignores keys the schema does not have. So a log table created before this
    slice — one with no `principal` column — accepts every append, reports success, and discards the
    attested caller. Nothing fails, nothing warns, and the record says the run had no principal,
    which is indistinguishable from a run that genuinely had none.

    That is the exact trap this module's docstring named as *the columns are forever*: a column left
    out yesterday cannot reach a log table that already exists. The escape it named — put unsettled
    things in a JSON column — does not apply, because a principal is not unsettled, and burying an
    audited identity inside a JSON blob is a worse answer than a column.

    **So the answer is a refusal rather than a widened port.** Two alternatives were rejected:

    - *Evolve the table.* `EditLogWriter` would gain a verb that alters a table, and the port's whole
      guarantee is that the runtime holds nothing that can point DDL at anything — `ensure_log` is
      `append_edit` with the row removed precisely so the verb count buys nothing new. A test
      enumerates those verbs; this would break it, and the guarantee it asserts is worth more than
      the convenience.
    - *Record the principal anyway and accept the loss.* That is a field written and never read,
      inverted into something worse: a field written, silently dropped, and believed.

    Checked only when the deployment can actually attest somebody (`McpConfig.attests`), so nothing
    that worked before this slice starts refusing: a deployment with no `auth:` records `None` in a
    column it may or may not have, and `None` is what an absent column already yields.

    Raises `PolicyError`, collecting every catalog, for `require_edit_log`'s reason."""
    from ..governance import PolicyError

    demanded = _catalogs_actions_write_to(ontology)
    problems: list[str] = []
    for name in sorted(demanded):
        catalog = catalogs.get(name)
        if catalog is None:
            continue
        try:
            if not catalog.table_exists(EDIT_LOG_TABLE):
                # Nothing to be incompatible with. The first append creates it from `EDIT_COLUMNS`,
                # which now carries `principal`.
                continue
            if "principal" not in catalog.describe(EDIT_LOG_TABLE).columns:
                problems.append(
                    f"catalog '{name}' (written by action(s) "
                    f"{', '.join(repr(a) for a in sorted(demanded[name]))}) — its "
                    f"'{EDIT_LOG_TABLE}' predates attested identity and has no 'principal' column"
                )
        except CatalogError as e:  # pragma: no cover - an unreadable log is require_edit_log's fault to report
            problems.append(f"catalog '{name}' — {e}")

    if not problems:
        return
    lines = ["'mcp.auth' attests a caller that this deployment's edit log cannot record:"]
    lines += [f"  - {p}" for p in problems]
    lines.append("An append against a table without that column succeeds and drops the principal,")
    lines.append("so every run would be recorded as though nobody was named. Add the column to")
    lines.append(f"'{EDIT_LOG_TABLE}' (a nullable string) in those catalogs, or point them at a")
    lines.append("catalog that has never held a log — the first append creates it with the column.")
    raise PolicyError("\n".join(lines))


def _catalogs_actions_write_to(ontology: Ontology) -> dict[str, list[str]]:
    """Which catalogs this deployment could ever write to, and the actions that would do it.

    A spec that declares no action leaves this empty, and both postures above then refuse nothing —
    not a lenient answer, an empty subject."""
    demanded: dict[str, list[str]] = {}
    for action in ontology.actions.values():
        target = ontology.object_types.get(action.target_object_type)
        if target is None:  # pragma: no cover - validator-enforced
            continue
        demanded.setdefault(target.backing_catalog, []).append(action.api_name)
    return demanded


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
