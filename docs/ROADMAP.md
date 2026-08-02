# Loom Roadmap

Living plan. The spec module (the compiler front-end) is done; everything below turns the
validated Ontology Model into a running system. Ordered so each milestone is independently
demoable — you can *see* the ontology do something new at the end of each.

Design decisions these build on live in [`spec-v0.md`](./spec-v0.md): one YAML spec compiles to
four surfaces; reads go through an engine-agnostic IR; writes are single-object via the Iceberg
catalog.

---

## ✅ Done — M0: Spec module (compiler front-end)

- [x] Canonical type system (`types.py`) — each type → `{Iceberg, JSON Schema}`, promotion rules
- [x] Expression mini-language (`expr.py`) — tokenizer + Pratt parser → AST
- [x] Typed Ontology Model (`model.py`)
- [x] Structural loader (`loader.py`) — one-kind-per-file, unknown-key errors, shape checks
- [x] Referential/semantic validator (`validator.py`) — accumulates all errors
- [x] `loom validate` CLI · 36 tests · CI

---

## ✅ Done — M1: Read slice, end to end: catalog → query → MCP

*Goal: point an MCP client at a Loom ontology and ask it for a real row from a real Iceberg
table.* The first time the ontology does the thing the README claims.

**Scope note — why MCP moved up.** M1 was originally just the catalog + query slice, with the
MCP server deferred to M4 behind migrations and the action runtime. That ordering ends M1 at a
`loom query` dev command that only a test can see. The read-only half of the MCP surface is
~150 LOC on top of the resolver (registry introspection + two stdio handlers) and it's what
makes the milestone demoable to a person rather than a test suite. So M1 now covers the whole
**read** path top to bottom, and M4 keeps only what genuinely depends on M2/M3 — the `run_<action>`
tools and capability negotiation.

The write path is untouched by this: no migrations, no action runtime, no `_loom_meta`.

- [x] `config.py` — the `loom.yaml` project config from spec §6 (`catalogs`, `engine`, `mcp`),
      reusing the same accumulate-all-errors `Diagnostics` as the spec loader.
- [x] `catalog/` — a `Catalog` port + table introspection (columns → Iceberg types, field ids),
      with pyiceberg-backed implementations. Binds an `objectType.backing` to a live table.
- [x] Wire up **physical validation** — implement `validator.check_physical()` (the stub):
      table/column existence + type promotion-compatibility against the bound catalog.
      Surfaced as `loom validate --physical`.
- [x] `query/ir.py` — the logical plan node set: `GetByKey`, `Search`, `Traverse`, `Project`.
- [x] `query/engine.py` — the `Engine` port (`capabilities()` / `compile()` / `execute()`).
- [x] `query/engines/duckdb.py` — first adapter; lowers IR → DuckDB SQL over Iceberg.
- [x] `resolver.py` — ontology ops → IR; link `Traverse` → JOIN via from/to mapping (+ reverse).
- [x] `mcp/registry.py` — Ontology Model → read tool set (`get_` / `search_` / `list_` per
      object type, generic `traverse`), input schemas from `PropType.json_schema()`.
- [x] `loom serve` over stdio, read-only. Hard-rule test: no raw-SQL tool is ever exposed.
- [x] `examples/` — a seedable local Iceberg warehouse + the worked-example ontology, so the
      whole path is runnable by hand and in CI.

**Definition of done:** a test seeds rows into a local Iceberg table and reads them back through
the resolver + DuckDB adapter, including a one-hop link traversal — and `loom serve` exposes
that same ontology as MCP tools, driven end to end over stdio.

**Two decisions taken here:**
- **Local-first catalog for tests and examples.** pyiceberg's SQLite catalog over a
  filesystem warehouse (`type: iceberg-sql`) sits behind the same `Catalog` port as
  `iceberg-rest`, so CI needs no running services. A REST catalog is a config change, not a
  code change.
- **DuckDB reads Iceberg through pyiceberg, not the DuckDB Iceberg extension.** The adapter
  compiles IR to real DuckDB SQL, and `execute()` binds each referenced table as a named
  relation materialized by a pyiceberg scan — with the scan's own predicate/column pruning
  pushed down. This avoids a runtime extension install and keeps the adapter honest about
  dialect lowering.

---

## ✅ Done — M2: Migration engine (`plan` / `apply` / `rollback`)

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

---

## ✅ Done — M3: Action runtime (single-object writeback)

*Goal: `run_upgradeTier(...)` mutates one row atomically.*

- [x] Parameter binding + validation-rule evaluation (reuse `expr` AST → evaluator).
- [x] Effect compiler → Iceberg **catalog-level** write (equality-delete on PK + append), one txn.
      All three operations: `create` / `modify` / `delete`, behind a third port (`RowWriter`).
- [x] `loom run` — one declared action, through the same entry point `run_<action>` will call.
- [x] Optimistic concurrency — snapshot check carried into the commit; conflict → typed retryable
      error, retried up to `MAX_ATTEMPTS` first.
- [x] Edit-log (audit) table — actor, action, before/after, snapshot id. `_loom_meta.edits`, behind
      a fourth port (`EditLogWriter`) that can name no table; refusals recorded, one row per run.
      *`conflict`'s `detail` carried expected/found/changed into it unchanged, as predicted.*
- [x] Tests: create / modify / delete happy paths, against the fake catalog and against live
      pyiceberg — plus a real competing commit landing between a run's read and its write, on both,
      and a record written beside a real row with the commit stamp that ties the two together.

