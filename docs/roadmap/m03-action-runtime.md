[← Roadmap index](../ROADMAP.md)

# ✅ Done — M3: Action runtime (single-object writeback)

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
  them would leak someone else's data past a governance layer that doesn't exist yet. *(M5's first
  slice extended that rule to the layer once it did exist: `before`/`after` also drop what a policy
  masks, and the unmapped columns are still carried — see below.)*

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

[← M2](./m02-migration-engine.md) · [M4 →](./m04-mcp-write-surface.md) · [backlog](./backlog.md)
