[← Roadmap index](../ROADMAP.md)

# ✅ Done — M9: Bulk ingest — a batch becomes rows, and the lake says so

*Goal: the plane `_loom_meta.edits` could not see.*

**What this reverses, and why on purpose.** `examples/retail/seed.py` carried the position in its own
docstring: *"Bulk loading is the user's concern — the framework's claim is that it can serve, migrate
and act on what's in the lake, not that it is the way data gets there."* That was not a gap waiting
to be filled; it was a decision, and this milestone changes it. The reason is that the sentence next
to it stopped being true. `governance.edit_log: required` says *a deployment that cannot record what
it writes does not run*, and a deployment satisfying it precisely could answer "what did this actor
do today" for every single-row agent write and have **nothing at all** to say about the four-million-row
overwrite that actually moved the numbers. The edit log was a half-truth, and ingest is the plane
where the missing half lives.

The evidence was in the repo. `examples/retail/sales_performance.py` is a bulk write that already
existed here: a hand-built Arrow schema kept in lockstep with the spec by hand, a `txn.overwrite`,
and hand-rolled `refreshed_at` / `source_table` / `source_snapshot_id` provenance columns. Every one
of those is something Loom already knows. Both halves now ship side by side, and a test asserts they
produce the *same table* — so what the declared entry adds is not different rows, it is a contract
check and a record.

**Scope, stated as what it is not.** Loom reads a **file** — `parquet`, `ndjson`, `csv`. It does not
connect to Kafka, crawl an object store or open a JDBC connection. A pipeline hands Loom a batch;
Loom decides whether that batch may become rows. Adding a format is a small decision about parsers;
adding a source would be a large one about what Loom is.

- [x] `BulkWriter` — a fifth port, and it opens no new plane
- [x] `LoadLogWriter` + `_loom_meta.loads` — a sixth port, and a third `_loom_meta` table
- [x] `ingest:` in `loom.yaml`, `governance.ingest`, and `edit_log: required` extended to both logs
- [x] `loom ingest <entry> <file>` with `--dry-run` / `--load-id` / `--reject-to`
- [x] `examples/retail` declares a real entry; the acceptance case is the two paths agreeing

**Six decisions taken here, and three of them are refusals.**

- **`BulkWriter` is a fifth port that opens no new plane.** It writes rows, which `RowWriter` already
  did. It is separate because of what `RowWriter` *is*: every verb there is singular and keyed on
  purpose, which is how §4's single-object boundary is enforced at the bottom of the stack and not
  merely at spec-load. A batch verb added there would hand the action runtime a multi-row write and
  dissolve the guarantee the port exists to make. `CatalogWriter.append_rows` was the other tempting
  home — right shape, wrong neighbours: it sits beside `alter_table`, so a loader holding it could
  migrate the table it is loading into.

  **And it has no DDL verb, permanently. Ingest never migrates.** A batch that does not fit is refused
  naming the column, and the fix is `loom plan` / `loom apply` — the never-drop rule pointed at a new
  plane, refusing to infer a schema change from the shape of somebody's file.

- **`append` asserts no snapshot; `merge` and `replace` require one.** The asymmetry with `RowWriter`,
  where all three verbs are checked, is the decision rather than an inconsistency. An append follows
  no read and puts no row over another — `EditLogWriter`'s argument at batch scale — so there is no
  honest value to pass, and requiring one would make two pipelines loading one table refuse each
  other over a race neither can lose. A merge's carried columns come from a read, so committing over
  a moved table writes somebody else's newer value back to what it used to be. A replace reads
  nothing and destroys everything, which sounds like the append case and is its opposite: what it
  must not do is destroy a commit nobody saw.

- **`ingest:` lives in `loom.yaml` and not in the ontology, and that placement is what keeps it off
  the tool surface.** §7 says the tool set is a function of the spec. Declared in the spec, something
  would have to decide whether an `ingest_<type>` tool appears — and the answer has to be no, for the
  reason `loom serve` exposes no raw-SQL tool. Declared in the deployment config, no tool can be
  *derived* from it, structurally rather than by a rule someone remembers. It is now the one declared,
  named, runnable thing in Loom with no row in §7's table.

- **`governance.ingest` defaults to `refused`, which points the opposite way to `edit_log`'s
  default** — and both are right under the same test: *what does a deployment that never asked for
  this get?* One that never asked for the `edit_log` posture is not asking to stop working; one that
  never asked for bulk writes is not asking to become bulk-writable. An upgrade that shipped ingest
  and a config that happens to describe a load are two things that happen for unrelated reasons, and
  their product must not be somebody's lake quietly gaining a way to be overwritten wholesale.

- **A load has an identity, and re-running one is a refusal.** Derived from the entry, the mode and a
  SHA-256 of the file's bytes unless `--load-id` overrides it; stamped into the write's own Iceberg
  commit as `loom.load_id`. A pipeline that times out and retries hands Loom the same file, and the
  honest question is whether that is one load happening twice or two identical loads. Loom answers
  *one*, because an operator who meant the other can say so and an operator who did not has no way to
  take back a doubled append. The duplicate check reads the *log*, not snapshot summaries, and the
  gap that leaves is stated rather than hidden: a crash between the commit and the record leaves a
  landed load with no row, so a re-run would not be caught — which is why the stamp is in the lake and
  this table is an index over it. Reading summaries instead would have meant a new verb on the read
  port, on every catalog, to close a window that leaves evidence.