**Eight decisions taken in the first slice** (parameter binding, rule evaluation, one row written):

- **Rows go through a *third* port, and none of the three is a superset.** M2's argument — the
  resolver holds a `Catalog` and therefore cannot execute DDL — points both ways one level down.
  `CatalogWriter` changes a table's shape and has no verb for deleting a row, so `loom apply`
  cannot touch data. `RowWriter` changes its rows and has no verb for altering a schema, so an
  action cannot touch DDL. `append_rows` stays on the schema port because it is how `_loom_meta`
  records history: purely additive, incapable of destroying anything. `writer_for` grows a
  *sibling* rather than an argument — `row_writer_for` — both over one exchange point whose error
  names the catalog and the plane it refused. The handle is acquired per run, for the one catalog
  the target object binds, so nothing holds a row-writable *typed* reference between calls.
  *(M4's first slice narrowed that last clause — it used to say no serving process holds a
  row-writable catalog, which stopped being the load-bearing claim once one could write. See below.)*

- **The read before the write is a full physical row, because of the columns nobody declared.**
  A modify is an equality-delete plus an append, so it rewrites the row entirely: every column no
  property maps is carried across or silently nulled. Those are exactly the columns `plan` reports
  as unmanaged — the never-drop rule one level down, where the data is rather than the schema. It
  is why the runtime reads through the `Catalog` port and not the resolver, which projects a row
  down to precisely the set a modify must not be limited to. A column whose *type* the ontology has
  no name for is carried the same way, untouched and unexamined: the conversion is driven by the
  table's own schema, so `array<T>` being deferred in §1 costs the write path nothing. `before`
  and `after` still report only declared properties — the unmapped columns travel, but reporting
  them would leak someone else's data past a governance layer that doesn't exist yet.

- **The snapshot is captured now and checked later, and the difference is said out loud.** Every
  read-then-write records the Iceberg snapshot it read (`Catalog.current_snapshot_id`, a *read*
  verb), and nothing enforces it. The result carries `concurrency: "recorded, not enforced"`, and
  `CONFLICT` exists as the one retryable failure code with nothing raising it — so the next slice
  is a check and one `Failure`, not a new result shape every caller would have to relearn. The
  snapshot is read *before* the rows, which is the load-bearing half: that order makes the recorded
  id at-or-before the data, so a later check can report a conflict that wasn't one but can never
  miss one that was. Scan-then-snapshot silently blesses a lost update. No unused
  `expect_snapshot_id=` was added to the port — a parameter nothing passes is not a seam.
  *(The second slice made this true and revised the last sentence — see below.)*

- **`{{ customer }}` and `newTier != object.tier` are one language, and always were.**
  `expr.parse()` already stripped a whole-string `{{ … }}` wrapper; that is now the stated rule
  rather than an implementation detail, so no evaluator, validator or engine ever sees a brace. Two
  consequences: an effect value may hold **any** expression, not just a parameter reference (which
  is what makes `placedAt: "now()"` work, and narrowing it would have made the most obvious thing a
  create wants inexpressible); and there is **no interpolation** — `"tier-{{ x }}"` is a load error
  with a message that says to use `+`. Two spec bugs fell out of settling this: §4 and §8 wrote the
  rule as `customer.tier`, which the validator has always rejected, and §5 claimed bare enum values
  were literals, which would have made `gold` a literal in one position and a parameter in another.

- **Null is a value you can test, not one you can order.** `null != 'gold'` is true; `null == null`
  is true. Deliberately not SQL's three-valued logic: this evaluates in process over one row and
  never reaches SQL, and an "unknown" precondition would leave the runtime nothing to do but refuse
  — making `null` a hazard in every rule about a nullable property, when a precondition is supposed
  to be a decision. Ordering, arithmetic and the boolean operators still fail on null rather than
  guessing, and `&&`/`||` short-circuit, which is what makes that livable:
  `object.ltv != null && object.ltv > 100`. The value domain is the read path's own — one
  `coerce_value`, shared, so "declared types are honored on the way in and out" is structural
  rather than duplicated, and a decimal is still a decimal after a comparison.

- **A failed rule is a typed result, not an exception — and every rule is evaluated.** M4 wants
  "typed results an agent can act on", so the shape exists before it is wrapped: a status, and a
  list of `Failure`s carrying a code from a closed set. Nothing an author, a caller or the data can
  cause raises. All rules run rather than stopping at the first, the same bargain `Diagnostics`
  makes with a spec author — an agent fixing one precondition per call is as miserable as a human
  fixing one typo per run. And a rule that could not be *evaluated* is its own code, distinct from
  one that returned false, because retrying them means different things.

- **All three operations, because two of them already validate.** `modify` exercises the whole path
  and shipping it alone was tempting — but the loader accepts all three effect kinds, the validator
  type-checks all three, and `loom validate` says *ok*, so a modify-only slice would ship specs that
  validate and cannot run. That is a worse seam than the two extra port verbs. On `delete` versus
  never-drop: never-drop is about **inference**, Loom refusing to read a destruction into the
  *silence* of a spec. `operation: delete` is the opposite of silence, and the scopes differ —
  never-drop governs schema, and Loom still never drops a column or a table in any command.

