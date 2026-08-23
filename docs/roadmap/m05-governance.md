[← Roadmap index](../ROADMAP.md)

# ✅ Done — M5: Governance

*Goal: row/column policies enforced identically for API and MCP callers.*

The milestone's first question was not on the list: **does attested identity come before or after
the grammar?** If policies are designed to filter against `mcp.actor`, they get shaped around a
value about to be replaced by a real per-call principal — and MCP's authorization is an OAuth 2.1
resource-server profile, which M4 called a milestone rather than a slice.

**After**, and the argument is structural rather than a matter of sequencing: `loom query` and
`loom run` have no transport, so nothing can ever attest an identity to them, and a spawned stdio
server carries no bearer either. A grammar expressible only against an authenticated caller would
make the *direct* half of this milestone's own claim — a direct call and an agent call filter
identically — ungovernable by construction, and would leave governance existing only over HTTP,
which is the transport-dependent surface M4's second slice spent a slice proving Loom does not have.
So M5 enforces **deployment-scoped** policy: one `loom.yaml` filters one way, for every caller of
it, and two audiences are served by two deployments. `mcp.actor` gains no second reader. The clause
attestation unblocks is reserved in the grammar and refused, so nothing written against v0 has to
change when it lands.

- [x] Design the `governance.policies` grammar (deliberately deferred in v0). *Named subject,
      named effect, and every key Loom cannot enforce refused rather than ignored.*
- [x] Enforce in the **resolver** (below MCP) so direct + agent calls filter the same way. *In the
      projection, and in the action runtime's own projection, which is the half a read-only reading
      of that sentence would have missed.*
- [x] Column masking + row predicates; policy tests over both paths. *Masking landed with the
      grammar; `rows:` landed in the slice below, compiled into the query on one plane and
      evaluated in process on the other, with the agreement of the two asserted differentially.*
- [x] What the **edit log** holds under a policy. M3 deliberately left three questions here rather
      than answering them where nobody could turn the answer off: whether `_loom_meta.edits` masks
      under the same policies as a read (it records declared properties and the bound parameters —
      less than the physical row, still somebody's data, in a table that outlives the row it
      describes); whether a retention window expires it; and whether "no log, no write" is
      expressible, since an unloggable run used to happen anyway and report `log_failed`. The
      record's *shape* is not deferred — the columns are fixed, because the table is only ever
      created. *The first is answered by the first slice; the third by the third slice, as
      `governance.edit_log` — which is a posture beside `policies:` and not a policy in it. The
      second ends in a **refusal**: expiry by deletion is not deferred, it is decided against, and
      `audit:` left the grammar rather than sitting in it as a reservation nothing would ever
      honour.*

**Eight decisions taken in the first slice** (the grammar, and column masking end to end):

- **A mask announces itself; a row predicate will not — and one principle decides both.** *The
  schema is public; the data is not.* The property names are already in the spec, in the tool
  description and in the JSON Schema, so `masked: ["ltv"]` on every read result tells a caller
  nothing the surface did not already say. The rows *are* the data, so "you may not see this one"
  is an existence oracle over it and the next slice will make a filtered row simply absent. The same
  principle settles a question that looked unrelated: **filtering on a masked property is refused,
  not answered emptily.** An empty result is an oracle — a substring filter on a withheld column
  binary-searches its value in a handful of calls, an exact one confirms a guess — and a refusal
  gives away only what the mask already said. The masked property also leaves the `filter` schema,
  because `cmd_serve` already refuses to advertise tools that fail on every call and an argument
  that fails on every call is the same thing one size down. The refusal is in the *resolver*, so
  `loom query` meets it too; the schema is only the surface not advertising it.

- **Policies subtract, never add — which is what settles `mcp.writes` rather than subsuming it.**
  M4 left "M5 may subsume it" hanging, and the answer is no. A policy can withhold; none can grant,
  and none can widen what the config already permits, so folding the write switch into the policy
  list would turn "is this server writable" from a line an operator reads into a set they have to
  evaluate. Composition is then total and order-free (masks union), and monotonicity is asserted
  rather than asserted-in-prose.

