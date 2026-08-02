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

## ⏳ M3 — Action runtime (single-object writeback)

*Goal: `run_upgradeTier(...)` mutates one row atomically.*

- [x] Parameter binding + validation-rule evaluation (reuse `expr` AST → evaluator).
- [x] Effect compiler → Iceberg **catalog-level** write (equality-delete on PK + append), one txn.
      All three operations: `create` / `modify` / `delete`, behind a third port (`RowWriter`).
- [x] `loom run` — one declared action, through the same entry point `run_<action>` will call.
- [ ] Optimistic concurrency — version/snapshot check; conflict → typed retryable error.
      *The snapshot is already captured and reported; what is missing is the check.*
- [ ] Edit-log (audit) table — actor, action, before/after, snapshot id.
- [x] Tests: create / modify / delete happy paths, against the fake catalog and against live
      pyiceberg. The concurrent-conflict path lands with the check.

**Eight decisions taken in the first slice** (parameter binding, rule evaluation, one row written):

- **Rows go through a *third* port, and none of the three is a superset.** M2's argument — the
  resolver holds a `Catalog` and therefore cannot execute DDL — points both ways one level down.
  `CatalogWriter` changes a table's shape and has no verb for deleting a row, so `loom apply`
  cannot touch data. `RowWriter` changes its rows and has no verb for altering a schema, so an
  action cannot touch DDL. `append_rows` stays on the schema port because it is how `_loom_meta`
  records history: purely additive, incapable of destroying anything. `writer_for` grows a
  *sibling* rather than an argument — `row_writer_for` — both over one exchange point whose error
  names the catalog and the plane it refused. The handle is acquired per run, for the one catalog
  the target object binds, so no serving process holds a row-writable catalog between calls.

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

---

## ⏳ M4 — MCP write surface + transport hardening

*Goal: the action runtime shows up as tools; serve over more than stdio.*

The read tools, the registry, and stdio `loom serve` land in M1 — what's left here is
everything that depends on M2/M3 or on a second transport.

- [ ] Per action: `run_<action>` with JSON Schema from parameters, description from the spec.
- [ ] Capability negotiation at serve — validate spec features vs. `engine.capabilities()`.
- [ ] HTTP transport alongside stdio.
- [ ] Structured tool errors — surface validation-rule failures and write conflicts as typed
      results an agent can act on, not opaque strings.

---

## ⏳ M5 — Governance

*Goal: row/column policies enforced identically for API and MCP callers.*

- [ ] Design the `governance.policies` grammar (deliberately deferred in v0).
- [ ] Enforce in the **resolver** (below MCP) so direct + agent calls filter the same way.
- [ ] Column masking + row predicates; policy tests over both paths.

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