- **The uniqueness of the key is checked before the write.** The PK is single-property in v0 and
  Loom doesn't own the table, so nothing guarantees it is unique, and an equality-delete on a key
  matching two rows removes both and appends one. The *read* path already refuses this
  (`Resolver.get`); refusing it on the write path matters more, and costs nothing because the read
  is already happening by key. Loom cannot repair the table — it declines to make it worse, and
  says so.

  `loom run` exists for the same reason `loom query` does, and the test is stronger because this
  one writes: if the dev command can do something the generated tools can't, the ontology has a
  back door. It takes an action apiName and named parameters — the shape `run_<action>` will take —
  and calls the same `ActionRuntime.run`, asserted rather than assumed.

**Six decisions taken in the second slice** (optimistic concurrency — the check the first slice
left open, and said it was leaving open):

- **The check is carried into the commit, not performed before it — so "enforced" is the honest
  word.** The port grows `expect_snapshot_id` and the pyiceberg implementation lowers it into an
  `assert-ref-snapshot-id` requirement on the transaction, staged *before* the write op so it
  replaces the one the snapshot producer stages for itself. The catalog validates requirements
  against metadata it re-reads and swaps the metadata pointer conditionally on what it validated
  against; a commit that lands in between loses. The alternative — the runtime re-reading, comparing,
  then writing — has a window between the comparison and the commit, so it *narrows* the race rather
  than closing it, and the docs would have had to say narrowing the way the first slice said
  recorded-not-enforced. They don't, because it doesn't.

  Two revisions to what the first slice predicted. The argument is **required, not optional**: one
  that can be omitted is a check that can be skipped by forgetting, and there is no value meaning
  "don't check" — `None` is the real expectation "I read a table with no snapshots". And it is on
  **all three** verbs, not two. The guarantee rests on a library's deduplication rule, so the
  implementation re-checks that its own requirement survived onto the transaction and refuses loudly
  if it didn't: a silent downgrade from a closed race to a narrower one is the worst outcome
  available, because everything above would go on claiming enforced.

- **"The row moved" means the table moved, and somebody else's write to a column Loom never mapped
  counts.** Iceberg's commit protocol can assert a ref's snapshot and nothing finer, so the only
  narrower test is comparing the row — and a row comparison cannot be carried into a commit. Choosing
  it would trade the guarantee for the precision. Coarse-and-closed beats narrow-and-open, and the
  false conflicts that follow (the snapshot is read *before* the rows, deliberately) are absorbed by
  the retry rather than by loosening the order.

  This settles the unmapped-column question **without qualifying the never-inspect rule** — it is the
  same posture stated twice. A `modify` writes those columns back from a read taken before the
  competing commit, so committing anyway would restore a stale value over somebody else's newer one.
  Loom refuses to look at the column *and* refuses to overwrite it blind; the snapshot check is how
  it manages the second without doing the first, since it compares no columns at all.

- **A conflict is retried here, bounded at three, and the count is on the result.** Returning the
  first conflict is what the roadmap line said and it is the weaker choice: a table-level check
  refuses on any concurrent commit, so something has to absorb them, and pushing that onto every
  caller means every caller writing the same retry loop — including the ones that are language
  models. Each attempt re-reads and re-evaluates every rule and effect expression; nothing is
  replayed, or a `now()` would freeze at a read that lost. The bound is about liveness, not
  correctness. The real objection — a retry can succeed against a row the caller never saw — is
  answered by what `validation` rules are *for*: they state which states the caller will act on, and
  they are re-checked against the newer row, which is stricter than the caller's own stale read.
  Where the competing write genuinely invalidates the action, the retry returns `validation_failed`
  or `object_not_found`, the real reason. So the retry turns most races into nothing and the rest
  into a decision.

- **All three operations are checked, and the reasons differ.** `modify` for the carry-across above.
  `create` because its read is the PK existence check and two concurrent creates both pass it, then
  both append — manufacturing exactly the duplicate `ambiguous_key` refuses ever after and Loom can
  never repair; checked, both read the same snapshot and only one can commit against it, which is
  what finally makes the existence check mean something for writers coming through Loom. `delete`
  against its own counter-argument: "the row is gone either way" holds only if the competing write
  was also a delete, and if it was a `modify` the row is not gone, it changed — in the one operation
  nothing can undo. When the competing write really was a delete, the retry finds nothing and returns
  `object_not_found`, which is that outcome said accurately rather than a delete claiming work it
  didn't do.

- **The prompt is outside the window: the run re-reads, and approval is about the shape.** Checking
  the *preview's* snapshot would put a person's thinking time inside a transaction, and it fails the
  test that decides this — `run_<action>` has no prompt, so a design keyed to a preview is one the
  MCP caller can never join. It also contradicts a decision already taken: `loom run` re-runs all
  four steps because a preview is not a recording. The CLI says so above the `y/N` instead of
  printing a snapshot id that would read as a hold, and reports afterwards if the table moved while
  someone was deciding. `ActionResult.concurrency` is status-dependent for the same reason: a preview
  writes nothing, so claiming "enforced" beside its snapshot id would be the exact misreading.

