[← Spec index](../spec-v0.md)

# 7. What the grammar compiles to (deterministic)

So the contract is complete on both ends, here's the fixed mapping from spec → generated MCP
surface. Nothing here is hand-authored.

| Spec element                         | Generated MCP tool(s)                                   | Input schema source |
|--------------------------------------|---------------------------------------------------------|---------------------|
| `objectType Customer`                | `get_customer(key)`                                     | PK type |
|                                      | `search_customer(filter, page)`                         | `searchable` props + property types → §7.1 |
|                                      | `list_customer(page)`                                   | pagination |
|                                      | `match_customer(text, filter, via, page)` — only with `semantic:` **and** `mcp.embedding` | free text + the same `filter` → §7.2; `via` from the declared links |
| `linkType placedBy` (+ `reverseName`)| contributes `order` / `customer` directions to `traverse(object, link, direction)`, and a `via` key to the `match_` of each end | link mapping |
| `action upgradeTier`                 | `run_upgrade_tier(parameters, dryRun)`                  | `parameters` → JSON Schema; `description` → tool description |

Tool names are the api name in `snake_case`, for every row of that table. This one used to read
`run_upgradeTier`, which no other row's spelling would have produced — a slip, corrected, in the
same class as the two §4/§5 bugs the action runtime turned up.

**And an `ingest:` entry generates nothing, which is why it is not in the spec.** It is the one
declared, named, runnable thing in Loom with no row in this table — a `loom ingest` command and
nothing else. That is enforced by where it is written rather than by a rule about what to skip: this
table maps the *spec* to a tool set, `ingest:` lives in `loom.yaml`, and a surface assembled from the
spec therefore cannot reach it. See §6.2.

**One tool per action, not one `run(action, params)`.** `traverse` is generic and an action is not,
and the rule that decides both is about the *schema* rather than the name: a generic tool is right
exactly when the varying element does not change the input schema. Every link takes the same
`(objectType, key, link, page)`; every action takes something different — an objectRef and a
two-value enum here, a string and a `decimal(12,2)` there. Collapsing them means typing `params` as
a free-form object, which would be the one place in this table where an agent is handed an untyped
bag and "declared types are honored on the way in" stops being structural. The cost is stated rather
than hidden: a spec with forty actions generates forty tools.

**Two argument namespaces, which never mix.** Names drawn from the spec's vocabulary go inside a
nested object — `search_`'s declared property filters under `filter`, `run_`'s declared parameters
under `parameters`. Names Loom chose stay at the top: `key`, `limit`, `offset`, `objectType`,
`link`, `dryRun`. That is what makes `dryRun` addable at all, because an ontology may declare a
parameter called `dryRun` and it can no more be shadowed than a property called `limit` can.

**Typed filters put Loom's words below a spec name, and the rule survives being restated.** It was
never "Loom's vocabulary appears once": it is that **each level of the argument tree belongs entirely
to one vocabulary, and they alternate.** Top level Loom's, `filter` the spec's, and — since §7.1 —
one more level of Loom's inside each property. A property name never appears where an operator does,
so nothing shadows anything and a spec may declare a property called `gte`.

**`dryRun` is an inspection verb, not an approval step.** It runs bind → read → validate and stops
before the write, returning `previewed` — the same thing `loom run` prints above its `y/N`, and the
only way an agent can learn what an action would do other than by doing it. It confers nothing on
the run after it: no state is carried, no row is held, and the next run does its own read and
asserts that one (§4.1). A design where a preview reserved anything for a later call is the one
§4.1 rejected when it put the prompt outside the window.

**Which actions become tools, and whether any do.** All of them, whatever their `status` — a
non-`active` element is labelled in its description (`DEPRECATED — …`) rather than hidden, because
hiding it would leave `loom run` able to run something the tool surface denies, and the runtime is
deliberately one entry point for both. Whether the write half is generated at all is a *deployment*
decision, not a spec one: `mcp.writes` (§6) is off by default, so declaring an action does not by
itself make a lake mutable by an MCP client.

**What a run returns.** `ActionResult` (§4.1) serialized — status, key, before/after, snapshot,
attempts, `editId`, and typed `failures[]`. Never a protocol-level error: the transport's error flag
answers *did this call become a run*, not *did the run succeed*, so a refused precondition, a
conflict and a write failure all arrive as content an agent branches on. That is also the only
encoding that can describe `applied` with a `log_failed` beside it, which a boolean gets backwards.

