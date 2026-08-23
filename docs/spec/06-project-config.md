[← Spec index](../spec-v0.md)

# 6. Project config — `loom.yaml`

Not part of the ontology, but the grammar the ontology's `catalog:` / engine references
resolve against.

```yaml
version: 0
catalogs:
  rest_main:
    type: iceberg-rest            # v1 catalog type
    uri: https://catalog.internal/api
    warehouse: s3://lake/warehouse
    auth: { type: oauth2, ... }   # opaque to the spec; passed to the catalog client
engine:
  type: duckdb                    # duckdb | trino | spark  (read path; §7 of the design)
  options: {}                     # engine-specific
mcp:
  name: loom
  transport: stdio                # stdio | http
  writes: false                   # expose run_<action> tools at all · default false
  actor: null                     # who a served run is recorded as · declared, never inferred
  host: 127.0.0.1                 # http only · the bind, and the posture
  port: 8000                      # http only
  path: /mcp                      # http only
  allowed_hosts: []               # http only · Host allow-list; derived on a loopback bind
  auth:                           # http only · the authorization server this deployment believes
    issuer: https://issuer.example
    audience: loom-prod
    jwks_uri: https://issuer.example/jwks
    clock_skew: 0                 # seconds, bounded — for drift, not for longer sessions
    claims: { dept: string, groups: "string[]" }   # what a policy may name of a caller
  embedding:                      # optional · where this deployment's vectors come from
    provider: local               # local | openai · default local — nothing leaves the machine
    model: bge-small-en-v1.5      # required, never defaulted — it is hashed with every vector
ingest:                           # optional · how rows get in, in bulk · §6.2
  - name: daily-sales             # required, unique — a refusal names it
    objectType: DailySalesPerformance
    mode: replace                 # append | merge | replace · required, never defaulted
    format: parquet               # parquet | ndjson | csv · required, never inferred
    columns:                      # property -> source column · defaults to the identity
      salesDate: sales_date
governance:                       # optional · what this deployment withholds, and what it demands
  edit_log: optional              # optional | required · refuse to run if it cannot record a write
  ingest: refused                 # refused | allowed · whether the loads above ever run
  policies:
    - name: hide-ltv              # required, unique — a refusal names it
      objectType: Customer        # the objectType it governs
      mask: [ltv]                 # properties withheld from every read of that type
      rows: "object.deletedAt == null"   # and the rows it will show · §5, narrowed
    - name: own-orders
      objectType: Order
      rows: "object.customerId == principal.sub"   # the caller, folded in per call
    - name: gold-desk
      objectType: Customer
      when: "principal.groups contains 'gold-desk'"  # which callers this applies to · rows only
      rows: "object.tier == 'gold'"
```

## 6.1 `governance`

**A policy may name the caller, and exactly half of one may.** `rows:` can be conditioned on who is
asking — by a `when:` guard, by a `principal.<claim>` inside the predicate, or both. `mask:` cannot,
ever, and that split follows from the first rule below rather than from what is built: a mask
*announces itself* in the tool description, the `filter` schema and `masked`, and §7 says the tool
set and its argument namespaces are a function of the spec. A per-caller announcement means a tool
set assembled per caller, or narrowed for everyone, or silent — so `mask:` beside `when:` is
**refused**, and *HR sees `ssn` and nobody else does* is still served by two deployments.

**Every surface that cannot attest a caller refuses a config whose policies name one.** `loom query`,
`loom run` and a spawned stdio server can never name anybody, and the alternative — apply only the
unconditional policies — is disqualified by *policies subtract, never add*: skipping the guarded
ones shows that caller **more**, and `loom query` becomes the way to read what the served surface
withholds. So the file means one thing and two surfaces decline it, loudly, before reading anything.
That refusal is not a check that names a surface: a decided policy set is what a read needs, and
asking for one while naming nobody is what fails.

A policy that names no caller is **deployment-scoped** exactly as it was: one `loom.yaml` filters one
way for every caller of it. `mcp.actor` still gains no second reader; it stays a string about a
deployment that reaches the edit log and nothing else, and an *attested* principal is what a policy
reads — a different kind of thing, from a different source.