- **The seam for testing a race is the port, not a hook.** A hook nothing in production calls drifts
  out of step with the path that matters, and a conflict path that only fires under load is one
  nobody knows works. Because reads and writes go through a narrow port, a test wraps a catalog in an
  adversary whose `scan` commits a competing write before returning — the interleaving driven by the
  runtime's own call sequence, so it is as deterministic as any other assertion. Against real
  pyiceberg the adversary commits through a **second, independently opened catalog handle**: a
  genuine concurrent writer producing a real commit that really advances `main`. Nothing in the
  runtime knows a test exists. That is the ports decision paying out; a hook would only have been
  needed if the runtime talked to pyiceberg directly.

  `conflict`'s `detail` is settled here rather than later because the edit log wants the same shape:
  expected, found, attempts, which declared properties moved (diffed through the same projection
  `before`/`after` use, so unmapped columns are compared no more than they are reported), and whether
  any of them is one this action reads or writes. An agent told only "conflict, retry" will hammer a
  table that is merely busy and give up just as readily when its intent has really been overtaken.

**Eight decisions taken in the third slice** (the edit log — the last box, and the one that closes
M3):

- **A fourth port, and the count is the honest thing to change.** `insert_row` was the obvious home
  and it is the wrong one: it *requires* `expect_snapshot_id`, and the log append follows no read and
  puts no row over another, so there is no honest value to pass — before considering that it would
  subject every action to a check against the hottest table in the system. A `CatalogWriter` held
  beside the row writer reopens "an action cannot touch DDL, because the port has no verb for it" to
  buy one append. And `append_rows` takes a table name and a batch, which is exactly the pair an
  action must not hold. So `EditLogWriter`, one verb, **no table argument** — there is nothing to
  point at the wrong table with. What it costs is the sentence in `catalog/base.py`: three ports and
  two planes became four and three, the third plane being Loom's own record. The fake proves it —
  a catalog implementing `RowWriter` and `EditLogWriter` and *not* `CatalogWriter` logs successfully,
  which is an assertion no real catalog can make, because a real one implements every port at once.

- **The first append creates the table; `apply` never learns it exists.** The alternatives were
  `apply` creating it up front, or a run refusing without it — both give the log a precondition the
  write does not have. Loom writes to lakes it never migrated (the whole posture `ambiguous_key`
  exists for), and an audit trail that switches itself off in exactly those deployments is worse than
  a create verb that can only reach `_loom_meta`. The quickstart seeds and runs without applying, and
  still logs. Per catalog, like `applied`: an action only ever writes one catalog, and *what did this
  actor do today* is a cross-table question a per-table sidecar cannot answer.

- **A refusal is recorded, and the invariant is restated rather than weakened.** "A run that refuses
  changes nothing" becomes "changes nothing **it was asked to change**" — no row, no column, no
  table — and it is changed in all four places it appears rather than quietly reinterpreted in one. A
  log of successes cannot answer *who tried to delete this customer*, and a conflict is a refusal, so
  a contended row would otherwise leave no trace of the attempts it swallowed. Still true of
  `loom apply`, which refuses before it holds a writer and records nothing: a stronger instance, not
  an exception. The boundary is the key — a call that could not be bound never addressed a row, and a
  record with no key answers no audit question (and every append is a commit, so an agent looping on
  a malformed call would otherwise write one per attempt into the table meant to hold edits).

- **Declared properties, because the physical row is a worse leak than the one the rule prevents.**
  The never-report rule is extended to a new reader rather than excepted for one. An unabridged copy
  of the data, in a table nothing governs, retained forever — and it is the copy that *outlives* the
  row, so the shipped `forgetCustomer` action would erase a customer into a permanent record of them.
  The incompleteness objection has a real answer rather than a shrug: the unmapped columns were
  carried across unchanged and the commit asserted the snapshot the read saw, so **what the record
  does not name, the run did not change** — the silence is the record of a guarantee. The bound
  parameters are recorded too, because a refused modify has no `after` and would otherwise say that
  somebody tried without saying what. What is *not* fixed — declared properties are still somebody's
  data and still outlive a delete — is spec-v0's open edges rather than a thing that got past.

- **The runtime never invents an actor.** `default_actor()` is honest for the commands a person runs
  and a lie for `run_<action>`, where it names whoever started `loom serve` and stamps every caller
  with one string. So `run` takes an `actor` argument, per call rather than per runtime because a
  serving process is long-lived and a caller is not; `loom run` passes `default_actor()` in at the
  one call site where it is true; nobody supplying one records `unknown`, which beats a confident
  wrong answer. M5 unpicks nothing — the argument is already there and does not move.

- **The record that has to be atomic is carried inside the commit.** Iceberg has no cross-table
  transaction, so the row write and the log append are two commits and both orderings lose something:
  log-then-write records intentions that may not have happened, write-then-log loses the records of
  writes that succeeded. Neither is acceptable on its own, because a gap indistinguishable from
  silence is not evidence. Iceberg has exactly one slot that *is* atomic with a row write — the
  snapshot summary — so `RowWriter`'s three verbs grew a required `commit_properties`, and the write
  stamps `loom.edit_id` into its own commit. This is the previous slice's move applied to a record
  instead of a check, and `table_properties()`'s move one plane down. Given that, write-then-log
  follows: a lost record is a stamped snapshot the log does not hold, which a reader can find, and
  `failed` becomes answerable for the first time. The guarantee is asymmetric and says so — a lost
  record of a *refusal* is undetectable, because a refusal leaves nothing to stamp. A failed append
  never fails the action, which has committed; it is a non-retryable `log_failed` beside the real
  status, which is where this diverges from `apply` (whose result lists what landed, so `failed`
  there is unambiguous). A catalog with no edit-log port gets the same treatment: "no log, no write"
  is a policy, and policies are M5.

