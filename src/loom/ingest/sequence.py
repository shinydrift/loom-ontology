"""An ordered run of declared loads — `loom ingest` several times, and a record that it was one run.

**A sequence is an order, not an atom, and everything here is shaped so that nobody can read it as
the second.** Iceberg's unit is the table; there is no cross-table transaction to be had. `apply`
met this first and answered it in the open — it "sequences tables, stops at the first failure, and
reports exactly which ones landed rather than pretending the run was atomic" — and a sequence of
loads inherits that verbatim, because what makes it true is one commit per table, which is the same
one level up. So the honest verbs are *stop* and *report*, never *roll back*: the loads that landed
before the stop are landed, and the result names them.

**The manifest is the file that varies.** `loom ingest` takes one data file on the command line
because that is the one thing that changes per run; a sequence needs several, so it takes one file
that names them. Everything else — which entries, in which order — stays in `loom.yaml` where it is
reviewed. A manifest that is missing an entry the sequence names, or that names one the sequence does
not, is refused before anything is read: a partial run is what running `loom ingest` per entry is
for, and a manifest quietly loading two of three tables is the failure mode this whole milestone is
against.

**What a sequence does not buy.** Nothing here checks referential integrity. Ordering customers
before orders makes the *result* coherent; it does not make Loom verify that every order's customer
arrived. Loom has no cross-table constraint to check and this does not add one — a sequence is an
order over loads, and reading it as integrity enforcement would be reading a schedule as a
guarantee.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..catalog.base import SEQUENCE_LOG_TABLE, CatalogError, Column, sequence_log_writer_for
from ..config import IngestSequence, LoomConfig
from ..governance import INGEST_ALLOWED
from ..migrate.meta import loom_version
from .log import UNKNOWN_ACTOR
from .result import APPLIED, PREVIEWED, REFUSED, IngestResult
from .runtime import IngestError, IngestRuntime

if TYPE_CHECKING:
    from ..catalog.base import Catalog

PARTIAL = "partial"
"""The status only a sequence can have: something landed, and then something did not.

Neither `applied` nor `refused` is true of it, and reporting it as either is the pretence this
module exists not to make. It has no counterpart in `IngestResult` because a single load is one
commit — it lands whole or not at all, so there is no half of it to name."""

SEQUENCE_STATUSES = (APPLIED, PARTIAL, REFUSED, PREVIEWED)

# `LOAD_COLUMNS`' rule, third application: every column optional but the two an empty value would
# make the row meaningless, because this table is only ever *created* and a column left out today can
# never reach a log that already exists.
SEQUENCE_COLUMNS: tuple[Column, ...] = (
    Column("sequence_id", "string", required=True),
    Column("recorded_at", "timestamptz", required=True),
    Column("sequence", "string", required=False),
    Column("actor", "string", required=False),
    Column("principal", "string", required=False),
    # Null today, as it is in `loads`, and here for the same reason: a sequence is a CLI command with
    # no attested caller, and adding the column when a surface can fill it is what this table's
    # schema rule forbids.
    Column("manifest", "string", required=False),
    Column("status", "string", required=False),
    # The order as declared, and the ids of the loads that were actually attempted. Two lists rather
    # than one: the difference between them is where the run stopped, and deriving that from a
    # single list would need the reader to know the sequence's declaration at the time it ran.
    Column("entries", "string", required=False),
    Column("loads", "string", required=False),
    Column("stopped_at", "string", required=False),
    Column("landed", "long", required=False),
    Column("attempted", "long", required=False),
    Column("loom_version", "string", required=False),
)


class SequenceError(RuntimeError):
    """The sequence, the manifest or their pairing is unusable. Raised before anything is loaded."""


def derive_sequence_id(name: str, load_ids: Sequence[str]) -> str:
    """The identity of *these files, through this sequence*.

    `derive_load_id`'s posture, one level up, and it has to be derived from the load ids rather than
    from the manifest: a nightly run points the same manifest at new files every night, so hashing
    the manifest would make every night the same run. Hashing what the loads will be called makes
    two runs identical exactly when every file in them is, which is the same question
    `derive_load_id` answers about one file — and it means the individual loads' own duplicate
    refusals and this id agree rather than contradicting each other."""
    payload = json.dumps([name, list(load_ids)], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class Step:
    """One load in a run: which entry, which file, and what happened."""

    entry: str
    source: str
    result: IngestResult | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None and self.result.ok


@dataclass
class SequenceResult:
    """What a run did, and where it stopped.

    `landed` is a count and `steps` is the list, because the two questions a reader has are "did this
    work" and "what is in the lake now" — and after a stop the second one has no shorter answer."""

    sequence: str
    manifest: str
    status: str
    steps: list[Step] = field(default_factory=list)
    sequence_id: str | None = None
    recorded: bool = False

    @property
    def ok(self) -> bool:
        return self.status in (APPLIED, PREVIEWED)

    @property
    def landed(self) -> list[Step]:
        return [s for s in self.steps if s.ok]

    @property
    def stopped_at(self) -> str | None:
        """The entry that refused, or None if the run finished. What `apply` reports as the failure
        point, and the thing a partial run's operator needs before they can decide anything."""
        for step in self.steps:
            if not step.ok:
                return step.entry
        return None

    def as_json(self) -> dict:
        return {
            "sequence": self.sequence,
            "sequenceId": self.sequence_id,
            "manifest": self.manifest,
            "status": self.status,
            "landed": len(self.landed),
            "attempted": len(self.steps),
            "stoppedAt": self.stopped_at,
            "recorded": self.recorded,
            "steps": [
                {
                    "entry": step.entry,
                    "source": step.source,
                    **({"load": step.result.as_json()} if step.result is not None else {}),
                }
                for step in self.steps
            ],
        }