`audit:` was reserved beside `when:` and is gone from the grammar rather than still sitting in it —
see "`edit_log` is a posture, not a policy" below. With `when:` enforced, nothing is reserved here:
a key that is neither enforced nor moved is simply unknown, and refused as one.

**What `when:` and `principal.` may say.** One expression language (§5), with a third reference form
whose rule is the same as the other two: *a reference is legal where its declaration is in scope.* A
bare name is an action parameter; `object.x` is a property the ontology declares; `principal.x` is a
claim **`mcp.auth.claims` declares**, in the deployment file, beside the issuer that mints it — so it
is legal in a policy there and refused in an ontology, which is what keeps a spec deployment-blind.
Claims are declared with types (`string`, `string[]`, `boolean`) for the reason every other
reference form is checked against a declaration: an undeclared one makes a typo invisible, and this
one would fail *closed* and silent. A guard also gets `contains`, which `rows:` refuses — a guard is
answered once, in process, over a list only Loom holds, while a predicate must be answered twice and
agree with itself.

A guard composes as an **implication**: a policy whose guard is false withholds nothing. That is why
it is not sugar for a longer `rows:`, where the same text would withhold everything instead.

**A missing claim is not a false one**, and it fails in the withholding direction: an undecided guard
**applies** the policy. That is the opposite disposition from the refusal above, under one rule that
covers both — *decidable at pairing time with somebody to tell → refuse; decidable only per call,
with only the caller to tell → withhold silently*, because telling a caller which policy did or did
not apply to them is the existence oracle this section refuses everywhere else.

**A principal never reaches the resolver.** What varies per call is a decided policy set, selected
above it: a principal is constant for the duration of a call, so everything it conditions folds to a
literal before the call begins — including a predicate naming the caller — and every enforcement
site reads a set that is already decided.

**Two rules decide the rest.**

*The schema is public; the data is not.* A **mask announces itself** — in `masked` on every read
tool's result, and in the tool description — because the property names are already in the spec and
in the JSON Schema, so saying "withheld" tells a caller nothing new. A **row predicate does not**,
because the rows *are* the data: a withheld row is simply **absent**, `get_` answers `found: false`
in the same words it uses for a key that never existed, and no tool description gains a sentence.
The same rule settles two questions that look unrelated. **Filtering on a masked property is
refused, not answered emptily**, since an empty result is an oracle (a substring filter on a
withheld column binary-searches its value) while a refusal only repeats what the mask already said;
a masked property also leaves the `filter` schema, for the reason `loom serve` refuses to start
rather than advertise a tool that fails on every call. And **a row predicate that cannot be
evaluated over a row cannot report it** — per row there is no channel, and per call, "this row
exists but I could not decide about it" is the existence oracle again. So it does not admit, which
is the whole of the null question below.

*Policies subtract, never add.* None can grant, and none can widen what the config already permits —
which is why `writes` above stays a switch of its own rather than being subsumed. Masks union, so
declaration order cannot matter.

**Four things a mask cannot withhold**, each refused where the spec and the deployment are paired
(`bind_reads` and `bind_writes`, the same place §6's engine negotiation happens, so `loom query`,
`loom run` and `loom serve` refuse identically — and each checked as though every policy always
applies, which costs nothing, because a mask may not carry `when:` at all):

- a property or objectType the spec does not declare — a policy protecting a misspelling protects
  nothing and looks exactly like one that works;
- a **primary key**: every surface addresses a row by it, so withholding it withholds the object
  rather than a property. Not declaring the object type is the honest spelling of that intention;
- a property a **link** joins on, whose value is the link's whole meaning;
- a property an **action** reads in a rule or writes in an effect. This one is about a *combination*
  — the spec is fine and the policy is fine — and it is what keeps §9.2's guarantee true: a rule
  reading a withheld property is an oracle the caller drives, and an effect writing one changes data
  the deployment says the caller may not see.

**None of the last three carries over to `rows:`**, and that is a consequence rather than an
oversight: each of them is a surface still trying to *use* a value it may not read. A predicate uses
the value and shows nobody, so it may filter on a primary key, on a link's join property, or on a
property an action reads. Only the first refusal survives — a predicate naming a property the spec
does not declare is the same invisible typo a mask is, and so is one naming a claim `mcp.auth.claims`
does not declare.