- **Withheld by never being *selected*.** The mask is applied to `Resolver._projection`, so a
  masked column is not in the plan, not in the compiled SQL, not in the Arrow batch pulled out of
  Iceberg, and therefore not in a result set that any layer above could return by forgetting to
  drop it. The alternatives — nulling the value, or a `"***"` sentinel — were rejected for reasons
  Loom has already paid for once: a null lies to the agent exactly the way a narrowed `Contains`
  would, and a sentinel puts a string in a `decimal` property, which spends "declared types are
  honored on the way in and out". Dropping the key is the only one of the three that withholds
  without lying, and it is the only one that never reads the data it is withholding.

- **A masked property is one *no* surface returns, so the write path was in this slice.** A mask on
  the read path alone would have shipped a mask that does not mask: `run_<action>` reports `before`
  and `after` as declared properties and `dryRun` returns them without changing anything, so a
  withheld `ltv` would have been one preview away from any caller with an action on that type.
  `_Run._project` withholds exactly what `Resolver._projection` does, and `_loom_meta.edits`
  inherits it because the record is built from the same projection — which answers the first of M3's
  three edit-log questions. What the runtime did **not** grow is a branch on a policy.

- **The two ways an action could still touch what it cannot see are refused at bind, not evaluated
  at run.** A validation rule that reads a masked property is an oracle the caller drives
  (`upgradeTier` refuses when `newTier == object.tier`, so three calls learn a withheld tier), and
  an effect that writes one destroys data the deployment says this caller may not read. Both are
  static facts about a spec — `model.properties_in_play` already had to name that exact set for the
  conflict detail, so it moved out of the runtime and became one definition with two readers — so
  the deployment refuses to start and nothing in the four steps has to know. This is also what keeps
  the carry-across honest: spec-v0's open edge asked whether a masked column is carried or the write
  refused, and the answer is **carried**, exactly as an unmapped column is, because the alternative
  destroys the data the policy exists to protect. Withheld from the account of the write, never from
  the write — and M3's *what the record does not name, the run did not change* survives word for
  word, because nothing can write what nobody can read.

- **Two more things a mask cannot withhold, and neither is an implementation limit.** A **primary
  key**: every surface addresses a row by it, so masking one withholds the object rather than a
  property, and not declaring the object type is the honest spelling of that intention (it is also
  what guarantees a projection is never empty). A property a **link** joins on: the value is the
  link's whole meaning. With the action rule above, that is four refusals, all collected and
  reported at once — `check_capabilities`' bargain, because somebody reconciling a policy file with
  a spec should learn the whole of what disagrees in one reading.

- **Checked where the spec and the deployment are paired, which M4 already argued for.** The
  capability slice said it was borrowing M5's principle a milestone early; this is the milestone
  paying it back through the same function. `build_resolver` binds policies beside
  `check_capabilities`, and `build_runtime` — which existed, was exported, and was called by nothing
  while `loom run` and `build_server` each constructed their own runtime — became the write plane's
  equivalent and is now called by both. Two constructions are two chances for one of them to be the
  ungoverned one, and `loom run` is precisely the direct caller this milestone's claim is about. Not
  in `loom validate`: a spec that is valid stays valid whatever a deployment withholds of it, and
  that command does not require a `loom.yaml` to exist at all.

- **A key Loom cannot enforce is refused key by key, where the whole block used to be refused
  wholesale.** `_check_governance` used to reject any non-empty `policies:` on the grounds that
  silently ignoring an access policy is far worse than not booting. That rule did not go away when
  enforcement landed; it moved down a level. `ENFORCED_KEYS` and `RESERVED_KEYS` must cover the
  grammar exactly under a test — `negotiate.NEGOTIATED`'s device, applied to a grammar — so a fifth
  key has to be declared as one kind or the other rather than arriving as the third kind Loom has
  already been bitten by: accepted, unenforced, and silent about it. `rows:` and `audit:` are
  refused as *not yet*; `when:` is refused as *this deployment cannot tell callers apart*, with the
  posture that works today in the hint.

  One trap worth recording, because the obvious grammar walks into it: the subject key is
  `objectType:` and not `on:`, because **YAML 1.1 resolves the bare key `on` to the boolean `True`**
  — a policy written the obvious way arrives with a key no grammar can name. `objectType` is also
  Loom's own vocabulary for the same thing (§7's `traverse` takes one). `_shape.check_keys` was
  hardened in passing: it used to hand a non-string key to `difflib` and raise instead of reporting.