def read_manifest(path: str | Path, sequence: IngestSequence) -> dict[str, str]:
    """`entry name -> file path`, checked against the sequence before anything is opened.

    **Paths resolve against the manifest's own directory, not the cwd.** A manifest is a description
    of a drop, and a drop is a directory of files beside it; resolving against the cwd would make the
    same manifest mean different things depending on where somebody stood when they ran it — exactly
    the hazard `_resolve_local_path` fixes for `warehouse:`.

    Both mismatches are refused, and they are different mistakes. An entry the sequence names and the
    manifest lacks is a run that would silently load two of three tables. An entry the manifest names
    and the sequence does not is a file somebody expects to land that nothing will read."""
    path = Path(path)
    try:
        doc = yaml.safe_load(path.read_text())
    except OSError as e:
        raise SequenceError(f"cannot read manifest '{path}': {e}") from e
    except yaml.YAMLError as e:
        raise SequenceError(f"manifest '{path}' is not valid YAML: {e}") from e

    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise SequenceError(
            f"manifest '{path}' must be a mapping of ingest entry name to file path"
        )

    listed = {str(k) for k in doc}
    wanted = set(sequence.loads)
    missing = [name for name in sequence.loads if name not in listed]
    if missing:
        raise SequenceError(
            f"manifest '{path}' has no file for {', '.join(missing)} — sequence "
            f"'{sequence.name}' runs {len(sequence.loads)} load(s) and a manifest that supplies "
            f"some of them would land part of the run and report success"
        )
    extra = sorted(listed - wanted)
    if extra:
        raise SequenceError(
            f"manifest '{path}' names {', '.join(extra)}, which sequence '{sequence.name}' does "
            f"not run — nothing would read {'them' if len(extra) > 1 else 'it'}"
        )

    out: dict[str, str] = {}
    for name in sequence.loads:
        value = doc[name]
        if not isinstance(value, str) or not value.strip():
            raise SequenceError(
                f"manifest '{path}': '{name}' must be a path to the file that load reads, "
                f"got {value!r}"
            )
        out[name] = str((path.parent / value.strip()).resolve())
    return out