**Where it is enforced: the projection, on both planes.** A masked property is never *selected*, so
it is not in the result set for any layer above to forget to drop, and `_loom_meta.edits` inherits
the mask because `before`/`after` are built from the same projection. The physical row is untouched:
a masked column is **carried across** a `modify` exactly as an unmapped one is (§4.1) — withheld
from the account of the write, never from the write, or the policy would destroy the data it exists
to protect.

**`rows:` is §5, narrowed to what two evaluators can be made to agree on.** It is the same
expression language a validation rule is written in — one grammar, one parser, `object.<prop>` for
a property, and no parameters because a policy has none. What it may *contain* is smaller:
comparisons between the row's own properties and literals, composed with `&&`, `||` and `!`, and
nothing else. Arithmetic, `lower()`, `len()` and `coalesce()` are **refused at load, naming the
node**, on one rule — *a predicate is lowerable when Loom, not the engine, decides what every
operator means.* `now()` is refused with them, for the one reason that is not about engines: it
puts a clock inside a filter, and *which instant, the read's or the run's* is a decision worth
writing down rather than inheriting. The subset may only ever widen, which accepts predicates that
used to be refused and cannot change one already written.

It has to mean the same thing twice, because the two planes read differently: the read path
**compiles it into the query** (it must filter before `LIMIT`/`OFFSET`, or `hasMore` lies), and the
write path **evaluates it in process over one row**, because the action runtime reads through the
catalog rather than the resolver and an agent that cannot see a row must not be able to act on it.

*Null: three answers, one admission rule.* A predicate is true, false, or **undecided**, and a row
is admitted **only on true**. `==` and `!=` never return undecided — §5's "null is a value" is kept
exactly, which is what makes `object.deletedAt == null` expressible, and it is carried into SQL as
`IS NOT DISTINCT FROM` rather than `=`. Ordering a null is undecided rather than an error, and
`!`, `&&`, `||` propagate it by the rules SQL's own `NOT`/`AND`/`OR` already follow — so the two
lowerings agree by construction, and negation stays fail-closed. The alternative that looks
simpler — make every leaf definitely true or false on both planes — fails open: `!(object.ltv >
100)` becomes `!false` for a null `ltv` and *admits* the row a policy was written to exclude.

*Applied to every governed table a read touches*, so a link is not the way around a filter: you
cannot traverse *from* a customer this deployment does not show you, and you cannot land on one.
And *the write path gates the row it read, never the row it writes* — a `modify` may move a row out
of the predicate, which is what a soft delete is. One consequence is deliberate and worth naming: a
`create` still reports `object_exists` for a key held by a row the policy excludes, because that
check has to be physical or two creates manufacture a duplicate primary key nothing can repair. On
that one path a predicate hides rows and not keys, and what it discloses is confined to *something
exists under the key you supplied*.

**`edit_log` is a posture, not a policy — which is why it sits beside `policies:` rather than in
it.** `audit:` was reserved inside the policy grammar for two slices, and it named two clauses that
turned out to belong in different places, so the key left rather than landing.

*"No log, no write"* subtracts an ability, which is the shape a policy has — but it names no
objectType, because unloggability is a fact about a **catalog**: the log is one table per catalog
(§9.2), reached through a port a catalog either implements or does not. A per-type spelling would
let a config say "Customer edits must be logged, Order edits need not" about a single fact
concerning a single catalog. It is also a switch an operator reads, which is exactly why `writes`
below is not a policy either.

**What it promises is about a deployment, never about a run**, and the name says so. There is no
transaction spanning a row's table and `_loom_meta.edits`, so *every applied run is logged* is not
available at any price. `edit_log: required` says the one thing that is true — **a deployment that
cannot record what it writes does not start** — and it is checked where the spec and the deployment
are paired, in `build_runtime`. It is the one governance key that binds a single plane: the read
path writes no rows, so it produces no records, so `build_resolver` has nothing to check.

Two kinds of unloggability, both knowable before any row is written, which is what makes this a
startup question:

- **structural** — the catalog implements no edit-log port, so every run against it writes its row
  and reports `log_failed` afterwards, for as long as the deployment lives;
- **physical** — the `_loom_meta` namespace or the table cannot be created. Provable only by doing
  it, so the check **creates the table** rather than probing for one. `table_exists` asks the wrong
  question (`false` is the ordinary state of a catalog whose first append has not happened), and
  creating a table records nothing that might not have happened — an empty log is a permission, not
  an intention, so this does not reopen §9.2's ordering.

A **per-write probe** was rejected, and not only because it narrows the window rather than closing
it: it is nearly blind. The log lives in the *same catalog* as the row it describes, so a catalog
nobody can reach already fails the row write, with nothing written and nothing to record. The
failures worth catching are specific to the log table, and the only probe that sees those is an
append — which is log-then-write, a table of intentions that may never have happened. So the whole
of this posture is spent at startup, and no round trip is added to the path of every action.

**Nothing after the write changes under either posture.** An append that fails once the row has
committed still comes back as `log_failed` beside an unchanged status, because *the row committed,
so `failed` would tell a caller to retry a delete that already happened* is not an argument a
config weakens. What survives that window is what always survived it: the commit carries
`loom.edit_id`, so a lost record is a stamped snapshot with no matching row — a gap a reader can
find.

**Default `optional`**, for the reason `writes` is off by default: an upgrade and a catalog with no
edit-log port are two things that happen for unrelated reasons, and neither is a deployment asking
to stop working.

*Retention is not here, and no key is coming for it* — see §9.2 and "Open edges".

**`engine.type` is negotiated against the ontology, not just resolved.** An adapter reports what it
can do, and a spec implies what will be asked of it: declaring a link means a traverse joins two
backing tables, declaring a *string* property `searchable` means a filter is a case-insensitive
substring match (an enum is a closed set and matches exactly, so it implies nothing), and the page
arguments §7 puts on every read tool mean a second page is an `OFFSET` — that last one required by
the surface rather than by anything a spec says. An engine that cannot do one of them is **refused
where the two are wired together**, so `loom query` and `loom serve` refuse identically, and the
message names the declaration to go and look at. The surface is never narrowed to fit: §7's tool set
is a function of the spec, and matching exactly where this section promised substring would return
rows rather than fail, which is the one failure an agent cannot see.

The four address keys mean nothing without an address, so setting any of them under
`transport: stdio` is an **error** rather than something quietly dropped — the same rule
`governance.policies` follows, for the same reason: a config that is silently ignored reads, to
whoever wrote it, exactly like one that was obeyed.

**`writes` is off by default, and that is a decision.** Until the action runtime became a tool set,
`loom serve` could not change anything, and deployments were pointed at real lakes on that basis.
Defaulting it on would mean an upgrade plus a spec that declares an action — two things that happen
for unrelated reasons — silently making a production lake mutable by any MCP client. So a deployment
says so, in the file a deployment is configured by. It is deliberately not a CLI flag (a flag lets an
invocation contradict the file an operator reviews) and deliberately not a governance policy: it
names no principal and filters no row. It is a switch on a whole surface, which §6 owns and §7
generates against — though M5 may end up subsuming it.

**`actor` is what a served run is recorded as** in `_loom_meta.edits`. Unset, runs record `unknown`.
See §4.1's "the actor is supplied by the caller, never invented" for why this is a config key rather
than an inference or a tool parameter.

**The transport has an address now, and the address is the posture.** `stdio` has none: a client
spawns the process and owns it. `http` is reachable by whoever can reach the port, and that single
difference decides everything else in this block.

*All of it is config, including the port.* The argument that put `writes` here — a flag lets one
invocation contradict the file an operator reviews — is weakest for a port number, which is not a
posture at all. It goes here anyway, because a file that describes half an address does not describe
the server, and reviewing the deployment would mean reading the unit file too. The host is the
strongest case rather than the weakest: it *is* the posture, and it is precisely the question this
file should answer.