- **A retried run is one row.** The attempts that lost wrote nothing, so they are not edits; they are
  one edit that took three tries, and `attempts` says so. The states they lost to are not this run's
  to describe either — a competing writer coming through Loom has its own record in the same table,
  and one that did not could never be described honestly. Three rows would mean most of the log
  described things that did not happen.

- **Rollback does not touch it, and `plan` cannot see it — but not for the reason it looked like.**
  The premise that `loom.managed` is the marker plan and rollback key off turned out to be false:
  that property is written by `apply` and read by *nothing*. What keeps both off `_loom_meta` is that
  `diff_ontology` only ever visits `desired_tables(ontology)` — the tables the spec declares — which
  is why `applied` was never proposed either. Pinned with a test now that an action can conjure a
  table no spec has heard of. Rollback leaves the log alone for the ordinary reason: it reverses DDL
  and only DDL, and the writer it holds has no verb that removes a row from anything.

---

## ✅ Done — M4: MCP write surface + transport hardening

*Goal: the action runtime shows up as tools; serve over more than stdio.*

The read tools, the registry, and stdio `loom serve` landed in M1; M2 and M3 are done, so what was
left here was a transport and the surface over a runtime that already exists. Two seams M3 left
pointing at this milestone: `ActionResult` is the shape `run_<action>` serializes rather than
composes, and `ActionRuntime.run` takes the `actor` this transport has to supply — an unauthenticated
one recording `unknown` is correct, and a served tool that never fills it in makes the edit log
useless without failing anything, which is worth a test here rather than a hope.

Both have landed. The surface came first (`run_<action>`), then the second transport — which turned
out to be about neither the tools nor the runtime, both unchanged, but about everything that stops
being true when a process stops belonging to the client that spawned it. The third slice is the one
box neither of them touched: whether the engine underneath can serve the surface at all.

- [x] Per action: `run_<action>` with JSON Schema from parameters, description from the spec.
- [x] Capability negotiation — validate what a spec demands against `engine.capabilities()`, at the
      point where the two are wired rather than at serve. *`negotiate.py`: three requirements, one
      of which is not a spec feature; a refusal rather than a narrowing; and the line around
      `native_merge` drawn where it belongs.*
- [x] HTTP transport alongside stdio. *`mcp.transport: http`, with an address in `loom.yaml` and a
      write surface bounded by the bind. The tool set, the registry and the runtime are unchanged;
      what changed is everything that stops being true when a process stops belonging to one caller.*
- [x] Structured tool errors — surface validation-rule failures and write conflicts as typed
      results an agent can act on, not opaque strings. *Both seams paid out as predicted: the tool
      serializes `ActionResult` rather than composing anything, and the actor was already an
      argument.*

The header was held by capability negotiation alone, and the box was kept out of the transport
slice on purpose: it has no transport content at all — the answer is identical over stdio and HTTP
— so bundling it would have meant a change about a port deciding whether an existing spec still
serves. It also carried an unresolved question of its own that deserved answering rather than
smuggling: `Capabilities` carries `joins`, `offset`, `case_insensitive_like` and `native_merge`,
nothing validated a spec against any of them, and `native_merge` is a *write-path* field sitting on
what looked like the read path's port. The third slice below is that question and its answer.

**Seven decisions taken in the first slice** (`run_<action>` — the surface over the runtime):

- **One tool per action, and the rule that says so is narrower than `traverse`'s was.** M1 justified
  a single generic `traverse` with "the link name is data, and enumerating object-type × link would
  grow the surface for no gain" — which is true of an action name too, so as written it decides this
  case wrongly. The rule it was reaching for is about the schema, not the name: *a generic tool is
  right exactly when the varying element does not change the input schema.* Every link takes the same
  `(objectType, key, link, page)`; every action takes something different. One `run(action, params)`
  has to type `params` as a free-form object, the only place in the generated surface where an agent
  gets an untyped bag and "declared types are honored on the way in" stops being structural. The
  sentence is rewritten where it is stated rather than qualified from a distance. The cost is real
  and stated: forty actions generate forty tools, and the answer to that is exposing fewer, never
  typing them less.

- **Two argument namespaces, which never mix — and `search_` was already built this way.** Names from
  the spec's vocabulary go inside a nested object (`filter`'s property filters, `parameters`'
  declared parameters); names Loom chose stay at the top (`key`, `limit`, `offset`, `objectType`,
  `link`, `dryRun`). Stating the rule rather than repeating the shape is what makes `dryRun` addable
  at all: an ontology may declare a parameter called `dryRun` and it can no more be shadowed than a
  property called `limit` can. The alternative — flat parameters with a reserved word — makes a spec
  that validates and cannot be served, which is the worst seam available.

- **An agent can preview, and a preview approves nothing.** `dryRun` runs bind → read → validate and
  stops, returning `previewed` — otherwise that status is one no MCP caller could observe and an
  agent's only way to learn what an action does is to do it. It is reconciled with §4.1 rather than
  bolted beside it: the prompt was put outside the concurrency window *because* `run_<action>` has no
  prompt, so a preview that reserved anything for a later call would be the design that decision
  rejected. Nothing is carried between a preview and a run; the run reads again and asserts that
  read. Approval of an agent's tool call belongs to the client's own UI, where the human is. A
  separate `preview_<action>` was the alternative and doubles a surface this slice just argued
  against doubling — and the two tools would have carried identical schemas, which is precisely when
  the first decision says one parameterized tool is right.

