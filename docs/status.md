# Status — how the milestones landed

The short version, plus the component table, is in the [README](../README.md#status). This is the
long version: what each milestone decided and why, in the order it was built. It is the narrative
companion to [`ROADMAP.md`](./ROADMAP.md), which tracks the same milestones as work items.

Early, but the **read path works end to end**: a YAML spec over real Iceberg tables, served to an
MCP client as typed tools. The **migration path** is complete — `loom plan` classifies what a spec
change would do to the physical tables, `loom apply` executes it (bootstrapping an empty warehouse
from nothing but a spec), and `loom rollback` puts an earlier spec back. And the **action runtime**
now writes: `loom run` binds an action's parameters, evaluates the validation rules the spec
declares, and mutates one row as one Iceberg commit — with the snapshot its read saw asserted
inside that commit, so a competing write refuses the run rather than silently losing to it. Every run
that named a row leaves a record in `_loom_meta.edits`, **including the ones that refused**, because
an audit trail of successes cannot say who *tried*; and the write stamps that record's id into its
own Iceberg commit, so the log is an index over facts the lake already carries rather than the only
copy of them. That completed M3 — and the runtime is now **reachable by the thing it was built
for**: `run_<action>` tools, one per declared action, with input schemas from the declared parameter
types and typed results an agent branches on rather than protocol errors it can only retry. Writes
are off unless `loom.yaml` says otherwise, and a served run is recorded as the actor a deployment
declared, or as `unknown` — Loom authenticates nobody, and a log that says so beats one that names
the wrong principal.

And `loom serve` now speaks **HTTP** as well as stdio, which is the first time a Loom process
outlives the client that started it. The tool set does not change — the surface is a function of the
spec, not of the transport — but two things about a shared process do. It answers **one tool call at
a time**, deliberately and out loud, because the DuckDB connection and the resolver underneath it are
built once per process, and the aliases every scan registers under are global. And the write
surface is bounded by the **bind**: `mcp.writes: true` refuses to start on anything but a loopback
address, because `mcp.actor` names a deployment rather than a caller, and that is only an honest
thing to record while the set of callers is the same one that could already run the binary.

That leaves M4 complete, because a spec and an engine are now **negotiated** before they are wired
together. An ontology implies things an engine has to be able to do — a declared link means a
traverse joins two tables, a searchable string property means a filter is a case-insensitive LIKE,
and the page arguments on every read tool mean a second page is an `OFFSET` — and an engine that
cannot do one of them is refused rather than served narrowly. Refused, because the alternatives are
to make the generated surface a function of the engine instead of the spec, or, worse, to quietly
match exactly where the spec promised substring: that one returns rows, so nothing fails and the
agent believes an answer that is wrong. The check sits where the two are paired rather than at
serve, so `loom query` refuses exactly what `loom serve` refuses.

**M5 is governance**, and its first question was one the boxes did not ask: whether an attested
per-call identity has to come first. It does not, and the reason decides the shape of everything
else — `loom query` and `loom run` have no transport, so nothing can ever attest an identity to
them, and a governance grammar written against an authenticated caller would leave the *direct* half
of "a direct call and an agent call filter identically" ungovernable. So a policy that names no
principal is a fact about a **deployment**, and the clause an attested caller turns on was reserved
in the grammar and refused rather than approximated against the deployment-wide `mcp.actor` — until
M6 gave it a caller to name. Both halves of a policy are live:

```yaml
governance:
  policies:
    - { name: hide-ltv, objectType: Customer, mask: [ltv], rows: "object.tier != 'bronze'" }
```

The property is withheld by never being *selected* — not in the SQL, not in the Arrow batch, not in
a result set anything above could return by mistake — from `loom query`, from every read tool, and
from an action's `before`/`after` and its edit-log record, since a `dryRun` would otherwise read out
what a policy withheld. It is still carried across a write, untouched, because the alternative
destroys the data the policy exists to protect. And masking a property some action reads or writes
refuses the deployment at startup rather than resolving it per call: Loom would rather not start
than serve a mask that an action can read around.

A **mask announces itself** and a **row predicate does not**, on one rule: the schema is public and
the data is not. The property names are already in the spec, so `masked: ["ltv"]` on every result
tells a caller nothing new; the rows *are* the data, so a withheld row is simply absent and `get_`
answers `found: false` in the words it uses for a key that never existed. The predicate is §5 — the
same expression language a validation rule is written in — narrowed to comparisons between the row's
own properties and literals, because that is the whole of what Loom rather than the engine decides
the meaning of. It is compiled into the query on the read path and evaluated in process on the write
path, so an agent that cannot see a row cannot run an action on it either, and the agreement of the
two is asserted differentially against real DuckDB over a table full of nulls. A row a predicate
cannot decide about is never admitted: negation stays fail-closed, and there is no way to report an
undecided row that is not an existence oracle over it.

**M6 is a caller with a name**, in two halves. `mcp.auth` makes Loom a **resource server and never an
authorization server**: it names an issuer, an audience and a key set, verifies `iss`, `aud`,
`exp`/`nbf` and an asymmetric signature, and issues, stores and mints nothing. There is no
trusted-proxy mode and none is coming — on any bind Loom can have, *this header came from the proxy*
cannot be told from *this header came from a client*. What a checked caller buys is two things: the
edit log records an issuer-qualified `principal` **beside** `mcp.actor` (one is true about a
deployment, the other about a caller, and both are true at once), and a policy may finally name one:

```yaml
mcp:
  auth: { issuer: ..., audience: ..., jwks_uri: ..., claims: { groups: "string[]" } }
governance:
  policies:
    - { name: own-orders, objectType: Order, rows: "object.customerId == principal.sub" }
    - { name: gold-desk, objectType: Customer, when: "principal.groups contains 'gold-desk'",
        rows: "object.tier == 'gold'" }
```

**Half a policy may name a caller.** `rows:` may be conditioned; `mask:` may not, because a mask
announces itself in the tool description and the `filter` schema, and a per-caller announcement makes
the tool set a function of the caller rather than of the spec. Serving two audiences different
*columns* is still two deployments. A claim is **declared** in `loom.yaml`, beside the issuer that
mints it, so a policy naming a caller is checked exactly as one naming a property is — and
`principal.` stays refused in an ontology, which is what keeps a spec deployment-blind. The caller
never reaches the resolver: a principal is constant for the duration of a call, so a policy is
decided *above* it and what reaches every enforcement site is a predicate naming nobody. A surface
that cannot attest anybody — `loom query`, `loom run`, a spawned stdio server — **refuses** such a
config rather than applying the rest of it, because policies subtract and never add, and skipping the
conditional ones would make the dev command the way to read what the served surface withholds.

**M7 is the query an agent actually wants.** A `search_` filter was equality and `searchable`
substring, so a date range — the whole point of a precomputed daily table — could not be asked for
at all. Now each property advertises the operators its *type* deserves, ANDed, in either spelling:

```jsonc
{"filter": {"salesDate": {"gte": "2026-01-01", "lt": "2026-02-01"}, "tier": "gold"}}
```

The bare spelling keeps exactly what it meant, because rewriting it as equality would return fewer
rows to every filter already written, with nothing raising. `searchable` still decides *which*
properties are filterable — making every property queryable would widen a surface no spec asked to
widen — and gives up only its invisible second job, since substring is now an operator you can see.
A caller's comparison is the same IR node a policy's is, so `eq` on a null column cannot mean one
thing for a caller and another for a deployment; what keeps a governance predicate un-advisory was
never the node but the field it hangs on, and one function now decides what may become a pushdown
hint. Two refusals: a bare `{"ltv": null}` filter, because JSON cannot tell a field an agent left
blank from one it meant as null and the old answer was a plausible non-empty result set; and a
capability flag for ranges, because no adapter could ever fail it.

**M8 opens with `in` — the query that cost N calls.**

```jsonc
{"filter": {"tier": {"in": ["gold", "platinum"]}}}
```

It shipped ahead of `or` because it is not one. This roadmap had all three of `or` / `in` / `not`
waiting on the same thing — a filter list that becomes a tree — and that is true of a disjunction of
*predicates*, not of a disjunction of *values*: `in` is one node in the conjunction that was already
there. It abbreviates `eq` and therefore inherits `eq`'s null, which SQL's own `IN` does not — that
one never matches a null element and answers unknown for a null column, so a one-element list and the
`eq` it stands for would select different rows on exactly the tables where it matters and no others.
`{"in": []}` is refused for M7's reason read backwards: the empty list's honest answer is
indistinguishable from a search that found nothing. It demands no new engine capability, and it is
the one filter that yields no pushdown hint on *correctness* grounds rather than for want of a
spelling — the hints are ANDed, so one per value would prune to the rows matching every value at once.

---

The grammar all of this compiles from is [`spec-v0.md`](./spec-v0.md). What is next is
[`ROADMAP.md`](./ROADMAP.md).