*`host` defaults to `127.0.0.1`*, for the reason `loom apply` refuses to run unattended: do not put
somebody's lake on a network because nobody said to. `loom serve` speaks cleartext — there is no TLS
key, and terminating TLS belongs to whatever sits in front — which is a second reason the default
bind is local.

*`writes: true` is refused on a non-loopback bind*, at startup, the way `cmd_serve` already refuses
rather than advertising tools that will fail. **Writes over a socket are not the same decision as
writes over a pipe**, and the difference is reachability rather than transport. It is worth being
exact about why, because the obvious reason is wrong: `actor` lives in *this file*, so it always
named a deployment rather than a session — three stdio clients reading one `loom.yaml` already
record one string for three callers. Many callers under one name is not what a socket introduces.
What a non-loopback bind changes is who is permitted to *be* one of those callers: over stdio,
whoever can run the binary and read this file; over `0.0.0.0`, whoever can reach the port. That is
where `actor:` stops being a statement anybody checked. The check is honest about its own limit —
it constrains what Loom **binds**, not what **reaches** it, and a proxy in front of a loopback bind
is outside anything this file can see.

*`allowed_hosts`* is the `Host` allow-list behind DNS-rebinding protection, which is always on. It
is optional exactly where it can be derived: a loopback bind knows the three names it answers to. A
non-loopback bind does not know the name the world reaches it by, so it is required there rather
than guessed.

## 6.2 `ingest` — how rows get in, in bulk

An entry names an object type, a mode and a file format. `loom ingest <entry> <file>` runs one, and
the file is the only thing it takes on the command line, because the file is the only thing that
varies per run.

**It is in `loom.yaml` and not in the ontology, and that placement is the design.** §7 says the tool
set, its names and its argument namespaces are a function of the spec. Put ingest in the spec and
something has to decide whether an `ingest_<type>` tool appears on the MCP surface — and the answer
is **no**, for the reason `loom serve` exposes no raw-SQL tool: a verb that writes an arbitrary batch
is not a declared single-object action, and handing one to an agent gives back everything §4's
boundary was built to withhold. Declaring it here means no tool can be *derived* from it,
structurally rather than by a rule someone remembers not to break. The precedent is
`governance.policies`, which also lives here and also names an `objectType`.

What an entry therefore *is*: a fact about a deployment — this warehouse gets its daily numbers from
a Parquet drop — rather than a fact about the ontology, which is the same test that put catalogs and
engines here.

**Three modes, and they differ in what they destroy.**

| mode | what it does to the rows already there | reads first? |
|---|---|---|
| `append` | adds to them | no — and therefore asserts no snapshot |
| `merge` | replaces the ones the batch names, by primary key | yes |
| `replace` | the table becomes exactly the batch | yes |

`merge` carries **every column the ontology does not map** across from the existing row. That is
§4.1's rule at batch scale and it is why `merge` is a mode rather than a flag on `append`: a merge is
an equality-delete plus an append, so a column Loom never declared is carried or it is silently
nulled.

`append` is the one verb with no snapshot expectation, and the asymmetry is deliberate. An append
follows no read and puts no row over another — `EditLogWriter`'s argument at batch scale — so there
is no honest value to assert, and requiring one would make two pipelines loading the same table
refuse each other over a race neither can lose. `merge` and `replace` both assert: the first because
its carried columns come from a read, the second because it must not destroy a commit nobody saw.

**Ingest never migrates.** The `BulkWriter` port has no DDL verb. A batch that does not fit the table
is refused naming the column, and the fix is `loom plan` / `loom apply` — the never-drop rule pointed
at a new plane, refusing to infer a schema change from the shape of somebody's file.

**Neither half of a policy conditions a load.** A `mask:` withholds a property from a caller and a
load has no caller; a `rows:` predicate decides which rows a deployment will *show*, which is not a
claim about which rows may exist; and a `when:` guard is unanswerable where nothing can attest
anybody, exactly as it is for `loom query`. What governs ingest is `governance.ingest`, and what
demands a record of it is `governance.edit_log`.