- **`isError` answers "did this call become a run?", never "did the run succeed?"** M1 already sent a
  `ResolverError` back as content because that is the form an agent can recover from; `ActionError`
  joins it. But a run that *reached* the runtime is never an error here whatever it returned. A
  refusal is the expected outcome of a precondition doing its job; the outcome is four-way and one
  code is retryable, neither of which a boolean carries; and **`applied` with a `log_failed` beside
  it is a real shape the boolean gets backwards** — `isError` would say the write did not happen when
  it did. So an agent branches on `status`, then `failures[].code`, then `retryable`, and the
  generated description says so because the input schema cannot.

- **What a serving process holds, said precisely enough to still be worth something.** M3 wrote "no
  serving process holds a row-writable handle between calls", which was true of a command that exits
  and is nearly vacuous for a long-lived one: the process holds `Catalog`s, and a real catalog
  implements every port, so it is one function call from being a row writer whatever the runtime
  does. The surviving narrow version is that nothing holds a row-writable *typed* reference, so
  `row_writer_for()` stays the one place the plane is named at a call site — and it is no longer the
  load-bearing claim. What replaces it is testable the way M3's port claims are: the runtime holds a
  `RowWriter` and an `EditLogWriter` and never a `CatalogWriter`, so **a serving process can change
  the rows the spec's actions declare and no schema at all.** The fake proves it, because a real
  catalog implements every port and can never show which one was used. The sentence is corrected in
  all three places it appears.

- **The actor is declared, never inferred — `mcp.actor`, and `unknown` when it is unset.** M3 kept
  `default_actor()` off this path because it falls back to the OS user, so it would name whoever
  started `loom serve` while looking like a principal. An operator writing `actor: agent:support-bot`
  is not doing that: it is a true statement about a deployment, and over stdio it is exactly true,
  because one client spawns one process and a session has one principal. Declared-versus-inferred is
  the distinction, not process-versus-caller. A client-supplied actor was the third option and is
  worse than `unknown` — an audit record whose subject fills in its own name is self-attestation, and
  MCP has no identity to attest with. What the edit log is worth over stdio is therefore written down
  rather than discovered: it answers what was done, to which row, when, with which parameters and
  whether it refused, and it does not answer *who*. That is a gap in the transport, and it closes per
  call when an authenticated one lands.

- **`status` is read at last: labelled, not hidden.** A non-`active` objectType, link or action still
  becomes a tool, with `DEPRECATED — ` / `EXPERIMENTAL — ` in front of the spec's own description.
  Hiding a deprecated action would leave `loom run` able to run something the tool surface denies —
  the exact back door `loom run` exists to not be — and hiding it *honestly* would mean making the
  runtime refuse it, turning a surface label into a kill switch and making `deprecated` mean broken.
  A label is also the form that works on this caller: an agent reads descriptions afresh every
  session and has no memory of a deprecation notice, so the notice has to be in what it reads.

  **And serving writes is a choice, off by default** (`mcp.writes`, §6). `loom serve` was provably
  read-only and deployments were pointed at real lakes on that basis; defaulting it on would let an
  upgrade plus an unrelated spec edit silently make a production lake mutable. It is a config key
  rather than a CLI flag, because a flag lets an invocation contradict the file an operator reviews.
  It belongs to *this* slice rather than the capability-negotiation box below: that box is about what
  the engine can do, and this is about what the deployment permits. It is deliberately not a
  governance policy either — it names no principal and filters no row — though M5 may subsume it.

  One thing this slice could not test the way it wanted to: a **conflict produced by a real race over
  the wire**. It needs a competing commit inside the window between the served read and the served
  write. The reason given here was "nothing a client can schedule over the protocol reaches inside a
  spawned process" — *and the transport slice below found that reason wrong, so it is corrected
  here rather than left standing.* The MCP SDK dispatches tool calls concurrently; HTTP
  demonstrably can carry an interleave, and stdio was never what prevented one. What prevents one is
  that Loom's own dispatch is synchronous top to bottom, so a served process answers one call at a
  time whatever the transport — and a commit from *outside* the process would still have to land
  inside a millisecond window, three attempts running, which nothing outside can schedule without
  the hook M3 declined to add (a hook nothing in production calls is a hook that drifts). The
  conclusion is unchanged and the argument for it is now the true one. The conflict's wire form
  stays asserted against `LoomMCPServer.call`, which is the exact function both adapters call and
  whose `(text, is_error)` pair is what goes on the wire.

**Six decisions taken in the second slice** (the HTTP transport — the same tool set, reachable
by anyone who can reach the port):

- **A served process answers one tool call at a time, and it is proved rather than assumed.** This
  was measured before it was decided: the MCP SDK dispatches `on_call_tool` concurrently, and two
  clients on one HTTP server genuinely interleave. So the serialization is entirely Loom's, and it
  comes from one rung down — dispatch is a plain function, every `ToolSpec.handler` is a plain
  function, and a synchronous callable cannot be interleaved. That premise is asserted structurally
  in `test_mcp_registry.py`, so making any handler `async` fails a test instead of quietly changing
  what the process guarantees.

  It stays serialized because the fix is not a transport's to make, and three pieces of shared state
  say why. `DuckDBEngine` holds **one** connection and registers every scan under `t0` / `t1` /
  `m0` — constants in `resolver.py`, so the *same three names* for every object type in every
  ontology; two overlapping reads would not merely contend, the loser would answer with the winner's
  rows. `build_server` builds one `Resolver` and one `ActionRuntime` for the process. And making
  those per-caller is the same change M5 needs to filter by principal — an argument for doing it
  once, there, rather than half of it here. The cost is real and is *said*, in the banner and the
  README, rather than discovered: a slow query blocks the server instead of queueing beside another
  call. An HTTP server that answers one request at a time is a scaling claim, and one that does it
  silently is a support ticket.

  A lock was the obvious alternative and is worse: over synchronous handlers it can never be
  contended, so it is code with no behaviour whose only effect would be to keep the guarantee alive
  the day somebody makes a handler `async` — turning a correctness question into an unexplained
  performance one. The assertion fails loudly instead.

