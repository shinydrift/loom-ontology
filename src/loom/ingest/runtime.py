"""The ingest runtime — one declared load, one commit, and a record that it happened.

The whole of a load is six steps, in this order and no other:

1. **Read.** The file, in the format the entry declared. Nothing is interpreted yet.
2. **Identify.** The load's id — supplied, or derived from the entry, the mode and the file's
   fingerprint — and a refusal if a load with that id already landed in this catalog.
3. **Check the batch.** Its columns against the target's properties, and the target's table against
   what a batch can fill. Everything here refuses the whole load: a column that is not there cannot
   be quarantined.
4. **Check the rows.** Every value coerced to its property's declared type, every key checked. These
   are the failures `--reject-to` can put in a file instead of refusing over.
5. **Write.** One call to one `BulkWriter` verb, which is one Iceberg commit.
6. **Record.** One row in `_loom_meta.loads`, after the commit, through a port that can name no
   table.

Everything that can refuse happens in 1-4, so **a load that refuses changes nothing it was asked to
change** — `loom apply`'s promise and `ActionRuntime`'s, and simpler than either, because there is no
partial state to reason about: one load is one commit.

Four boundaries this file is careful about:

- **It holds a `BulkWriter`, never a `CatalogWriter`.** Ingest never migrates. A batch that does not
  fit the table is refused naming the column, and the fix is `loom plan` / `loom apply`. The port has
  no DDL verb, so this is a fact about what the runtime *can* do rather than a check it performs.

- **It does not go through the resolver, and for the opposite reason the action runtime does not.**
  There, the resolver's projection was too narrow for a write that has to carry unmapped columns
  across. Here there is no read to project at all in two of three modes — a load is values arriving
  from outside, and the only thing that decides whether they may become rows is the type system.

- **Governance does not condition a load, and that is stated rather than assumed.** A `mask:`
  withholds a property from a caller and a load has no caller; a `rows:` predicate decides which rows
  a deployment will *show*, which is not a claim about which rows may exist. Conditioning a load on
  either would be inventing a meaning for them that §6.1 never gave them — and a `when:` guard is
  unanswerable here for the reason `loom run` cannot answer one: there is no transport, so there is
  nobody to attest. What governs ingest is `governance.ingest`, which is a posture about the
  deployment, and `governance.edit_log`, which is a demand that it be able to say what it did.

  The one place this could have gone the other way is a mask over a property an entry loads. The
  action runtime refuses that pairing at bind time — an action that reads or writes a masked property
  makes the deployment refuse to start — and the reason is leak-through-report: `before` and `after`
  would carry the value back out. A load's record carries counts and a fingerprint and no values at
  all, so there is nothing to leak and nothing to refuse.

- **It never invents an actor.** `default_actor()` is honest for `loom ingest` — a person at a
  terminal or a CI job that set `LOOM_ACTOR` — and is called by the CLI rather than here, so that a
  future surface passing its own attested identity does not have to un-inherit one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..catalog.base import (
    Catalog,
    CatalogError,
    ConcurrencyError,
    bulk_writer_for,
    load_log_writer_for,
)
from ..config import INGEST_APPEND, INGEST_MERGE, INGEST_REPLACE, IngestEntry, LoomConfig
from ..governance import EDIT_LOG_REQUIRED, INGEST_ALLOWED
from ..model import ObjectType, Ontology, coerce_value
from .log import (
    UNKNOWN_ACTOR,
    LoadLog,
    LoadRecord,
    derive_load_id,
    load_commit_properties,
    now,
    require_load_log,
)
from .result import (
    AMBIGUOUS_KEY,
    APPLIED,
    CONFLICT,
    DEPLOYMENT_REFUSED,
    DUPLICATE_KEY,
    DUPLICATE_LOAD,
    FAILED,
    LOG_FAILED,
    MISSING_COLUMN,
    NULL_KEY,
    PREVIEWED,
    QUARANTINABLE,
    REFUSED,
    SOURCE_ERROR,
    TABLE_MISSING,
    TYPE_ERROR,
    UNMAPPED_COLUMN,
    WRITE_FAILED,
    Failure,
    IngestResult,
)
from .source import Batch, SourceError, read_batch, write_rejects

MAX_REPORTED_ROWS = 20
"""How many row-level failures are reported before the rest are counted rather than listed.