**Nothing in this table differs by transport, and that is stated rather than implied.** The surface
is a function of the spec; `mcp.transport` is not one of its inputs. Both transports are handed the
same assembled server, so there is no seam here for one of them to widen — a tool that exists over
HTTP exists over stdio, with the same name, the same input schema and the same description.

The corollary matters more than the claim, because **a transport with real status codes invites
re-litigating `isError`**. It does not get to. An HTTP status answers *did this exchange happen*;
`isError` answers *did this call become a run*. They are questions at different layers and never two
votes on one thing, so **an HTTP status never disagrees with `isError`**: every tool outcome —
`applied`, `previewed`, `refused`, `failed`, an unknown link, an unknown tool — is a `200` carrying
content. A non-`200` is only ever about the exchange (a rejected `Host`, a rejected `Origin`, a
malformed body, an unknown session), and no tool result can produce one. The alternative maps a
refusal onto a 4xx, where an agent's transport raises before its own branch on `status` ever runs,
and a validation rule doing its job arrives looking like a broken client.

Two invariants the compiler guarantees: **the LLM never receives raw SQL** (only these
verbs), and **governance policies filter both the direct API and the MCP tools identically**
(enforced in the resolver, below the MCP layer — and on the write plane in the action runtime's own
projection, since `dryRun` would otherwise read out of `before` what a policy withheld from a read).

The first invariant is a claim about the **tool set**, and §6.1's policies are the one thing that
changes what a tool *returns* without changing what it is. The line between those is worth stating
where both are: the set of tools, their names and their argument namespaces are a function of the
spec; a policy can **subtract** from what one advertises and returns — a withheld property leaves
the projection, the `filter` schema and the result — and can never add to any of them. That is the
same direction the engine may not push in: §6 refuses a spec an engine cannot serve rather than
narrowing the surface to fit, because an engine is an implementation detail, while a policy is the
deployment's declared intent about its own data.

## 7.1 The filter grammar — what `search_<type>` takes

```yaml
filter:
  tier: gold                                    # a bare value — the v0 spelling
  salesDate: { gte: '2026-01-01', lt: '2026-02-01' }   # comparisons, ANDed
  status: { in: [open, pending] }               # membership — a disjunction of values
```

Operators are generated from the **property type** and nothing else:

| type | operators |
|---|---|
| `string` | `eq` `ne` `in` `gt` `gte` `lt` `lte`, and `contains` when the property is `searchable` |
| `int` `long` `double` `decimal` `date` `timestamp` | `eq` `ne` `in` `gt` `gte` `lt` `lte` |
| `enum` `boolean` `objectRef` | `eq` `ne` `in` |

`in` is offered wherever `eq` is, and gated by nothing, because it **is** `eq`: whatever a type can
be compared to for equality it can be compared to twice. It demands no engine capability for the
same reason — §6's rule is that a requirement is something a spec can demand and an engine can fail,
and no dialect that can say `WHERE c = ?` cannot say `WHERE c IN (?, ?)`.

An `enum` is not ordered: its `values` are a declared set and their order in the file is a list, so
`tier > 'bronze'` would answer with the engine's collation rather than with anything the spec said.
An `objectRef` travels as a key, and keys are equal or not.

**A bare value is type-directed sugar** — `contains` for a `searchable` string, `eq` for everything
else — which is exactly what it meant in v0. It is kept because rewriting it as a plain `eq` would
return *fewer* rows to every filter already written against a searchable string, with nothing
raising: a silent narrowing, which §6 already calls worse than a refusal.

**Composition is `AND`**, between operators on one property and between properties.

**`in` is the one disjunction, and it is not the `or` this grammar still lacks.** An earlier draft
of this section said `in` was "sugar over an `or` that does not exist yet" and therefore had to wait
for one; that was wrong, and the difference is what a disjunction is *over*. An `or` composes
**predicates** and needs the filter list to become a tree. `in` disjoins **values**, against one
column, all of them constants — one node in a list that is already a conjunction. `or` and `not`
stay deferred, and `not` is the one that costs, because it reopens the no-negation argument below.