**Six decisions taken in the second slice** (`rows:`, lowered two ways):

- **Neither way out of the null question was available, and the third one is what the first slice's
  own refusal already implied.** A `rows:` predicate is parsed by `expr.parse()` — one language —
  but lowered twice: compiled into the query on the read path (it must filter before paging, or
  `hasMore` and `offset` lie), and evaluated in process over one row on the write path, because
  `ActionRuntime` reads through the `Catalog` port and an agent that cannot see a row must not be
  able to act on it. M3's evaluator is two-valued and SQL is three-valued, so the same predicate
  could admit on one plane and drop on the other.

  *Emulating two-valued semantics in the lowering* — totalize every leaf so it is definitely true
  or false on both planes — **fails open under negation**: `!(object.ltv > 100)` becomes `!false`
  for a null `ltv`, and a predicate written to exclude admits. *Refusing any predicate that touches
  null* costs `object.deletedAt == null`, the most ordinary policy there is, and still does not
  close the question, because a table can hold a null in a column the spec declares non-nullable —
  Loom already knows tables contradict specs, which is why `ambiguous_key` exists.

  So: **three answers, one admission rule.** True, false, or **undecided**, and a row is admitted
  only on true. `==` and `!=` never return undecided — §5's *null is a value* is kept exactly and
  carried into SQL as `IS NOT DISTINCT FROM`, which is the one operator where §5 and SQL genuinely
  disagree and the one place we intervene. Ordering a null is undecided rather than an error, and
  `!`, `&&`, `||` propagate it by the rules SQL's own connectives already follow, so the two
  lowerings agree by construction rather than by emulation, and negation stays fail-closed. What
  forced *undecided* over M3's `expression_error` is the first slice's argument, not a new one: a
  policy predicate has nobody to tell. Per row there is no channel, and per call, "this row exists
  but I could not decide about it" is the existence oracle that slice refused. So M3's rule is
  untouched where it applies, and what differs between a rule and a policy is not the meaning of an
  operator but **the disposition of "cannot decide"** — which is now written into `evaluate.py`
  beside the two-valued argument it qualifies. The agreement is a claim, so it is an assertion:
  every predicate in a corpus against every row of a null-saturated table, through real DuckDB and
  through the in-process evaluator, admitted sets compared.

- **The lowerable subset is a rule, not a list.** *A predicate is lowerable when Loom, not the
  engine, decides what every operator means.* Operands are `object.<prop>` references and literals,
  operators are the six comparisons, composition is `&&`/`||`/`!`. Arithmetic and string `+` are
  refused because the engine computes them and engines disagree — integer division, and the
  decimal/float mixing `evaluate.py` deliberately *refuses* while SQL silently coerces; `lower()`,
  `upper()` and `len()` because case folding and length are the engine's answers; `coalesce()`
  because it is the null tool and what null means here is precisely what Loom owns rather than
  borrows per row. `now()` is refused for the one reason that is not about engines — it never
  reaches one — but it puts a clock inside a filter, and *which instant, the read's or the run's*
  is a decision worth writing down rather than inheriting. A bare identifier is refused too: it is
  a *parameter* reference in §5 and a policy has none, so one language keeps one meaning for each
  reference form. `LOWERABLE` and `NOT_LOWERABLE` partition `expr`'s whole operator and function
  set under a test — `ENFORCED_KEYS`' device applied to an expression language — and the set may
  only ever **grow**, which accepts what used to be refused and cannot change one already written.
  Two refusals are about the predicate rather than the grammar: ordering against a `null` literal
  (undecided for every row, so it withholds the object type while reading like a filter) and a
  predicate that names no property (the same answer for every row, either way).