@dataclass
class SequenceRuntime:
    """Drives an `IngestRuntime` through a declared order, and records that it was one run.

    It holds the ingest runtime rather than reimplementing any of it, which is the point: every
    refusal a single `loom ingest` would make is made here too, unchanged, and a sequence adds
    exactly two things — an order, and a record that the loads in it belonged together."""

    ingest: IngestRuntime
    sequences: Mapping[str, IngestSequence]
    catalog: Catalog | None = None

    def run(
        self,
        name: str,
        manifest: str | Path,
        *,
        actor: str = UNKNOWN_ACTOR,
        dry_run: bool = False,
    ) -> SequenceResult:
        sequence = self.sequences.get(name)
        if sequence is None:
            known = ", ".join(sorted(self.sequences)) or "none are declared"
            raise SequenceError(f"no sequence named '{name}' in loom.yaml — declared: {known}")

        if self.ingest.posture != INGEST_ALLOWED:
            # Raised up front, unlike `_Load`, which reports the same posture per load so a
            # `--dry-run` of one entry still answers. A sequence has no such answer to give: the
            # posture is a fact about the deployment, so attributing it to the first entry prints
            # `stops at 'customers'` over a run in which no entry could have worked and the manifest
            # was never at fault. It joins the refusals `read_manifest` makes before anything opens.
            raise SequenceError(
                f"this deployment does not perform bulk loads — 'governance.ingest' is "
                f"'{self.ingest.posture}', so sequence '{name}' cannot run any of its "
                f"{len(sequence.loads)} load(s)"
            )

        files = read_manifest(manifest, sequence)
        result = SequenceResult(
            sequence=name,
            manifest=str(manifest),
            status=PREVIEWED if dry_run else APPLIED,
        )

        for entry in sequence.loads:
            source = files[entry]
            try:
                loaded = self.ingest.load(entry, source, actor=actor, dry_run=dry_run)
            except IngestError as e:
                # A load that could not even be attempted — an entry naming an object type the
                # ontology lost, say. It stops the run like any other failure rather than raising
                # past the loads that already landed, which would leave the caller holding an
                # exception and no idea what is in the lake.
                result.steps.append(Step(entry=entry, source=source, result=None))
                result.status = PARTIAL if result.landed else REFUSED
                return self._record(result, actor, dry_run, note=str(e))
            result.steps.append(Step(entry=entry, source=source, result=loaded))
            if not loaded.ok:
                result.status = PARTIAL if result.landed else REFUSED
                return self._record(result, actor, dry_run)

        return self._record(result, actor, dry_run)

    def record_stop(self, preview: SequenceResult, *, actor: str = UNKNOWN_ACTOR) -> IngestResult | None:
        """Run the one load that stopped a rehearsal for real, so the refusal is recorded.

        **What this exists to repair.** `cmd_sequence` previews before it runs, and a refused preview
        does not go on to run the order — which is right, because a sequence row for an order nobody
        attempted is the intention-shaped record `_record` writes after the fact to avoid. But a
        preview records nothing, so until this method the refusal was recorded *nowhere*: the same
        bad batch left a `refused` row in `_loom_meta.loads` through `loom ingest` and no row at all
        through `loom sequence`. Under `governance.edit_log: required` — a posture whose whole claim
        is that a deployment which cannot record what it writes must not run — *who tried to replace
        this table* had no answer for anything attempted through a sequence.

        **Only the entry that stopped, and only ever that one.** The loads before it were previewed
        and wrote nothing, so there is nothing of theirs to record; the loads after it were never
        reached. Re-running the stopping entry is `cmd_ingest`'s own move — it runs a refused preview
        for real for exactly this reason — and it is safe for the same reason: the refusal is a
        property of the batch and the log, so it refuses again and `_refuse` records it.

        Returns the recorded result, or `None` when there is nothing to record: a run that did not
        stop, or one that stopped above `_refuse`'s gate, where a load never named itself and the row
        would cite nothing."""
        stopped = next((s for s in preview.steps if not s.ok), None)
        if stopped is None:
            return None
        try:
            return self.ingest.load(stopped.entry, stopped.source, actor=actor)
        except IngestError:
            # The rehearsal already reported this one; a load that cannot be attempted has no id and
            # would leave no row anyway, so there is nothing here a second failure could add.
            return None

    def _record(
        self, result: SequenceResult, actor: str, dry_run: bool, note: str | None = None
    ) -> SequenceResult:
        """One row in `_loom_meta.sequences`, after the fact.

        **After the run and never before**, for `IngestRuntime._record`'s reason: log-then-write
        records intentions that may not have happened. A preview records nothing, for that method's
        other reason — every real run previews nothing extra, but a dry run writes no rows at all, so
        recording one would put a row in the log for a run that did not happen.

        **A failed append does not fail the run.** By the time this runs the loads have committed and
        recorded themselves individually; what is lost is only the fact that they were one run."""
        result.sequence_id = derive_sequence_id(
            result.sequence, [s.result.load_id for s in result.steps if s.result and s.result.load_id]
        )
        # **The run's own `dry_run`, not the status.** A preview that stops halfway has a status of
        # `partial` — it is describing what would happen — and gating on the status would put a row
        # in the log for a run nobody performed. Which is the same distinction `_Load` draws by
        # branching on `self.dry_run` before it decides anything, and the one a status can never
        # carry: `previewed` says *this ran nothing*, and after a stop the interesting half of the
        # answer is what it would have done.
        if dry_run or self.catalog is None:
            return result

        row = {
            "sequence_id": result.sequence_id,
            "recorded_at": datetime.now(UTC),
            "sequence": result.sequence,
            "actor": actor,
            "principal": None,
            "manifest": result.manifest,
            "status": result.status,
            "entries": json.dumps([s.entry for s in result.steps]),
            "loads": json.dumps(
                [s.result.load_id for s in result.steps if s.result and s.result.load_id]
            ),
            "stopped_at": result.stopped_at,
            "landed": len(result.landed),
            "attempted": len(result.steps),
            "loom_version": loom_version(),
        }
        if note is not None:
            row["stopped_at"] = f"{result.stopped_at or ''}: {note}".strip(": ")
        try:
            sequence_log_writer_for(self.catalog).append_sequence(SEQUENCE_COLUMNS, row)
            result.recorded = True
        except (CatalogError, TypeError):
            # Reported through `recorded` rather than raised: the loads are in the lake either way,
            # and each carries its own row in `_loom_meta.loads`. What a caller loses is the grouping.
            result.recorded = False
        return result