**Null is a value you can test and not one you can order, and a bare `null` is refused.** `{"ltv":
{"eq": null}}` selects the rows where `ltv` is null (§5.2, unchanged, and the same
`IS NOT DISTINCT FROM` lowering a policy's `==` gets). `{"ltv": {"gte": null}}` is refused, because
it is undecided for every row. And `{"ltv": null}` — a bare null — is **refused permanently**: JSON
cannot distinguish a field a caller left blank from one it meant as null, and an agent emitting null
for a value it did not have is the likeliest way this argument is ever malformed. v0 answered it as
`IS NULL`, which is a plausible non-empty result set for a question nobody asked. The generated
schema says all of this: the equality operators admit a `null` and the four ordering ones do not.

**`in` inherits every one of those answers, because it abbreviates `eq`.** An element may be null
and means there what it means to `eq`, so `{"tier": {"in": [null]}}` selects the rows
`{"tier": {"eq": null}}` selects — *not* what SQL's own `IN` would say, which never matches a null
element and answers unknown for a null column. An abbreviation that selected different rows than the
thing it abbreviates would be a trap, and an invisible one: the two agree on every table with no
nulls in the filtered column.

**`{"in": []}` is refused**, which is the same argument as the bare null reached from the other end.
An empty list has an honest answer — no rows — and returning it is precisely what makes it a
refusal: a caller cannot tell "your list was empty" from "nothing matched", so an agent whose
candidate set collapsed to nothing would be told, in the vocabulary of a result, that its question
was answered. `minItems: 1` in the generated schema is that refusal announced rather than only
enforced. The list itself is never null — `{"in": null}` is refused as a shape, not as a value.

**An ordering comparison over a null column does not return the row.** SQL's three-valued answer and
§6.1's *admitted only on true* agree here rather than by coincidence: this grammar has no negation,
which is the only place the two can differ.

**A JSON object or array where a value goes is refused**, which is the third refusal and the one
that arrives from the empty side. A scalar is *coerced* rather than type-checked — `"100"` reads as
a double, `42` reads as `"42"` for a substring match — and the bottom of that path is `str(value)`,
which obliges a container with a language-level repr like `"{'deep': 1}"`. No row holds one, so
`{"name": {"eq": {"deep": 1}}}` answered `0 rows` with no error: a malformed argument dressed as a
result, exactly what the bare null is refused for.

**The advertised `filter` properties are exactly the ones accepted**, and that is enforcement rather
than presentation. The schema lists `searchable` minus whatever a policy withholds (§7); a filter on
any other property is refused — a declared-but-not-searchable one by §2 rule 6, a withheld one as
the oracle §6.1 refuses. This is written down because it was once only advertised: the surface built
its schema from `searchable` while the resolver accepted any declared property, so `filter`'s
`additionalProperties: false` was a claim nothing made true, and a caller who ignored the schema
could range-query a column the deployment never offered.

**`limit` above the maximum is refused, not clamped.** Both bounds are enforced the same way, so the
`limit` a page envelope reports is always the page that was served. Clamping reads as generosity and
is a silent narrowing: the caller gets `MAX` rows, the envelope echoes the number it asked for, and
a client paging with `offset += limit` steps past everything it was not given.

## 7.2 `match_<type>` — ranking, and why it is not an operator in §7.1

```
match_ticket(text: "the customer wanted their money back",
             filter: { queue: logistics },        # the same grammar §7.1 defines
             via:    { handledBy: { owner: ada } },  # …read against the type at the far end
             limit: 10, offset: 0)
```

`search_<type>` finds rows that **say** a word. An agent asking *which tickets were a payment
dispute?* gets nothing for "sent the money back", "chargeback", "customer wanted out" — the answer is
in the text and the caller's words are not the data's words.

**It is a tool rather than an operator**, and the reason is the same one that makes `traverse`
generic and `run_` per action: a rule about what the thing *is*, not about its name. Every filter in
§7.1 is a **predicate** — it decides each row true or false, `order_by` stays pinned to the primary
key, and composition is `AND`. A similarity clause decides nothing. It **ranks**. Putting one under
`filter` would introduce `k` and an ordering into a grammar that has neither, and
`{similar} AND {tier: gold}` would have two different answers — rank-then-filter and
filter-then-rank — with nothing in the grammar to choose between them. `search` is also a word
already spent on rows.

**`filter` narrows before the ranking, always.** That is a rule and not a per-query decision:
choosing by estimated selectivity is a query planner, and Loom does not have one. So a filtered call
ranks fewer rows rather than re-ranking the ones a filter kept, and a governed row is not ranked low
— under a §6.1 `rows:` predicate it *does not exist* for that caller, because the predicate rides on
the table at the point a type becomes one.

**The tool needs both halves.** The spec declares `semantic:` and the deployment configures
`mcp.embedding` (§6.3); with no provider there is no tool, exactly as `mcp.writes: false` exposes no
action. That is not a policy narrowing a surface: §6.1 refuses a `mask:` over a semantic property
before the deployment starts, because no deployment gets to be the one that makes a tool disappear.

### `via` — narrowing by a linked object

Ranking by meaning and being unable to say *belonging to a gold-tier customer* is not most of the
feature; it is the half nobody can use. `via` is keyed by **link name**, one key per declared link
out of the ranked type (both directions, as `traverse` sees them), and each value is that far type's
own §7.1 filter object:

```
via: { handledBy: { owner: ada }, tags: { label: { contains: "vip" } } }
```

**A top-level argument rather than a dotted key inside `filter`.** §7's namespace rule is one
namespace per level of the argument tree, and the spec has *two* vocabularies here — link names and
property names. A `handledBy.owner` key inside `filter` would put both on one level, so an ontology
with a link and a property of the same name would have a surface that could not say which was meant.

**Existential, always.** A hop keeps a near row when **at least one** object on the far end matches.
On a to-one link that reading is invisible; on a to-many link it is a choice, and the other reading —
*every* linked object matches — is a quantifier this grammar has no spelling for and `traverse`
answers by handing you the rows. An empty `{}` is therefore an existence test rather than a no-op:
*tickets that are in a queue at all*.

**Each near row comes back once.** A hop is a semi-join and not a join, which matters exactly where
it is invisible: joining would return the near object once per far row that matched — same object,
same score — and the page would be smaller than the number on it. Deduplicating afterwards would
work only while the projection is unique, and a §6.1 mask can remove the primary key from it.

**Both ends are governed, and the far end's rows are the rows this deployment shows.** A `rows:`
predicate on the far type withholds through the hop: a linked object you may not see is not a link
you may follow, so `via: { handledBy: {} }` finds nothing through it. A `mask:` on a far property
removes it from that hop's advertised schema and refuses a filter naming it — the same oracle
refusal §6.1 makes for a filter on the ranked type, one join out, since a hop can binary-search a
withheld value exactly as a filter can.

**It narrows before the ranking**, with `filter` and with the other hops, all ANDed. One hop per
link name; several links are several keys.

`search_<type>` has no `via` and does not need one — it can already ask the question from the other
end by traversing. The fragment exists on the ranked read because a ranking cannot be composed that
way: a rank is over the rows that survive narrowing, and there is no *second* call that would
recover the answer.

**What comes back.**

```json
{ "objectType": "Ticket", "property": "body", "model": "BAAI/bge-small-en-v1.5",
  "embeddedAsOf": "2026-08-23T09:14:02Z",
  "count": 2, "limit": 10, "offset": 0, "hasMore": false, "masked": [],
  "matches": [ { "score": 0.83, "object": { "ticketId": "t1", "…": "…" } } ] }
```

`matches` rather than `objects`, because the elements are not objects — each is a score paired with
one. The score sits **beside** the object and never inside it, which is §7's namespace rule reaching
the result: the object is the spec's vocabulary, `score` is Loom's, and an ontology may declare a
property called `score`. It is a cosine similarity, comparable between rows and between calls of one
deployment and meaningless against a different model — which is why `model` is in every envelope.

Pages are pages: the order is total (score descending, then primary key), so `offset` means what it
means everywhere else and `hasMore` is computed the same way.

**Only embedded rows can be ranked, and the envelope does not count the ones that cannot.** A vector
is derived by `loom embed` (§9) rather than at query time, so a row written since the last reconcile
has none and is simply absent. The count of them needs an anti-join on every call, and the deciding
reason not to report it is that an agent cannot *act* on it — it can neither wait nor trigger a
reconcile, and this surface says things a caller can do something about. The cost is stated rather
than hidden: **`match_` can silently omit a row that exists.** `embeddedAsOf` — the oldest stamp
among the rows returned — is what the caller gets instead, and `loom embed` and the serve banner are
where an operator gets the rest.

**Cost, in two halves that are different sizes.** No vector index: pre-filtering means the distance
is brute-forced over the survivors anyway, and an ANN index is an optimisation for the *unfiltered*
case. So the **arithmetic** is linear in the filtered set, and `filter` is the lever.

The **I/O** is not. The keys that survive a filter are known only after the object side is read, and
the pushdown channel carries a conjunction of equality pairs — a key set has no spelling in it, the
way a range has none — so the vector column is materialized whole on every call, filtered or not.
That is the floor, it grows with the embedded rows rather than with the answer, and the serve banner
says both halves rather than the flattering one.

A `via` hop adds one subquery over the far table per link named, which narrows the *arithmetic* the
way `filter` does and does not move the floor.

---

[← §6 Project config — `loom.yaml`](./06-project-config.md) · [§8 Worked example →](./08-worked-example.md)