- **The predicate rides on `ir.TableRef`, so both ends of a traverse are governed by one line.**
  `Resolver._table` is the only place an object type becomes a table, so a governed type cannot
  enter a plan without its filter — *you cannot search a customer but you can traverse to one* is
  not a rule anybody has to keep, because there is nowhere to write it. `GetByKey` is governed for
  free and a `through` table correctly carries none, standing for no object type. A `TableRef` with
  a predicate is a **view**: the read-path twin of a projection that never selects a masked column.
  Rejected: a `predicate` field on each of `GetByKey`/`Search`/`Traverse` — three places to
  remember, one of them with two ends. It is never a `ScanRequest` predicate, because that channel
  is a documented pushdown *hint* an adapter may ignore, and a governance filter must not be
  advisory anywhere; it contributes only its **columns** to the scan, since a policy may filter on
  a property it also masks.

- **A filtered row is absent on the write path too, and that survived contact.** `_Run` gates on
  the row it *read* — not admitted becomes `object_not_found`, in the words a concurrent delete
  already produces. The gate is on `before` and never on the result, because a `modify` that moves
  a row out of the predicate is exactly a soft delete (`deletedAt: now()` against
  `rows: "object.deletedAt == null"` — the most ordinary policy's most ordinary companion action),
  so refusing it would break the pair the feature exists for. `create` has no `before` and is
  ungated. Of a policy's two halves only the row half is a branch in the four steps, and it is one
  line. The refusal is still recorded: the run named a row, which is `_record`'s existing gate, and
  an audit trail that dropped these could not answer *who tried to act on a row this deployment
  does not show them*.

- **One existence oracle, named rather than discovered.** A `create` reports `object_exists` for a
  key held by a row the policy excludes. The check has to be physical, or two creates that both
  read past an excluded row both append and manufacture the duplicate primary key `_read` refuses
  forever after and Loom can never repair. So on exactly one path a row predicate hides rows and
  not keys, and what discloses is confined to *something exists under the key you supplied* — no
  property of it, and a key the caller chose. Where it is safe to say a policy's shape out loud is
  the mirror of this: the serve banner names a filtered type, because the operator starting the
  process holds the `loom.yaml` it describes; no tool description does.

- **The evaluator moved out of `action/`, and two predictions were corrected where they were
  written.** `evaluate.py` sat under the action package while it had one consumer; a governance
  predicate is evaluated over a row by the same rules and is not an action, so it is `loom.evaluate`
  now and the leaves of a policy go through the same `==` a rule does. §5.2 argued its null rule
  from *the language never reaches SQL*, which a compiled row predicate makes half false — the rule
  survives, argued from what it is for rather than from what was not listening. And `ir.py`
  predicted that ranges would arrive "with the filter grammar"; they arrived with governance
  instead, as a node set deliberately separate from `Eq`/`Contains` — those two are what a
  *caller's* `filter` argument compiles to, which is why `searchable` makes one of them a substring
  match, and `name == 'x'` in a policy is equality and never `ILIKE`.

**Five decisions taken in the third slice** (what the edit log holds under a policy, and what it
turns out no policy can say):

- **`audit:` named two clauses that belong in different places, so the key left rather than
  landing.** Testing each half against the shape the first two slices established — a policy has a
  subject (`objectType:`) and effects that subtract from what a caller gets, union, and are
  order-free — "no log, no write" **subtracts** but has **no subject**: unloggability is a fact
  about a *catalog*, since the log is one table per catalog reached through a port a catalog either
  implements or does not, and a per-type spelling would let a config say "Customer edits must be
  logged, Order edits need not" about a single fact concerning a single catalog. It is also a switch
  an operator reads, which is the first slice's own argument for keeping `mcp.writes` out of the
  list: folding a switch into a policy list turns a line somebody reads into a set they have to
  evaluate. So it is `governance.edit_log`, a sibling of `policies:`. Retention fails the subtraction
  test outright — it withholds nothing from any caller and deletes from the lake. **Splitting them
  was the better outcome than one key meaning two things**, and it is what let the second half end
  in a refusal instead of dragging the first half into one.

- **"No log, no write" is not a name Loom can ship, and the behaviour under it is.** Iceberg has no
  transaction spanning a row's table and `_loom_meta.edits`, so *every applied run is logged* is not
  available at any price, and a key that read that way would be the failure this block refuses
  everywhere else — a config promising more than it enforces reads, to whoever wrote it, exactly
  like one that was obeyed. `edit_log: required` therefore promises about a **deployment**: one that
  cannot record what it writes does not start. Both kinds of unloggability are knowable before any
  row is written, which is what makes it a startup question rather than a per-run one — *structural*
  (the catalog implements no port, so every run writes and reports `log_failed` forever) and
  *physical* (the namespace or the table cannot be created). The second is provable only by doing
  it, so the check **creates the table** rather than probing: `table_exists` asks the wrong question,
  since `false` is the ordinary state of a catalog whose first append has not happened, and creating
  a table records nothing that might not have happened — an empty log is a permission, not an
  intention, so it does not reopen `_record`'s ordering.

  The **per-write probe was refused for a stronger reason than narrowing-not-closing**: it is nearly
  blind. The log lives in the *same catalog* as the row it describes, so a catalog nobody can reach
  already fails the row write itself, with nothing written and nothing to record. The failures worth
  catching are specific to the log table, and the only probe that sees those is an append — which is
  log-then-write, a table of intentions that may never have happened. So the posture is spent
  entirely at startup and no round trip enters the path of an action. And **nothing after the write
  changes under either posture**: an append that fails once the row has committed still reports
  `log_failed` beside an unchanged status, because *the row committed, so `failed` would tell a
  caller to retry a delete that already happened* is not an argument a config weakens.

- **A retention window would spend the one property the ordering was chosen for, so it is decided
  against rather than deferred.** A reader holding a stamped snapshot with no matching row must be
  able to conclude one thing — *the record was lost* — and expiry makes it two, indistinguishable.
  Three further facts point the same way: nothing in Loom runs on a schedule, so a `retain: 30d`
  would be a default for a command nobody runs, which is `loom.managed`'s shape exactly; the port
  cannot delete, deliberately; and *deletion was never what was wanted*. What the log actually owes
  is **erasure** — declared properties outlive the row they describe — and erasure keeps the
  invariant if it is a **redaction in place**: keep the row and `edit_id`/`recorded_at`/`action`/
  `operation`/`status`, empty `parameters`/`before`/`after`/`object_key`. The skeleton stays
  citeable, the stamp still finds a row, the personal data is gone. That is a rewrite by a holder
  that is not the action runtime, so `EditLogWriter` gains no verb and the backlog names the shape
  rather than the wish.

- **A second verb on a one-verb port, and the argument that it widens nothing.** `ensure_log` is
  `append_edit` with the row taken out: same single table, same absent table argument, same DDL
  already reachable on a first append. The port's whole guarantee is *there is nothing to point at
  the wrong table with*, and that is unchanged — asserted, not asserted-in-prose, by a test that
  enumerates the verbs and checks no `table` parameter on any of them. The same test asserts the
  absence that matters more: **no verb removes a record, and none is coming.**

- **The one governance key that binds a single plane, and one stale bullet corrected.**
  `build_resolver` has no business with `edit_log` — the read plane writes no rows, so it produces
  no records, so there is nothing it could fail to record, and a resolver refusing to build over an
  unloggable catalog would make `loom query` unusable to protect a trail it never touches. So this
  is checked in `build_runtime` only, which is the asymmetry every earlier slice's "both planes"
  wording would have predicted wrongly. Two predictions were corrected where they were written:
  spec-v0's "refusing to act when the log is unavailable" edge called the clause a *policy* and
  bounded it as a per-write conversion, and both were wrong in the ways above; and the `rows:`
  open edge had **outlived the slice that closed it** and still read as refused.

---

[← M4](./m04-mcp-write-surface.md) · [M6 →](./m06-attested-identity.md) · [backlog](./backlog.md)