def build_sequences(
    ontology, config: LoomConfig, catalogs: Mapping[str, Any]
) -> SequenceRuntime:
    """Pair the declared sequences with a runtime that can run them.

    The entry names were checked against `ingest:` at config load, so nothing is re-checked here —
    what this adds is `build_ingest`'s own refusals, which are the ones that need an ontology."""
    from ..governance import EDIT_LOG_REQUIRED, INGEST_ALLOWED
    from .runtime import build_ingest

    ingest = build_ingest(ontology, config, catalogs)
    catalog = _log_catalog(config, catalogs)
    if (
        catalog is not None
        and config.sequences
        and config.ingest_posture == INGEST_ALLOWED
        and config.edit_log == EDIT_LOG_REQUIRED
    ):
        # `build_ingest`'s condition, one table over: only when the deployment both declares
        # sequences and permits loads, so proving a log it will never write does not create
        # `_loom_meta.sequences` in a catalog whose operator only meant it for later.
        require_sequence_log(catalog)
    return SequenceRuntime(
        ingest=ingest,
        sequences={s.name: s for s in config.sequences},
        catalog=catalog,
    )


def _log_catalog(config: LoomConfig, catalogs: Mapping[str, Any]):
    """Where `_loom_meta.sequences` lives when a spec binds more than one catalog.

    The first catalog the config declares, which is a decision rather than an accident: a run is one
    fact, so it gets one row, and a row per catalog touched would make "how many sequences ran" a
    question about how many catalogs the deployment happens to have. `version` in `_loom_meta.applied`
    is global to the spec for the mirror-image reason."""
    for name in config.catalogs:
        if name in catalogs:
            return catalogs[name]
    return None


def require_sequence_log(catalog) -> None:
    """Create `_loom_meta.sequences` up front, for `require_load_log`'s reason.

    `governance.edit_log: required` is a demand about writes, and a sequence run is how a deployment
    makes several at once. A posture that proved two logs and not the third would leave a deployment
    able to run an unrecorded sequence while believing it could not."""
    try:
        sequence_log_writer_for(catalog).ensure_sequence_log(SEQUENCE_COLUMNS)
    except CatalogError as e:
        raise SequenceError(
            f"'governance.edit_log' is 'required' but {SEQUENCE_LOG_TABLE} cannot be created in "
            f"catalog '{getattr(catalog, 'name', '?')}': {e}"
        ) from e