- **`mcp.actor`'s justification was already weaker than it read, and gets corrected rather than
  extended.** The first slice defended it with "over stdio it is exactly true, because one client
  spawns one process and a session has one principal". But this key lives in `loom.yaml`, which
  configures a *deployment*: three stdio clients reading one file already record one string for
  three callers. One name for many callers is not what a socket introduces, and declared-versus-
  inferred — the part that was load-bearing — survives untouched. What a socket changes is
  **reachability**: who is permitted to *be* one of those callers.

- **So the limit is drawn on the bind, not the transport — and `writes: true` on a non-loopback bind
  refuses to start.** Over stdio the caller set is "whoever can run the binary and read the config";
  over loopback HTTP it is very nearly the same set; over `0.0.0.0` it is not remotely the same set,
  and there `actor:` names a deployment nobody bounded. A refusal rather than a warning, because
  `cmd_serve` already refuses to start rather than advertise tools that will fail, and because
  nobody reads the third line of a banner on a server that came up. It is honest about its own
  limit: it constrains what Loom *binds*, not what *reaches* it, and a proxy in front of a loopback
  bind is outside anything the config can see.

  The third way out — an identity **attested** by a transport that checked it, which is neither
  declared nor inferred and the only one of the three worth more than `unknown` — is named and not
  built. MCP's authorization is an OAuth 2.1 resource-server profile, so attesting means validating
  a bearer token on issuer, audience, expiry and signature; reading a header instead is the
  client-supplied actor the first slice rejected by name, wearing a hat. That is a milestone, not a
  slice, so spec-v0's open edge is **rewritten** rather than closed: it now names the three
  categories, says what is missing (the validation, and config for an authorization server), and
  records that a loopback server may write today because its callers are the set stdio's were.

- **The address is all config, including the port, and defaults to loopback.** The first slice's
  argument — a flag lets one invocation contradict the file an operator reviews — is weakest for a
  port number, which is not a posture. It goes in config anyway: a file describing half an address
  does not describe the server. The host is the strongest case rather than the weakest, and
  `127.0.0.1` is the default for the reason `_confirmed()` refuses without a terminal — don't put
  somebody's lake on a network because nobody said to. There is no TLS key; termination belongs in
  front, which is a second reason the default bind is local. `allowed_hosts` backs DNS-rebinding
  protection and is required exactly where it cannot be derived: a loopback bind knows its three
  names, a public one does not know the name the world reaches it by.

- **The stderr rule stays and its reason is replaced.** "stdout is the transport" is false the
  moment a transport has an address instead of a pipe. The banner stays on stderr because it is
  *diagnostics*, and one output shape is worth more than one that is right for two transports and
  open again for the third — whatever collects those lines should not need to know how the tools are
  being served. uvicorn's access log, the one thing that would have written to stdout, is off.

- **An HTTP status never disagrees with `isError`.** A transport with real status codes invites
  re-litigating a decision the first slice took, and it does not get to: the status answers *did
  this exchange happen* and `isError` answers *did this call become a run* — different layers, never
  two votes on one thing. Every tool outcome is a `200` carrying content; a non-`200` is only ever a
  rejected `Host`, a rejected `Origin`, a malformed body or an unknown session. Mapping a refusal
  onto a 4xx would make an agent's transport raise before its own branch on `status` ever ran.
  Asserted with a raw HTTP client, because an SDK client hides the number.

  Two things this slice deliberately did **not** move. The principal stops exactly where it did —
  `mcp.actor` reaches the edit log and nothing else, and the resolver is handed no identity, because
  inventing a per-call principal with no source and no reader is the mistake `expect_snapshot_id`
  was kept out of `RowWriter` to avoid. And the surface does not branch on transport: both adapters
  are handed one assembled server from `build_mcp_server`, which is asserted with no socket in
  sight, and `test_no_tool_can_take_a_query`'s walk is re-run over the schemas as received across
  the wire.

**Five decisions taken in the third slice** (capability negotiation — the one box the milestone was
waiting on, and the question it was carrying):

- **Three requirements, and one of them is not a spec feature — so the box's own wording is
  corrected rather than satisfied.** It read "validate spec features vs. `engine.capabilities()`",
  and two of the three are exactly that: `joins` is demanded by declaring a link, because a traverse
  joins two backing tables; `case_insensitive_like` by declaring a *string* property searchable,
  because `Resolver._filters` emits a `Contains` for that condition and an `Eq` for everything else
  — so a searchable **enum** demands nothing, and `Customer.tier` is the case that shows the rule is
  about the property's type rather than the `searchable:` list. `offset` is not a spec feature at
  all: every generated `search_` / `list_` / `traverse` tool carries the page arguments for every
  ontology there is, because they are Loom's own vocabulary and not the spec's (§7's two argument
  namespaces, seen from the third side). It is a constant requirement of the **surface**. It is
  checked anyway, because the question a deployment is asking is not "does my spec use features this
  engine has" but "can this engine serve the tools I am about to advertise" — and the answer has to
  cover the parts of that surface no spec chose. Strip every link and every searchable property from
  an ontology and `offset` is still required; there is a test that does exactly that.