**`governance.ingest` defaults to `refused`**, which is `mcp.writes`' posture rather than
`edit_log`'s, and the two defaults point opposite ways for the same test — *what does a deployment
that never asked for this get?* A deployment that never asked for the `edit_log` posture is not
asking to stop working; a deployment that never asked for bulk writes is not asking to become
bulk-writable. So declaring an entry is necessary and not sufficient, exactly as declaring an
`action` is necessary and not sufficient for `run_<action>` to be served.

**`governance.edit_log: required` covers both logs.** Its own words are *a deployment that cannot log
does not run*, and they were written when the only thing Loom could write was one row through an
action. A bulk load is a write, so a posture that proved only `_loom_meta.edits` would leave a
deployment able to load unrecorded while believing it could not — which is the half-truth ingest
exists to close, reproduced inside the fix for it. Under `required`, `_loom_meta.loads` is created at
startup too.

**A load has an identity, and re-running one is a refusal.** The id is supplied with `--load-id` or
derived from the entry, the mode and a SHA-256 of the file's bytes. It is stamped into the write's own
Iceberg commit as `loom.load_id` — the only record of a load that is atomic with it — and recorded in
`_loom_meta.loads` beside the row counts, the source path and its fingerprint. A pipeline that times
out and retries hands Loom the same file, and Loom answers *that is one load happening twice*; an
operator who meant the other thing says so with `--load-id`.

**A refusal is whole.** One bad value refuses the whole batch, because a partial load leaves the lake
in a state nobody declared. `--reject-to <path>` is the escape hatch and it is narrow: it quarantines
the rows that failed **their own** checks and loads the rest. It cannot rescue a load whose columns
are wrong — there is no subset of a batch that has the right columns — and it cannot absorb a
duplicate primary key, because choosing which of two rows sharing a key survives is a decision the
file does not contain.

**A zero-byte file cannot empty a table.** `mode: replace` with an empty batch is a real value — a
materialization whose source went empty is saying so — but an empty NDJSON declares no *columns*, so
it fails the ordinary column check with no special case anywhere. A truncated upload and a deliberate
empty batch are the same zero bytes, and one of them wipes a table. A header-only CSV or an empty
Parquet table can say *these columns, and no rows*; NDJSON cannot.

**Formats are files, and that is the boundary.** `parquet`, `ndjson`, `csv`. Loom does not connect to
Kafka, crawl an object store or open a JDBC connection. A pipeline hands Loom a batch; Loom decides
whether that batch may become rows. Adding a fourth format is a small decision about parsers; adding
a *source* would be a large one about what Loom is.

## 6.3 `mcp.embedding` — where a vector comes from

A spec declares `semantic:` on an object type (§2 rule 7); this says which model computes it. The
split is the one `engine:` and `mcp.actor` already make: *that this property is worth searching by
meaning* is true of the model wherever it is deployed, and *which model, running where* is true of
one deployment. So one ontology serves with a local model in a lab and a hosted one in production,
and no spec file changes.

**`provider` defaults to `local`** — the model runs in this process and no row's text leaves the
machine. `openai` is the option, and it is named here rather than accepted as any string so the set
of places a lake's text can be sent to is something this file enumerates. The same posture as the
loopback default bind: reaching the network is a deliberate act.

**`model` is required and has no default.** It is folded into the hash stored beside every vector,
so a default Loom could change in a later release would silently invalidate every vector in every
warehouse that took it — and the failure of a stale vector is not an error, it is a ranking that
means nothing.

**There is no `dims` key.** The width is a property of the model, so declaring it beside the model
name is a chance to declare it *wrong*, and that failure is silent in the same way: vectors of the
declared width get written and ranked against each other. The provider is asked, and what it answers
is recorded per row as an observed fact rather than a declared one.

**Absent means no semantic tools, not a refusal**, and the contrast with engine negotiation is
the point. `check_capabilities` asks *could this engine ever serve what this spec describes* — a
spec's own claim, so a mismatch is a contradiction and refuses to start. This key asks *does this
deployment switch it on*, and a deployment that configures no provider is not describing a
contradiction. It is one of the deployments that reads without embedding, exactly as a deployment
with `writes: false` is one that serves without actions. The surface is what the deployment permits.

---

[← §5 Expression mini-language](./05-expressions.md) · [§7 What the grammar compiles to →](./07-compilation.md)
