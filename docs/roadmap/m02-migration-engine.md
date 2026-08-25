[← Roadmap index](../ROADMAP.md)

# ✅ Done — M2: Migration engine (`plan` / `apply` / `rollback`)

*Goal: edit the YAML, run `loom plan`, see a classified diff; `loom apply` evolves Iceberg.*

- [x] Diff engine (`migrate/`) — classify changes: safe/additive · physical-safe (Iceberg
      field-id) · breaking.
- [x] `loom plan` — terraform-style dry-run of the classified diff.
- [x] `_loom_meta` state store — serialized applied spec + version + content-hash + history.
- [x] `loom apply` — write port + namespace creation; executes physical DDL in an Iceberg
      transaction; bumps version; idempotent.
- [x] `renamedFrom:` handling — treat as a field-id remap, not drop+add. Property-level and on
      `through` columns; a `rename` change kind classified safe; the old column no longer reported
      as unmanaged.
- [x] `loom rollback` — restore the spec `_loom_meta` recorded and re-plan it against the live
      catalog. DDL only: it reverses schema changes, never data. Renames come back as renames;
      adds are left live and named.

**Five decisions taken in the `rollback` slice:**
- **Rollback is the ordinary loop over an older spec, with exactly one addition.** It emits no
  change kind and no write op that `apply` doesn't, and it executes through `apply_plan` under the
  same whole-plan refusal. The one thing it proposes that a plain apply of the restored spec would
  not is the reverse rename — everything else is the plan the restored spec would have produced
  anyway, because the live catalog is already the baseline.
- **Only renames reverse; everything else is said rather than faked.** An `add` reverses to a drop
  and Loom never drops, so the column stays live and the restored spec no longer maps it: it is
  unmanaged, and the report *names* it rather than leaving it to be discovered. A `promote`
  reverses to a narrowing and a `relax` to a tightening, and both are breaking, so the whole
  rollback is refused. That is not a gap — once the column is a `long`, the spec that says `int`
  no longer describes this lake, and the way out is forward.
- **`renamedFrom` points forward, so the reverse rename comes out of the history.** The restored
  spec cannot name the column it has to be renamed back from; a naive re-plan would add the old
  name beside the full new column and strand it, which is the exact failure `renamedFrom` exists to
  prevent. So `apply` now records its renames as *data* alongside the prose in `summary`, and
  rollback composes them across versions (`a→b` then `b→c` means `a` is `c` now), inverts the
  chain, and overlays it on the restored spec's desired columns. Parsing the display string was the
  alternative and was rejected: a mis-parse renames the wrong column.
- **A version selects a spec, not a per-catalog target.** This is what makes the multi-catalog case
  tractable: a version whose text differs makes every bound catalog stale, so a catalog with no row
  at version 5 is one whose text didn't change at 5 and is already at that spec. Nothing is skipped
  and nothing is rolled back to "its own earliest" — the version names one text, catalogs holding
  it must agree on its hash, and a catalog with no history that far back is named in the report.