- **`_loom_meta.loads` is a third table, not more rows in `edits`.** That table's columns are forever
  — `append_edit` only ever creates it — and they are action-shaped: `action`, `operation`,
  `object_key`, `before`, `after`, `attempts`. A load has none of those and four things `edits` can
  never grow a column for. The precedent is one level up and points the same way: `applied` was not
  folded into anything either. And there is **no `before`/`after` here at all** — an action's record
  carries them because a handful of values fit in a row; a load's answer is the batch, and copying it
  would make the table an unabridged second copy of somebody's nightly drop.

**Three refusals, and the third one was found by building it.**

- **Governance does not condition a load, and it is said rather than assumed.** A `mask:` withholds a
  property from a caller and a load has no caller; a `rows:` predicate decides which rows a deployment
  will *show*, which is not a claim about which rows may exist; a `when:` guard is unanswerable where
  nothing attests anybody, exactly as for `loom query`. The one place this could have gone the other
  way is a mask over a property an entry loads — the action runtime refuses that pairing at bind time
  — and the reason there is leak-through-report: `before`/`after` carry the value back out. A load's
  record carries counts and a fingerprint and no values at all, so there is nothing to leak.

- **`--reject-to` cannot absorb a duplicate key.** Whole-batch refusal is the default, because a
  partial load leaves the lake in a state nobody declared, and the escape hatch is deliberately narrow:
  it quarantines rows that failed **their own** checks. Choosing which of two rows sharing a primary
  key survives is a decision the file does not contain, and Loom will not invent one — so
  `duplicate_key` refuses the batch even with the flag. A null primary key is refused in every mode
  for a neighbouring reason: M7 refused `{"prop": null}` as a filter permanently, so a row loaded
  under a null key is one the ontology can describe and never retrieve.

- **A zero-byte file cannot empty a table, and no special case is what makes that true.** The design
  going in said `mode: replace` with an empty batch empties the table, and it does — but only when the
  source *declares columns and zero rows*. An empty NDJSON declares nothing, so it fails the ordinary
  column check every batch faces. That turned out to be the safer answer and it needed no code: a
  truncated upload and a deliberate empty batch are the same zero bytes, and one of them wipes a
  table. A header-only CSV or an empty Parquet table can say *these columns, and no rows*; NDJSON
  cannot, and both directions are tests.

**Two things review turned into decisions rather than fixes**, and both are about telling *absent*
apart from *empty* — which is the shape of nearly everything that went wrong in this slice.

- **A column the batch does not have is left out of the row, never written as null.** Writing null
  was the obvious reading of "this row has no value for that property", and under `merge` it
  destroyed the one thing the mode exists to protect: `_carry_across` lays the batch's row over the
  stored one, so a null overwrote a **mapped** property while the unmapped columns beside it survived
  untouched — the exact inverse of the promise, and invisible in the direction anyone would check.
  Leaving the key out carries the stored value instead, and `append`/`replace` are unchanged, because
  `_batch` builds against the table's own schema and an absent key is null there anyway. Whether an
  absent column is acceptable *at all* stays a batch-level question, asked once in `_check_columns`.
- **A preview records nothing, including a refusal** — `ActionRuntime.run`'s rule, which this had
  quietly diverged from by recording refusals during `dry_run`. Restoring it exposed the thing that
  divergence was covering for: `loom ingest` previews before every real load, so a refusal found at
  preview would never be recorded at all, and *who tried to replace this table* would be answerable
  only for loads that worked. The command now runs the refusing load for real unless `--dry-run` was
  asked for. Nothing is written either way, so there is nothing to confirm first — and the record
  ends up describing an attempt somebody actually made rather than a question they asked.

  This is where `loom ingest` deliberately parts company with `loom run`, which returns on a refused
  preview and therefore does not log CLI-side refusals either. The asymmetry is the subject matter:
  an action refusal is one row a caller asked about; a load refusal is somebody pointing a file at a
  whole table.

**One thing this leaves owing.** A `merge` reads its target with a full scan, because `Catalog.scan`
takes a conjunction of equality pairs and a batch of N keys has no pushdown spelling — it is one scan
of the table or N scans of it, and one is cheaper. Correctness does not depend on it (the filter is
applied in the runtime regardless), and the channel that would fix it is exactly the one the
range-pushdown backlog entry describes.

**Probed as a client (2026-08-25): a quarantine described itself as a refusal.** A three-row drop
with two bad rows, loaded with `--reject-to`, applies — one row written, two set aside, exit 0,
`status: applied`. It said otherwise at every step. The preview above the `y/N` marked the
set-aside rows with `!`, the refusal mark, and then printed `nothing was written.` — telling the
operator the opposite of what pressing `y` was about to do. The summary afterwards printed one
`error:` line per quarantined row, which is the same text a genuinely refused load prints, so
nothing reading the output could tell the two apart.

`QUARANTINABLE` moved out of the runtime and into `ingest.result` so the CLI and the runtime decide
"is this a row `--reject-to` absorbed" with one set rather than two spellings. A quarantined row now
renders `·` and reports as `rejected:`; `nothing was written.` is printed only when something
actually refused. A row failure with no `--reject-to` is unchanged — the same code, the same
message, still a refusal.

---

[← M8](./m08-in-filter.md) · [M10 →](./m10-semantic-search.md) · [backlog](./backlog.md)