- **A refusal, never a narrowing — and the third degradation is the one that decides it.** Loom
  already refuses rather than degrades in three places argued separately (`cmd_serve` would rather
  not start than advertise tools that fail on every call; `loom apply` refuses a breaking plan whole
  with no `--force`; `mcp.writes: true` refuses a non-loopback bind), and what makes them agree is
  visible from here. The narrowings available are dropping `traverse`, stripping `offset` out of the
  page schema, and compiling `Contains` down to `Eq`. The first two make the generated surface a
  function of the **engine**, which spends the one claim — the surface is a function of the spec and
  nothing else — that the transport slice just proved survives a second transport, and spends it on
  a config mismatch. The third is worse than both and worse than failing: an exact match where the
  spec promised substring **returns rows**, so nothing errors, nothing retries, and the agent
  believes an answer that is wrong. A capability mismatch is also the worse *shape* of failure to
  leave running — an engine without `OFFSET` serves page 1 of everything and fails page 2, so it
  works until it doesn't, and by then a client is holding the tool list.

- **`native_merge` is a routing hint, and the line gets drawn around negotiation rather than around
  the port.** A **requirement** is something a spec can demand and an engine can fail. Nothing can
  demand `native_merge`: writes go through the catalog's `RowWriter`, which every catalog
  implements, so an engine that cannot `MERGE` is a slower way to serve an ontology and never a
  reason to refuse one — it selects an implementation, not a possibility. Which means the complaint
  ("a write-path field on the read path's port") had the wrong premise: `Capabilities` was never the
  read path's structure. It describes an **engine**, and this is where an engine is asked what it
  is; that the engine only reads today does not make the question a read-path question. What was
  actually wrong was `Engine`'s docstring, which called `capabilities()` "what the serve-time
  negotiation reads" as though that were all of it, and it is fixed where it was written. The
  distinction is then made checkable rather than conventional: `NEGOTIATED | NOT_NEGOTIATED` must
  cover the dataclass exactly, under a test, so a fourth flag fails until somebody says which kind
  of fact it is. Without that, the quiet answer available is "a third kind: unread" — which is how
  `loom.managed` got written by `apply` and read by nothing for two milestones.

- **It happens where a spec and an engine are wired, not at serve.** "At serve" is where a mismatch
  is *observed*, not where it belongs. `build_resolver` is the one function that pairs the two, so
  checking there means `loom query` refuses exactly what `loom serve` refuses; checking in
  `cmd_serve` would leave a dev command reading successfully out of an engine the served surface
  will not stand on, which is the shape of back door `loom query` was deliberately built not to be.
  It is the same principle M5 states for governance — enforce below MCP so a direct call and an
  agent call get the same answer — arriving one milestone early because this is the first check that
  had a choice about which rung to sit on. It is deliberately *not* an invariant of `Resolver`,
  which stays constructible from any engine: the pairing is what has to be checked, not the pair,
  and that is what lets a test drive the resolver with a fake and an adapter be exercised before
  anybody has decided what it will serve.

- **The write path is not negotiated, and that is not an omission.** `ActionRuntime` reads a whole
  row and writes it back through the catalog's ports; it never compiles a plan, so it asks the
  engine for nothing and there is nothing to check. An engine that fails negotiation still runs
  actions — `loom run` is unaffected. `loom serve` refuses anyway, because it builds both halves and
  one of them cannot stand, which is the honest answer for a surface that is advertised as a set.

---

## ⏳ M5 — Governance

*Goal: row/column policies enforced identically for API and MCP callers.*

- [ ] Design the `governance.policies` grammar (deliberately deferred in v0).
- [ ] Enforce in the **resolver** (below MCP) so direct + agent calls filter the same way.
- [ ] Column masking + row predicates; policy tests over both paths.
- [ ] What the **edit log** holds under a policy. M3 deliberately left three questions here rather
      than answering them where nobody could turn the answer off: whether `_loom_meta.edits` masks
      under the same policies as a read (it records declared properties and the bound parameters —
      less than the physical row, still somebody's data, in a table that outlives the row it
      describes); whether a retention window expires it; and whether "no log, no write" is
      expressible, since today an unloggable run still happens and reports `log_failed`. The record's
      *shape* is not deferred — the columns are fixed, because the table is only ever created.

---

## Backlog — spec edges (from spec-v0 §"Open edges")

Consciously deferred in v0; each is a self-contained follow-up:

- [ ] Composite (multi-property) primary keys — ripples into `key` exprs + objectRef encoding
- [ ] Complex property types — `array` / `struct` / `map`
- [ ] Computed / derived properties — backed by an expression instead of a column
- [ ] **Multi-object actions** — the post-v1 feature the single-object boundary reserves room for
- [ ] More engine adapters — Trino, Spark (+ route writes through native `MERGE` when
      `capabilities().native_merge`)

---

## Cross-cutting / infra

- [ ] `pyproject` extras for engine backends (`[duckdb]`, `[trino]`) and catalog clients
- [ ] Example end-to-end project under `examples/` (seedable local Iceberg + a demo agent loop)
- [ ] Docs site / expanded README now that M1 has landed
- [ ] Type-check (mypy) + lint (ruff) in CI alongside pytest