- **A rollback is an append that says `applied`.** Not a deleted row and not a new status:
  `status` is what the *next* run's "has this spec already been applied here?" check reads, and
  after a rollback the lake genuinely is at the restored spec. The marker goes in `summary` as
  `rollback_of`, because `_loom_meta` is only ever created and never altered — a new column would
  never reach a history table that already exists.

  One boundary worth stating outright, because the line this replaced ("point physical schema at
  an earlier snapshot") invited the opposite reading: **`apply` only ever ran DDL, so `rollback`
  only ever reverses DDL.** No snapshot rollback, no expiry, no deletes. Rows written since are
  nobody's to throw away. The same logic decides the working tree in the other direction: spec
  files absent from the restored snapshot *are* deleted — named before the prompt — because the old
  spec plus whatever came after is not the spec that was recorded. Files are written last, after
  the DDL, so a refused or declined rollback leaves the tree untouched.

**Four decisions taken in the `renamedFrom` slice:**
- **The spec says what it wants; the live catalog decides what that currently means.** One
  unchanged property plans four ways depending on which columns exist: the rename (old only),
  nothing at all (new only), a warned plain add (neither), and a refusal (both). The second is
  where idempotency comes from, and it needed no new mechanism — the baseline was already the
  live catalog, so a landed rename simply has nothing left to say about itself.
- **Both columns live is a breaking change, not a load error.** The spec isn't wrong; the *lake*
  is in a shape the plan can't resolve, which is what breaking already means here. Making it a
  change rather than a diagnostic keeps the rest of the diff on the page — an error aborts the
  plan and prints nothing — and routes it into the existing whole-plan refusal, which is the
  right outcome, since merging two columns means dropping one and Loom never drops.
- **`renamedFrom` outlives its migration, and Loom never suggests removing it.** One spec is
  deployed to lakes at different versions: after a rename ships to production, staging is still
  on the other side of it, so "you can delete this now" would be true of one catalog and false of
  another from the same file. Chained renames are deliberately not expressible for the same
  reason a state file isn't — a lake several versions behind should apply the versions it missed,
  not be modelled inside the spec.
- **A rename is `safe`, not `physical-safe`.** Physical-safe is the label meaning *the stored type
  moved and only field ids keep the files readable*; a rename moves neither type, nullability nor
  field id. What it does break is readers outside the ontology that select by name, and that goes
  in the plan's reason line rather than inflating a severity that means data safety.

  One implementation note worth recording, because it inverts the obvious: pyiceberg's
  `UpdateSchema` resolves every `path=` against the schema the transaction *opened* with, so a
  promotion following a rename in the same transaction must still name the **old** column. The
  plan therefore orders renames first and `alter_table` documents that ordering as part of its
  contract — it is what lets the adapter translate the later edits back.

**Three decisions taken in the `apply` slice:**
- **A breaking plan is refused whole.** Not "apply the safe parts and report the rest": a
  partial apply leaves the lake in a state that neither the old spec nor the new one describes,
  and `_loom_meta` would then be recording a spec that was never really applied. There is
  deliberately no `--force` — every breaking change either loses data or leaves existing rows
  violating a constraint that was just declared, and the fix is a data migration (add nullable,
  backfill, tighten) that Loom has no verb for until the action runtime lands.
- **Atomicity is per table, and says so.** Iceberg's unit is the table, so each table's column
  edits and its provenance properties commit in one transaction. There is no cross-table
  transaction to be had, so `apply` sequences tables, stops at the first failure, and reports
  exactly which ones landed rather than pretending the run was atomic.
- **Writes are a second port, not extra methods on `Catalog`.** `CatalogWriter` is what `apply`
  asks for; everything else in Loom holds a read-only `Catalog` and therefore cannot execute DDL
  at all. The same reasoning as "no raw-SQL tool is ever exposed", applied one layer down. One
  consequence worth stating: **`version` is global to the spec, not per catalog** — a row is
  written to each catalog the spec binds, but all carry the same number, so "version 7" names the
  same apply wherever you read it.

**Two decisions taken in the `plan` slice:**
- **The live catalog is the baseline, not a state file.** `Catalog.describe()` already returns
  column types and Iceberg field ids, which is everything a diff needs, so `plan` needs no
  `_loom_meta` to work — and diffing against one instead would make `plan` lie the moment
  somebody changed a table out of band. `_loom_meta` records what `apply` *did*; it is a
  history and an idempotency key, not the planner's source of truth. That's why it moved down
  the list into the `apply` slice.
- **Loom never proposes a drop.** An objectType maps a *subset* of a table's columns, so a
  column no property mentions is not a deleted property — it's someone else's data. Those are
  reported as unmanaged and left alone. It also means `plan` is not `validate --physical`: that
  pass treats a missing table as an error, which is exactly what a plan reports as a creation.

**Probed as a client (2026-08-25), and the promotion table was wrong.** M2 had only ever been
driven by tests, and its worked example was run against a real SQLite/filesystem warehouse for the
first time: bootstrap from an empty catalog, widen a column, tighten one, rename one, roll back.
`int -> double` — the example this milestone's guide page used to demonstrate `physical-safe` —
turned out not to be an Iceberg promotion at all. Iceberg promotes `int -> long` and
`float -> double` and nothing else among the types a spec can name, so:

- `loom validate --physical` said **ok** (correctly: the column *reads* as a double),
- `loom plan` said **physical-safe** with "existing data files are not rewritten",
- `loom apply` got `Cannot change column type: score: int -> double` out of pyiceberg, exited 1,
  and appended a `partial` row to `_loom_meta.applied` for a run that committed nothing.

Three answers to one question. The fix splits the two rules that had been one function:
`types.promotable` stays the *read* question physical validation asks, and `types.iceberg_alterable`
is the DDL question the planner asks. Anything outside Iceberg's set is `breaking` now, refused
before the catalog is touched — which is the same answer the milestone already gave every other
change it cannot make safely.

Why the suite could not see it: every planner test builds a `Column` by hand against a fake
catalog, and the apply tests that *do* use pyiceberg only ever exercised `int -> long`. Both halves
agreed with each other because neither half ever asked Iceberg. The regression test is in
`tests/test_apply_iceberg.py` and runs the refused plan against a real catalog.

---

[← M1](./m01-read-slice.md) · [M3 →](./m03-action-runtime.md) · [backlog](./backlog.md)