*Every failure is reported* is the rule this bends and the bend is deliberate: a million-row file
with a mis-typed column produces a million identical failures, and a result carrying them is one no
terminal prints and no log stores. So the *kinds* are all reported — nothing is hidden — and the
examples are bounded. `--reject-to` is what an operator uses when they want every offending row,
and it writes them to a file rather than to a result."""


class IngestError(RuntimeError):
    """The caller asked for something the deployment doesn't declare — an ingest entry that isn't
    there, an entry naming an object type that isn't. Distinct from a `Failure`, which is a load
    that ran and refused."""


@dataclass
class IngestRuntime:
    """Runs the declared loads of one deployment against one set of catalogs."""

    ontology: Ontology
    catalogs: Mapping[str, Catalog]
    entries: Mapping[str, IngestEntry] = field(default_factory=dict)
    posture: str = INGEST_ALLOWED

    def load(
        self,
        entry_name: str,
        source: str | Path,
        *,
        actor: str | None = None,
        load_id: str | None = None,
        dry_run: bool = False,
        reject_to: str | Path | None = None,
    ) -> IngestResult:
        """One declared load, once, recorded once, and the last word either way.

        **Not retried.** An action's conflict is retried in-process because the check it comes from
        asserts a whole table's snapshot, so unrelated traffic refuses runs that were perfectly
        correct, and something has to absorb the conflicts that check invents. The same argument does
        not reach here: absorbing a conflict means re-reading the table and rebuilding the batch, and
        doing that silently turns one refusal an operator can see into an unbounded amount of work
        nobody asked for. A load is also, unlike an action, something a person is usually watching.

        `reject_to` is the escape hatch from *whole-batch refusal*, and it is deliberately narrow: it
        moves the rows that failed **their own** checks into a file and loads the rest. It cannot
        rescue a load whose columns are wrong, because there is no such thing as the subset of a
        batch that has the right columns."""
        entry = self.entries.get(entry_name)
        if entry is None:
            known = ", ".join(sorted(self.entries)) or "none"
            raise IngestError(f"unknown ingest entry '{entry_name}' (declared: {known})")
        target = self.ontology.object_types[entry.object_type]
        run = _Load(self, entry, target, actor or UNKNOWN_ACTOR, dry_run=dry_run)
        return run.execute(source, load_id=load_id, reject_to=reject_to)

    def preview(self, entry_name: str, source: str | Path) -> IngestResult:
        """Everything but the write. `loom plan` for a batch."""
        return self.load(entry_name, source, dry_run=True)

    def catalog_for(self, obj: ObjectType) -> Catalog:
        catalog = self.catalogs.get(obj.backing_catalog)
        if catalog is None:
            raise IngestError(
                f"objectType '{obj.api_name}' is backed by catalog '{obj.backing_catalog}', "
                f"which is not declared in loom.yaml"
            )
        return catalog


@dataclass
class _Load:
    """One execution. Holds failures as they accumulate, so exactly one place decides whether the
    write happens."""

    rt: IngestRuntime
    entry: IngestEntry
    target: ObjectType
    actor: str
    dry_run: bool = False
    failures: list[Failure] = field(default_factory=list)
    rejected: list[Mapping[str, Any]] = field(default_factory=list)
    quarantined: bool = False
    """Whether the rejected rows were actually written somewhere.

    Distinct from `rejected` being non-empty, and the distinction is what keeps `IngestResult`'s
    three counts honest. A refused load has bad rows *and* wrote none of them anywhere — reporting
    them as `rowsRejected` would say a subset was set aside when in fact the whole batch was
    declined, and would break `rows_read == rows_written + rows_rejected` on exactly the results an
    operator reads most carefully."""

    # ---- the six steps ---------------------------------------------------------

    def execute(
        self,
        source: str | Path,
        *,
        load_id: str | None,
        reject_to: str | Path | None,
    ) -> IngestResult:
        catalog = self.rt.catalog_for(self.target)
        table = self.target.backing_table

        if self.rt.posture != INGEST_ALLOWED:
            # Reported before the file is opened: a deployment that does not load has no business
            # reading somebody's data to tell them so. Reported rather than raised so that a
            # `--dry-run` still says whether the load *would* have worked.
            self._fail(
                DEPLOYMENT_REFUSED,
                f"this deployment does not perform bulk loads — 'governance.ingest' is "
                f"'{self.rt.posture}'",
                {"entry": self.entry.name},
            )
            return self._result(REFUSED, source=str(source))

        # 1. read
        try:
            batch = read_batch(source, self.entry.format)
        except SourceError as e:
            self._fail(SOURCE_ERROR, str(e), {"format": self.entry.format})
            return self._result(REFUSED, source=str(source))

        # 2. identify — before any row is examined, so a re-run of a load that already landed costs
        # a log scan rather than a million coercions.
        identity = load_id or derive_load_id(self.entry.name, self.entry.mode, batch.fingerprint)
        landed = LoadLog(catalog=catalog).landed(identity)
        if landed is not None:
            self._fail(
                DUPLICATE_LOAD,
                f"load '{identity}' already {landed.get('status')} in catalog "
                f"'{catalog.name}' at {landed.get('recorded_at')} — refusing to run it again",
                {
                    "loadId": identity,
                    "recordedAt": str(landed.get("recorded_at")),
                    "status": landed.get("status"),
                    "rowsWritten": landed.get("rows_written"),
                },
            )
            return self._refuse(catalog, identity, batch)

        # From here the load has named itself, which is what makes it an attempted load rather than
        # a malformed command — and therefore what makes it worth a row in the log. `_refuse` below
        # records; the two returns above it do not, and `_refuse`'s docstring carries why.

        # 3. check the batch
        mapping = self._mapping()
        self._check_columns(batch, mapping)
        self._check_table(catalog, table)
        if self.failures:
            return self._refuse(catalog, identity, batch)

        # 4. check the rows
        rows = self._rows(batch, mapping)
        if self.failures and (reject_to is None or not self._only_row_failures()):
            # A quarantine file absorbs rows, never a batch: `duplicate_key` refuses here even with
            # `--reject-to`, because which of two rows sharing a key to set aside is a decision the
            # source does not contain.
            return self._refuse(catalog, identity, batch)

        # 4b. prepare — the read the two reading modes need, *before* the dry-run branch, so a
        # preview meets every refusal a run would. It costs the real load a second scan, which is
        # the price `loom run` already pays for previewing before every run.
        prepared, snapshot = self._prepare(catalog, table, rows)
        if self.failures and not self._only_row_failures():
            return self._refuse(catalog, identity, batch, snapshot=snapshot)

        if self.rejected:
            # **After `_prepare`, not before it.** A quarantine file is the input to the next
            # attempt rather than a record of what happened, so it is written before the load
            # commits — but writing it before the last refusal could fire would leave a file on disk
            # describing a subset of a batch that was then declined whole, which is the one reading
            # of `--reject-to` that is not true.
            try:
                write_rejects(reject_to, self.rejected)  # type: ignore[arg-type]
            except OSError as e:
                self._fail(SOURCE_ERROR, f"could not write rejected rows to '{reject_to}': {e}")
                return self._refuse(catalog, identity, batch, snapshot=snapshot)
            self.quarantined = True

        if self.dry_run:
            # Not recorded, and this is the one gate that is about the *status* rather than about
            # how far the load got: a preview writes nothing, and `loom ingest` previews before every
            # real load, so recording them would put two rows in the log for every one load.
            return self._result(
                PREVIEWED, load_id=identity, batch=batch, written=len(prepared), snapshot=snapshot
            )

        # 5. write
        stamp = load_commit_properties(identity, self.entry.name, self.actor)
        try:
            self._write(catalog, table, prepared, snapshot, stamp)
        except ConcurrencyError as e:
            self._fail(
                CONFLICT,
                f"'{table}' moved between the read and the write, so the load was declined — "
                f"nothing was written; run it again",
                {"table": table, "expectedSnapshotId": e.expected, "foundSnapshotId": e.found},
            )
            return self._refuse(catalog, identity, batch, snapshot=snapshot)
        except CatalogError as e:
            self._fail(WRITE_FAILED, str(e), {"table": table})
            return self._record(
                self._result(FAILED, load_id=identity, batch=batch, snapshot=snapshot),
                catalog,
                batch,
            )

        # 6. record
        result = self._result(
            APPLIED, load_id=identity, batch=batch, written=len(prepared), snapshot=snapshot
        )
        return self._record(result, catalog, batch)

    def _refuse(
        self, catalog: Catalog, identity: str, batch: Batch, snapshot: int | None = None
    ) -> IngestResult:
        """A refusal that named itself, recorded.

        **The gate is the load id, which is `run.addressed`'s counterpart.** The edit log records a
        refusal once the run got as far as naming a *row*, on the grounds that an audit trail of
        successes cannot answer *who tried to delete this customer*. The same question here is *who
        tried to replace this table*, and it is worth as much — so every refusal from step 3 onward
        writes a row.

        The two refusals above the gate do not, and what that gives up is worth stating rather than
        leaving to be noticed. A `source_error` means the file could not be read, so there is no
        batch, no fingerprint and no id: the record would carry an empty key and cite nothing, which
        is a *request* log rather than a load log — the same boundary `_record` drew for a call that
        never reached an object. And an `ingest_refused` is the deployment declining to be the kind
        of thing that loads at all; recording it would mean creating `_loom_meta.loads` in exactly
        the deployments that set `governance.ingest: refused` to avoid having one.

        **A preview records nothing, including a refusal.** `ActionRuntime.run` returns before
        `_record` on a dry run and this matches it: `loom ingest` previews before every real load, so
        a preview that recorded would put a row in the log for a load nobody has agreed to run yet —
        and would create `_loom_meta.loads` in a catalog whose operator was only asking a question.
        What keeps the audit claim true is the command rather than this method: when the operator did
        not pass `--dry-run`, `cmd_ingest` runs the load for real after a refused preview, so the
        refusal that gets recorded belongs to a run somebody actually attempted. `cmd_sequence` owes
        the same debt and pays it the same way, through `SequenceRuntime.record_stop` — it declines to
        run the *order* after a refused rehearsal, but still runs the one load that stopped it, or the
        refusal would be recorded nowhere."""
        if self.dry_run:
            return self._result(REFUSED, load_id=identity, batch=batch, snapshot=snapshot)
        return self._record(
            self._result(REFUSED, load_id=identity, batch=batch, snapshot=snapshot), catalog, batch
        )

    def _only_row_failures(self) -> bool:
        """Whether everything recorded so far is a row a `--reject-to` has already quarantined.

        The gate on the two refusal branches after step 4: row-level failures are the operator's to
        absorb by quarantining, and anything else refuses the load whatever they asked for."""
        return all(f.code in QUARANTINABLE for f in self.failures)

    # ---- 3. the batch ----------------------------------------------------------

    def _mapping(self) -> dict[str, str]:
        """Property name -> source column name, with the identity as the default.

        Built over *every* declared property rather than over `entry.columns`, so a property the
        entry never mentions is still looked for under its own name. That is what makes `columns:`
        an override list rather than a whitelist — a spec that gains a property does not silently
        stop loading it."""
        return {
            name: self.entry.columns.get(name, name) for name in self.target.properties
        }

    def _check_columns(self, batch: Batch, mapping: Mapping[str, str]) -> None:
        """The batch's columns against the target's properties, both ways.

        Both directions are errors and they are different errors. A source column no property claims
        is data the operator believes they are loading and Loom would discard — the mirror image of
        §2 rule 7's unmanaged column, where the data is already in the lake and leaving it alone is
        the right answer. A property with no source column is a value the row will not have, which
        matters only if it may not be null."""
        claimed = {source: prop for prop, source in mapping.items()}
        unmapped = [c for c in batch.columns if c not in claimed]
        if unmapped:
            self._fail(
                UNMAPPED_COLUMN,
                f"{len(unmapped)} column(s) in the source are not mapped by any property of "
                f"{self.target.api_name}: {', '.join(sorted(unmapped))}",
                {"columns": sorted(unmapped)},
                # The refusal is the right one and it used to end here, which left the most natural
                # first journey with nowhere to go: `loom infer` drafts a type *and* an `ingest:`
                # entry from a file, and if that file holds one `array<T>`/`struct`/`map` column —
                # which the spec has no name for — the drafted entry cannot load the file it was
                # drafted from. `loom infer` says so, in a comment, in the file the operator
                # redirected somewhere else. Saying it here is saying it where it happens.
                hint=(
                    f"a column no property claims is refused rather than dropped, so this batch "
                    f"cannot be narrowed for you — either map {'it' if len(unmapped) == 1 else 'them'} "
                    f"by adding a property to {self.target.api_name} (`loom plan` then `loom apply` "
                    f"for the column), or leave the column unmanaged and load a file without it. A "
                    f"column whose type the spec has no name for — `array<T>`, `struct`, `map` — can "
                    f"only take the second route"
                ),
                hint_columns=sorted(self.target.properties),
            )

        present = set(batch.columns)
        pk = self.target.primary_key
        for name, prop in self.target.properties.items():
            source = mapping[name]
            if source in present:
                continue
            if name == pk:
                self._fail(
                    MISSING_COLUMN,
                    f"the source has no column '{source}' for the primary key "
                    f"{self.target.api_name}.{name} — every row would be unaddressable",
                    {"property": name, "column": source},
                )
            elif not prop.nullable:
                self._fail(
                    MISSING_COLUMN,
                    f"the source has no column '{source}' for {self.target.api_name}.{name}, "
                    f"which is not nullable",
                    {"property": name, "column": source},
                )

    def _check_table(self, catalog: Catalog, table: str) -> None:
        """That the table exists and that a batch could fill it.

        **The first half is where ingest's no-DDL rule becomes visible to an operator.** A table that
        is not there is not a table this load creates; it is one `loom apply` creates, and saying so
        is more useful than a storage error three layers down.

        The second half only applies where the whole row comes from the batch. A `merge` reads the
        existing row and carries every unmapped column across, so a required physical column no
        property maps is filled by the row that is already there — the same carry-across
        `RowWriter.replace_row` requires, and the reason a merge into a table with columns Loom
        cannot name is fine while an append into one is not."""
        if not catalog.table_exists(table):
            self._fail(
                TABLE_MISSING,
                f"table '{table}' does not exist in catalog '{catalog.name}' — ingest never "
                f"creates or alters a table",
                {"table": table},
                hint="run 'loom plan' and 'loom apply' to create it from the spec, then load into it",
            )
            return
        if self.entry.mode == INGEST_MERGE:
            return
        try:
            schema = catalog.describe(table)
        except CatalogError as e:  # pragma: no cover - it existed a moment ago
            self._fail(WRITE_FAILED, str(e), {"table": table})
            return
        mapped = {prop.column for prop in self.target.properties.values()}
        orphaned = sorted(
            col.name for col in schema.columns.values() if col.required and col.name not in mapped
        )
        if orphaned:
            self._fail(
                MISSING_COLUMN,
                f"'{table}' requires column(s) {', '.join(orphaned)}, which no property of "
                f"{self.target.api_name} maps — a '{self.entry.mode}' writes whole rows, so there "
                f"is nothing to fill them from",
                {"table": table, "columns": orphaned},
                hint="map them as properties, or use 'mode: merge', which carries existing values across",
            )

    # ---- 4. the rows -----------------------------------------------------------

    def _rows(self, batch: Batch, mapping: Mapping[str, str]) -> list[dict[str, Any]]:
        """Source rows to physical rows: every value coerced, every key checked.

        Coerced through `model.coerce_value` — the same function the read path binds a filter with
        and the action runtime binds a parameter with. A file reader that interpreted its own values
        would be a third answer to *is `"42"` a long*, and the two that exist agree on purpose.

        A row that fails is not written, and whether the *load* fails with it is `reject_to`'s
        business, decided by the caller. What is not the caller's business is which rows are
        acceptable: a rejected row is rejected either way."""
        pk = self.target.primary_key
        pk_column = self.target.pk_property.column
        present = set(batch.columns)
        rows: list[dict[str, Any]] = []
        seen_keys: dict[Any, int] = {}
        duplicates: list[Any] = []

        for index, source_row in enumerate(batch.rows):
            row, problems = self._coerce(source_row, mapping, present, index)
            if problems:
                self.rejected.append({**dict(source_row), "_loom_rejected": problems})
                continue
            key = row.get(pk_column)
            if key is None:
                self._reject_row(source_row, index, NULL_KEY, f"row {index}: {pk} is null")
                continue
            if key in seen_keys:
                # Batch-level, and therefore never quarantined: choosing which of two rows carrying
                # one key survives is a decision the file does not contain and Loom will not invent.
                duplicates.append(key)
                continue
            seen_keys[key] = index
            rows.append(row)

        if duplicates:
            shown = sorted({str(k) for k in duplicates})[:MAX_REPORTED_ROWS]
            self._fail(
                DUPLICATE_KEY,
                f"{len(duplicates)} row(s) repeat a {pk} already in the batch: {', '.join(shown)}"
                + ("…" if len(duplicates) > len(shown) else ""),
                {"property": pk, "keys": shown, "count": len(duplicates)},
                hint="one key cannot name two rows — deduplicate the source; Loom will not choose",
            )
        return rows

    def _coerce(
        self,
        source_row: Mapping[str, Any],
        mapping: Mapping[str, str],
        present: set[str],
        index: int,
    ) -> tuple[dict[str, Any], list[str]]:
        """One source row to one physical row, keyed by column name.

        Returns the row and the reasons it is unacceptable — both, rather than raising on the first,
        so a quarantine file says everything wrong with a row instead of one thing per attempt."""
        row: dict[str, Any] = {}
        problems: list[str] = []
        for name, prop in self.target.properties.items():
            source = mapping[name]
            if source not in present:
                # **A column the batch does not have is left out of the row entirely, not written as
                # null**, and the difference is the whole correctness of `merge`. `_carry_across`
                # lays this row over the stored one, so a `None` here would overwrite a value that is
                # already in the table — nulling a *mapped* property while the unmapped ones beside
                # it survive, which is the exact inverse of what the mode promises. Leaving the key
                # out carries it instead. For `append` and `replace` the outcome is unchanged:
                # `_batch` builds against the table's own schema, so an absent key is null anyway.
                #
                # Whether an absent column is acceptable at all is `_check_columns`' question, asked
                # once for the batch rather than once per row.
                continue
            raw = source_row.get(source)
            if raw is None:
                if not prop.nullable and name != self.target.primary_key:
                    problems.append(f"{name} is null, and the property is not nullable")
                row[prop.column] = None
                continue
            try:
                row[prop.column] = coerce_value(
                    prop.type, raw, self.rt.ontology.object_types, f"property '{name}'"
                )
            except ValueError as e:
                problems.append(str(e))
                row[prop.column] = None
        if problems:
            self._fail_row(index, problems)
        return row, problems

    def _fail_row(self, index: int, problems: Sequence[str]) -> None:
        if sum(1 for f in self.failures if f.code == TYPE_ERROR) >= MAX_REPORTED_ROWS:
            return
        self._fail(TYPE_ERROR, f"row {index}: {'; '.join(problems)}", {"row": index})

    def _reject_row(self, source_row: Mapping[str, Any], index: int, code: str, message: str) -> None:
        self.rejected.append({**dict(source_row), "_loom_rejected": [message]})
        if sum(1 for f in self.failures if f.code == code) < MAX_REPORTED_ROWS:
            self._fail(code, message, {"row": index})

    # ---- 5. write --------------------------------------------------------------

    def _prepare(
        self, catalog: Catalog, table: str, rows: Sequence[Mapping[str, Any]]
    ) -> tuple[list[dict[str, Any]], int | None]:
        """The rows as they will be written, and the snapshot the write will assert.

        **Everything that reads happens here, before the dry-run branch and before the write**, which
        is what keeps *a load that refuses changes nothing it was asked to change* true of the modes
        that read: a merge over a duplicated key is a refusal, and a refusal discovered inside the
        write path would be one discovered too late to be a refusal.

        Returns `None` for an append's snapshot, which reads nothing and asserts nothing."""
        if self.entry.mode == INGEST_APPEND:
            return list(rows), None
        # The snapshot *before* the rows, for `_Run.execute`'s reason at batch scale: it makes the
        # recorded id at-or-before the data, so the check can report a conflict that was not one but
        # can never miss one that was.
        snapshot = catalog.current_snapshot_id(table)
        if self.entry.mode == INGEST_REPLACE:
            return list(rows), snapshot
        return self._carry_across(catalog, table, rows), snapshot

    def _write(
        self,
        catalog: Catalog,
        table: str,
        rows: Sequence[Mapping[str, Any]],
        snapshot: int | None,
        stamp: Mapping[str, str],
    ) -> None:
        """One verb, one commit, and — for the two modes that read — the snapshot carried into it.

        The writer is asked for here rather than held, so the runtime keeps no bulk-writable typed
        reference between loads and the plane it is asking for is visible at the call site. By the
        time this runs every decision has been made: it dispatches and does not check."""
        writer = bulk_writer_for(catalog)
        if self.entry.mode == INGEST_APPEND:
            writer.append_batch(table, rows, commit_properties=stamp)
        elif self.entry.mode == INGEST_REPLACE:
            writer.replace_table(table, rows, expect_snapshot_id=snapshot, commit_properties=stamp)
        else:
            writer.merge_batch(
                table,
                self.target.pk_property.column,
                rows,
                expect_snapshot_id=snapshot,
                commit_properties=stamp,
            )

    def _carry_across(
        self, catalog: Catalog, table: str, rows: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """The existing row under each key, with the batch's values laid over it.

        §4.1's rule at batch scale, and it is why `merge` exists as a mode rather than as a flag on
        `append`: a merge is an equality-delete plus an append, so every column the ontology does not
        map has to be carried across or the write silently nulls it. The same columns `loom plan`
        reports as unmanaged and leaves alone — the never-drop rule one level down, where the data
        is.

        **This is a full scan, and that is a real cost stated rather than hidden.** `Catalog.scan`
        takes a conjunction of equality pairs, so a batch of N keys has no pushdown spelling: it is
        one scan of the table or N scans of it, and one is cheaper. An Iceberg-native adapter could
        prune on a key set, and the channel would have to carry more than a `(column, value)` pair to
        say so — exactly the shape of the range-pushdown entry already in the backlog. Correctness
        does not depend on it: the filter is applied here regardless.

        A key matching more than one existing row is refused, for `_Run._read`'s reason and with more
        at stake: an equality-delete over a duplicated key removes both rows and appends one."""
        key_column = self.target.pk_property.column
        wanted = {row.get(key_column) for row in rows}
        existing: dict[Any, dict[str, Any]] = {}
        ambiguous: set[Any] = set()
        for stored in catalog.scan(table).to_pylist():
            key = stored.get(key_column)
            if key not in wanted:
                continue
            if key in existing:
                ambiguous.add(key)
            existing[key] = stored
        if ambiguous:
            shown = sorted({str(k) for k in ambiguous})[:MAX_REPORTED_ROWS]
            self._fail(
                AMBIGUOUS_KEY,
                f"{len(ambiguous)} key(s) match more than one row in '{table}' "
                f"({', '.join(shown)}) — the backing table violates the uniqueness "
                f"{self.target.api_name}.{self.target.primary_key} declares, and a merge over a "
                f"duplicated key would delete both rows and append one",
                {"table": table, "keys": shown, "count": len(ambiguous)},
                hint="Loom cannot repair this — the duplicate rows are still there, and the fix is "
                "out of band",
            )
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            base = dict(existing.get(row.get(key_column), {}))
            base.update(row)
            out.append(base)
        return out

    # ---- 6. record -------------------------------------------------------------

    def _record(self, result: IngestResult, catalog: Catalog, batch: Batch | None) -> IngestResult:
        """One row in `_loom_meta.loads`, after the fact.

        **After the write, never before**, for `ActionRuntime._record`'s reason: log-then-write
        records intentions that may not have happened, and write-then-log loses records of writes
        that succeeded — but the second loss is *detectable*, because the commit stamped
        `loom.load_id` into its own Iceberg snapshot. A stamped snapshot with no matching row is a
        gap a reader can find.

        **A failed append does not fail the load.** By the time this runs the rows have committed,
        and returning `failed` would tell an operator to re-run a load that already landed — which,
        for an append, doubles it. It comes back as `log_failed` beside an otherwise unchanged
        status."""
        from dataclasses import replace

        entry = LoadRecord(
            load_id=result.load_id,
            recorded_at=now(),
            entry=self.entry.name,
            actor=self.actor,
            object_type=self.target.api_name,
            mode=self.entry.mode,
            catalog=self.target.backing_catalog,
            table_name=self.target.backing_table,
            status=result.status,
            source=result.source,
            source_fingerprint=batch.fingerprint if batch is not None else "",
            rows_read=result.rows_read,
            rows_written=result.rows_written,
            rows_rejected=result.rows_rejected,
            read_snapshot_id=result.read_snapshot_id,
            failures=[f.as_json() for f in result.failures],
        )
        try:
            LoadLog(catalog=catalog, writer=load_log_writer_for(catalog)).record(entry)
        except CatalogError as e:
            return replace(
                result,
                failures=(
                    *result.failures,
                    Failure(
                        code=LOG_FAILED,
                        message=f"the load was not recorded in the load log: {e}",
                        detail={"loadId": result.load_id, "status": result.status},
                    ),
                ),
            )
        return result

    # ---- result ----------------------------------------------------------------

    def _fail(
        self,
        code: str,
        message: str,
        detail: Mapping[str, Any] | None = None,
        hint: str | None = None,
        hint_columns: Sequence[str] | None = None,
    ) -> None:
        payload = dict(detail or {})
        if hint:
            payload["hint"] = hint
        if hint_columns:
            payload["knownProperties"] = list(hint_columns)
        self.failures.append(Failure(code=code, message=message, detail=payload))

    def _result(
        self,
        status: str,
        load_id: str = "",
        batch: Batch | None = None,
        source: str | None = None,
        written: int = 0,
        snapshot: int | None = None,
    ) -> IngestResult:
        return IngestResult(
            entry=self.entry.name,
            object_type=self.target.api_name,
            mode=self.entry.mode,
            catalog=self.target.backing_catalog,
            table=self.target.backing_table,
            status=status,
            load_id=load_id,
            source=source if source is not None else (batch.path if batch else ""),
            rows_read=len(batch) if batch is not None else 0,
            rows_written=written,
            # Reported only where the identity `rows_read == rows_written + rows_rejected` holds:
            # a load that was accepted. A refusal set nothing aside whatever the quarantine file on
            # disk says — the whole batch was declined — and `failed` does not know what landed.
            rows_rejected=len(self.rejected) if self.quarantined and status in (APPLIED, PREVIEWED) else 0,
            read_snapshot_id=snapshot,
            failures=tuple(self.failures),
        )


def build_ingest(
    ontology: Ontology, config: LoomConfig, catalogs: Mapping[str, Any] | None = None
) -> IngestRuntime:
    """Pair this spec with this deployment on the bulk plane. Every static refusal lives here.

    `build_runtime`'s sibling, and it takes no engine for the same reason: a load bypasses the
    compute engine entirely, which is what keeps the write path identical across DuckDB, Trino and
    Spark.

    **Entries are resolved even when the posture refuses them.** A deployment that declares a load it
    will not perform is in a legitimate state — the same one a spec declaring actions is in under
    `mcp.writes: false` — and a typo in an entry is worth reporting either way. What the posture
    decides is whether a load *runs*, and that refusal is a `Failure` on a result rather than an
    exception here, so `--dry-run` still answers the question an operator is asking.

    **`governance.edit_log: required` is checked here as well as in `build_runtime`**, and only when
    the posture permits loads: proving a log a deployment will never write would create
    `_loom_meta.loads` in every catalog of every deployment that declared an entry and meant it for
    later. See `log.require_load_log` for what that posture can honestly claim.

    **And `governance.policies` is bound here, though this plane applies none of them.** A load has
    no caller to withhold a column from and no rows to filter — the operator running it is the
    deployment — so the program this builds is used for nothing but being buildable. That is the
    point: `bind_policies` is where a policy is checked against the spec it governs, and a
    deployment whose governance does not fit is one `loom query`, `loom run`, `loom serve` and
    `loom embed` all refuse to start. Without this line `loom ingest` was the exception, and an
    exception on the plane that writes whole tables: a mask naming a property an action writes took
    the read surface down and left bulk loads running. "Every static refusal lives here" is what
    this function claims, and a policy that does not fit the ontology is one of them."""
    from ..catalog import open_catalogs
    from ..governance import bind_policies

    open_cats = catalogs if catalogs is not None else open_catalogs(config)
    auth = config.mcp.auth
    bind_policies(ontology, config.policies, auth.claims if auth else {})
    entries = _resolve(ontology, config.ingest)
    posture = config.ingest_posture
    if posture == INGEST_ALLOWED and config.edit_log == EDIT_LOG_REQUIRED:
        require_load_log(
            open_cats,
            [ontology.object_types[e.object_type].backing_catalog for e in entries.values()],
        )
    return IngestRuntime(
        ontology=ontology, catalogs=open_cats, entries=entries, posture=posture
    )


def _resolve(ontology: Ontology, entries: Sequence[IngestEntry]) -> dict[str, IngestEntry]:
    """Every declared entry against the ontology, accumulating every problem.

    Accumulated rather than raised one at a time for `Diagnostics`' reason — an operator fixing one
    entry per run is as miserable as an author fixing one typo per run — and raised as one
    `IngestError` because by this point there is no `Diagnostics` to add to: the config parsed
    cleanly, and what is wrong is the *pairing*."""
    problems: list[str] = []
    out: dict[str, IngestEntry] = {}
    for entry in entries:
        target = ontology.object_types.get(entry.object_type)
        if target is None:
            known = ", ".join(sorted(ontology.object_types)) or "none"
            problems.append(
                f"  - ingest '{entry.name}' loads objectType '{entry.object_type}', which the "
                f"ontology does not declare (known: {known})"
            )
            continue
        unknown = sorted(set(entry.columns) - set(target.properties))
        if unknown:
            problems.append(
                f"  - ingest '{entry.name}' maps {', '.join(unknown)}, which "
                f"{target.api_name} does not declare"
            )
            continue
        clash = _aliased(entry, target)
        if clash:
            problems.extend(f"  - ingest '{entry.name}': {line}" for line in clash)
            continue
        out[entry.name] = entry
    if problems:
        raise IngestError(
            "the declared ingest entries do not fit this ontology:\n" + "\n".join(problems)
        )
    return out


def _aliased(entry: IngestEntry, target: ObjectType) -> list[str]:
    """Two properties reading one source column, over the **effective** mapping.

    `config._parse_ingest_columns` already refuses this among *declared* entries, and that check
    cannot see this one: the mapping every property actually gets is `columns` overlaid on the
    identity, so `columns: {name: tier}` makes `name` read the column `tier` while `tier` still reads
    it too — two declarations, one of them implicit, and no duplicate in the file to notice.

    Caught here rather than there because it needs the property list, which is the ontology's, and
    caught at all because the consequence is silent: one source value written into two physical
    columns, forever, with nothing raising."""
    seen: dict[str, str] = {}
    problems: list[str] = []
    for name in target.properties:
        source = entry.columns.get(name, name)
        if source in seen:
            problems.append(
                f"properties '{seen[source]}' and '{name}' both read source column '{source}' — "
                f"one of them by default, since a property with no entry in 'columns' reads a "
                f"column of its own name"
            )
        else:
            seen[source] = name
    return problems
