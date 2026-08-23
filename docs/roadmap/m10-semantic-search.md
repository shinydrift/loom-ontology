[← Roadmap index](../ROADMAP.md)

# 🔨 M10: Semantic search — a column searched by meaning

*Goal: the question `contains` cannot be asked.*

`search_<type>` finds rows that **say** a word. An agent asked *which orders had a payment dispute?*
gets nothing for "sent the money back", "chargeback", "customer wanted out" — the answer is in the
text and the caller's words are not the data's words. Every other lever in the surface is exact by
construction, which is why this needs a plane of its own rather than another operator: §7.1's
filters are **predicates**, each deciding a row true or false with `order_by` pinned to the primary
key, and a similarity clause decides nothing. It **ranks**. Putting one in `filter:` would introduce
`k` and an ordering into a grammar that has neither, and `{similar} AND {tier: gold}` would have two
different answers — rank-then-filter and filter-then-rank — with nothing in the grammar to choose
between them.

That is the same shape as M8's correction, arrived at before the mistake instead of after: right
about the word (*it filters the result set*), wrong about the shape.

## The four slices

- [x] **1 — the grammar, and every refusal it owes.** `semantic:` in the loader, `mcp.embedding` in
      the config, `vector_search` in `NEGOTIATED`, the fifth mask refusal. No vector, no table, no
      tool. Grammar before plane, the way M5 went.
- [x] **2 — `EmbeddingProvider`, the sidecar, and `loom embed`.** Loom's first model dependency, and
      where staleness is defined. Also the seventh port, and the first `list<float>` column Loom has
      ever created.
- [x] **3 — `match_<object>`.** The tool, the brute-force lowering, the result envelope. Also the
      **fourth source node** — the read IR was three shapes for nine milestones.
- [ ] **4 — `via`.** Cross-object filtering, without which the interesting queries are not
      expressible. **M10 closes here, and there is no partial ship**: slices 1–3 generate a tool
      that can rank orders by meaning and cannot say *belonging to a gold-tier customer*, which is
      the query anyone actually has.

## Decided before any of it was built

- **A tool per object type, not an operator in the filter grammar** — see above. `match_<object>`,
  because `search` is a word already spent on rows; the same discipline as `filters.py`'s note that
  `contains` is spent.

- **One semantic property per type, and the key is a *name* rather than a list.** `primaryKey` and
  `title` are the precedent: a list is what `searchable` is because it is genuinely many. Refusing
  a two-element list would be a rule somebody has to be told about, for a spec nobody can write.
  Going plural later widens the key to accept both, which is additive.

- **Only a `string` may be embedded**, which is *narrower* than `searchable` and reverses M7's
  direction deliberately. M7 widened `searchable` to every scalar because every scalar has
  comparisons worth offering; the opposite holds here. An ordered type already has an order, so
  `gte` says exactly what a similarity score would approximate, and an `enum` is a closed set that
  `eq`/`in` answer exactly. Embedding either buys a fuzzy answer to a question with a precise one.

- **`vector_search` is negotiated — the first flag this module's rule has let through.** Three were
  refused before it and all three for one reason: nothing could fail them. `range_comparisons` was a
  floor because every dialect that can say `WHERE c = ?` can say `WHERE c >= ?`. There is no
  comparable implication for vector distance: ranking needs a fixed-width array type and arithmetic
  over it, which a dialect can be a complete SQL engine without. Both halves of the test hold — a
  spec demands it by declaring `semantic:`, an adapter fails it by not having array math.

  It is demanded by the **spec**, not by the deployment that configures a provider. An ontology
  whose engine has no array arithmetic describes a surface that engine could never serve, and
  finding that out only in the deployments that switch embedding on would make the refusal a
  property of a config file rather than of the pairing `negotiate.py` exists to check.

- **`Capabilities.vector_search` defaults `false`**, unlike the three above it, and the asymmetry is
  what a default *asserts*. Those three are floors, so defaulting them true describes almost every
  adapter correctly. This one is not implied by being able to filter, so an adapter claims it or it
  does not have it — and a fourth adapter that says nothing is described correctly rather than
  optimistically.

- **The spec declares intent, the deployment declares mechanism.** `semantic: notes` is true of the
  model wherever it runs; `provider`/`model` is true of one deployment. A spec can no more demand
  `text-embedding-3` than it can demand a transport.

- **Absent `mcp.embedding` withholds a tool; it does not refuse to start.** The distinction from
  `check_capabilities` is worth stating because the two look alike. Negotiation asks *could this
  engine ever serve what this spec describes* — a spec's own claim, so a mismatch is a contradiction.
  This asks *does this deployment switch it on*, and a deployment configuring no provider is not
  describing a contradiction; it is one of the deployments that reads without embedding, exactly as
  `writes: false` serves without actions.

- **`model` is required and `dims` is not a key.** Both are the same failure avoided twice. The model
  is folded into every stored vector's hash, so a *default* Loom could change in a later release
  would silently invalidate every vector in every warehouse that took it. And `dims` is a property
  of the model, so declaring it beside the model name is a chance to declare it wrong — vectors of
  the declared width get written, ranked against each other, and mean nothing. Neither failure is an
  error; both are a ranking that quietly stops meaning anything, which is why the answer is to not
  let the file say it.

- **`provider: local` by default.** No row's text leaves the machine unless a deployment says so —
  the loopback-bind posture, applied to a different wire. The provider set is enumerated rather than
  free-form so the places a lake's text can be sent to are something `loom.yaml` lists.

## The fifth thing a mask cannot withhold

A mask over the semantic property is refused at bind time, beside the other four. It is the
combination shape the action refusal already has — the spec is fine, the policy is fine, and their
deployment together cannot stand — and it sharpens an argument `governance.py` already makes.
Filtering on a masked property was refused because a caller who can filter on a withheld value
binary-searches it a bit at a time. A **ranking** hands back how *near* each row came, so the same
probe returns a gradient rather than a bit and converges faster than the search it replaces.

The reason it is not simply *withhold the tool as well* is §7: a tool is derived from the spec, and
no deployment gets to be the one that makes one disappear.

## Settled for the slices that have not been built

Recorded here because each was decided against a real alternative, and a decision nobody wrote down
gets re-litigated by whoever builds it.

- **Vectors live in a Loom-managed sidecar, one table per object type**, under `_loom_meta` beside
  `applied`, `edits` and `loads`. Not a column in the object's own table, for three reasons that
  compound: `ALL_KINDS` has no `array`, so it cannot be a declared property at all until complex
  types land; as an *unmanaged* column it would make `loom plan` report Loom's own data as somebody
  else's, permanently; and `ActionRuntime._read` carries unmapped columns across a modify, so a
  `run_` that changes the embedded text would write the **old vector back beside it in the same
  commit** — internally consistent by construction, with nothing to compare, which is the one kind
  of staleness that cannot be detected. Per type rather than one global table because the key is a
  *join* column and a string-encoded primary key would need a cast on every call.

  This is the migrate layer's posture applied one level down: *this table is not mine*.

- **`source_hash` covers the model, not just the text.** `hash(text ‖ model ‖ dims)`, so changing
  provider invalidates everything by construction rather than by anyone remembering to.

- **The sidecar holds only facts about the row it is keyed to.** No source text — that is a governed
  copy outside the table governance is written against, and `forgetCustomer` would gain a second
  place to reach. No denormalised link columns — they optimise the join, but the cost of a ranked
  query is the distance computation over the survivors, and they buy a staleness axis `source_hash`
  structurally cannot see (one customer changing tier invalidates the vector row of every order they
  ever placed) plus a governance hole (a denormalised column is not an `ir.TableRef`, so no policy
  rides on it). One line has now decided three questions.

- **`loom embed` is the mechanism; inline is not built in v1.** "Automatic" means automatic
  *derivation* — you never hand Loom a vector — not automatic *timing*. Embedding at query time
  calls a model on every call; at serve time it is a boot that fans out N of them. And M9 is why
  the reconcile cannot be optional even if inline existed: `loom ingest` writes four million rows
  without passing the action runtime, so the write path a `run_`-time hook covers is the minority
  of writes by a wide margin.

- **Filtering is part of retrieval, and pre-filtering is a rule rather than a heuristic.** Choosing
  per query by estimated selectivity is a query planner, and Loom does not have one. `pushdown_hints`
  is the precedent: the hint is advisory and the `WHERE` is re-applied regardless, because an
  optimisation is never load-bearing for correctness here.

  For a governance predicate the question does not arise at all — a governed row is not filtered out
  of the ranking, it *does not exist* for that caller, because the predicate rides on `ir.TableRef`
  at the point a type becomes a table. Which also means `via` inherits cross-object governance for
  free: `_table` already governs both ends of a traverse.

- **No vector index in v1, and the row counts are why.** Pre-filtering means brute-forcing distances
  over the survivors anyway, and at 10⁵ rows × ~10³ dimensions that is ~10⁸ multiply-adds — tens of
  milliseconds in a vectorised engine. `array_cosine_similarity` is core DuckDB, so this needs no
  extension; `vss` buys an HNSW index, which is an optimisation for the **unfiltered** case. The
  pleasant symmetry: the hard case for an index is the one that does not need it.

  The cost belongs in the banner beside the existing note about a slow query blocking the server:
  `match_` is linear in the filtered set.

- **The envelope carries `embeddedAsOf` and not a count of unembedded rows.** *(Built: the field is
  there and the count is not, and what changed is which set the stamp is taken over — see "Decided
  while slice 3 was built".)* The count needs an
  anti-join over the admitted set on every call, but the deciding reason is that an agent cannot
  *act* on it — it cannot wait and it cannot trigger a reconcile, and this surface says things a
  caller can do something about. What that gives up is real and stated: `match_` can silently omit a
  row that exists, so the honesty moves from the caller to the operator, and the reconcile has to be
  reliable rather than best-effort. The count goes to `loom embed`'s output and the banner.

- **A model change refuses and names the flag** — `--remodel`. Deliberately unlike `loom apply`,
  which refuses a breaking plan with *no* force flag: there no safe version of the operation exists,
  and here it is merely expensive and reversible.

  **Built differently from the plan, and the plan was the weaker version.** This said a swap is
  recognised by *every hash mismatching at once*, which is true and is a heuristic — and it misfires
  exactly where it is least tolerable, on a small table whose rows all legitimately changed between
  reconciles. The model that produced a vector is a fact about that vector, so by the sidecar's own
  "only facts about the row it is keyed to" rule it belongs in the table; a dictionary-encoded string
  per row turns the inference into the fact. `source_hash` still folds the model in, so invalidation
  stays by construction. What changed is that the *refusal* is now exact rather than probabilistic.

  A consequence worth stating, because the flag's name suggests otherwise: `--remodel` **permits** a
  re-embed and does not cause one. Against an unchanged model the hashes still match and the honest
  amount of work is none.

## Decided while slice 2 was built

Everything above was decided before the code existed. These came out of writing it, and each is
recorded because it was a real choice rather than the only option:

- **`VectorWriter` is the seventh port, and the first that opens a plane rather than re-cutting one.**
  The two logs write *records*: append-only, never read back, permanently without a delete verb
  because an expired record and a lost one are the same sight. A vector is not a record — it
  describes a row that exists now, it goes stale, and keeping it correct needs exactly the upsert and
  the delete the log ports refuse. The alternative was a `BulkWriter`, which fails on the property
  `_loom_meta` has always had: it takes a table name, so a runtime holding one to maintain a sidecar
  could point it at the ontology's own tables.

  The port therefore keeps *the table is not an argument* while writing many tables — its verbs take
  an **object type** and derive the name themselves. `vector_table()` is the only function that
  produces one.

- **`list<float>` is creatable without being in `ALL_KINDS`, and that gap is the point.** No property
  may declare it, because a spec that could say `type: list<float>` is a spec that can hand Loom a
  vector — the thing `semantic:` exists not to be. The type system stays what an *ontology* can say;
  `iceberg_type()` becomes what *Loom* can create. This milestone is the first time those differ.
  Nested field ids are allocated past the last column, since Iceberg numbers them out of the same
  space and a collision is a corrupt schema rather than an error.

- **The prune commits before the merge.** An orphan is text that outlived the row it described, so a
  run that fails after the merge should still have removed it. The reverse ordering makes the one
  operation with a deadline behind it the one most likely to be skipped.

- **A reconcile commits per batch, and is resumable rather than atomic.** What needs embedding is
  recomputed from hashes every run, never tracked in a cursor, so a failure halfway leaves the
  batches it committed and the rest for next time. One commit for the whole run would buy atomicity
  with a gigabyte of Python floats held in memory — paid to make a failure *less* resumable.

- **A dry run calls the model exactly once.** `dims` is folded into every `source_hash`, so a preview
  that guessed it would report on hashes the real run will not compute — it would preview a different
  reconcile. One probe string is the smallest honest version of the command.

- **Blank text is the absence of text, not staleness.** A row whose semantic property is null or
  empty gets no vector and is counted apart, because a reconcile that called it pending would embed
  nothing and never converge. It also means the orphan set keys on *rows with text*: text that is
  blanked leaves a vector behind exactly as a deleted row does.

- **`--type` is a flag, not a leading positional.** Every other command puts its subject first; this
  one has no required subject, so two optional positionals would make `loom embed ontology` ambiguous
  with `loom embed Customer`, resolvable only by guessing which names a directory.

- **`provider: local` is fastembed**, which is a smaller model than the obvious alternative and the
  cost is stated: sentence-transformers is the quality baseline and arrives with torch, so the
  *default* provider would pull over a gigabyte. A default that expensive is one people route around.

## Decided while slice 3 was built

Everything above slice 2's section was decided before the code existed. These came out of writing the
ranked read, and each is recorded because it was a real choice rather than the only option:

- **`ir.Match` is the fourth source node, and the read IR had been three shapes since v0.** Worth
  saying why it could not be anything smaller. A `Search` with one more filter was the tempting
  version and it is the same mistake the milestone opened by refusing, one layer down: the other
  three nodes *decide* rows, and this one imposes an ordering the caller did not give. A node is
  what makes the choice structural — the filters stay the flat conjunction they always were, and the
  ranking happens over what survives them, with no way to spell the other order.

  The sidecar rides on the node as a `VectorRef` beside the governed `TableRef`, which is
  `ThroughRef`'s shape and `ThroughRef`'s answer to governance: it stands for no object type, so no
  policy names it, and there is no field on it to put one in.

- **The comparability guard turned out to be load-bearing for *liveness*, not only for meaning.**
  The plan said a vector from another model should not be ranked, which is true and sounded like a
  quality argument. It is stronger than that: `array_cosine_similarity` over two different widths
  **raises** in DuckDB rather than answering, so a sidecar caught part-way through a `--remodel`
  would make every `match_` fail rather than rank the generation that is current. `WHERE model = ?
  AND dims = ? AND property = ?` is therefore in the compiled query for correctness *and* in the
  scan as the equality pairs that channel can carry.

  **All three columns slice 2 wrote are read by it, and the third was found in review.** `property`
  looked ornamental — slice 2 called it "single-valued today, and present anyway" — and it closes
  the narrowest window in the milestone. Re-point `semantic:` from one column to another and every
  `source_hash` changes, so a reconcile fixes it; between the deploy and the reconcile the sidecar
  holds vectors of the *old* text, and without the clause they would be ranked, silently, under an
  envelope naming the new property and a fresh `embeddedAsOf`. That is the shape `loom.managed` had
  and this milestone avoided: a column written and never read is a check nobody is doing.

  The consequence is named rather than hidden: after a model swap `match_` returns **nothing** until
  `loom embed --remodel` runs. That is a loud failure rather than a quiet wrong one, and the envelope
  names the model on every call so an empty page is diagnosable.

- **`embeddedAsOf` is the oldest stamp among the rows returned, not across the whole sidecar — and
  the plan was the weaker version.** `embed.store` predicted slice 3 would consume its sidecar-wide
  reading. Three things are wrong with that: an envelope describes the answer it is attached to, and
  the sidecar-wide number is a fact about rows the caller was never shown; computing it per call
  needs a scan of a column nothing in the answer is keyed to, which is the same per-call extra read
  this milestone refused for the count of unembedded rows; and getting it would put a catalog handle
  in the layer that composes envelopes. The definition stays in one place — `store.oldest` — and each
  caller hands it its own set. `loom embed` still reports the sidecar-wide reading, because that one
  is the *operator's* question.

- **`limit`/`offset`, not `k`.** A ranked page is still a page. The order is total — score
  descending, then the primary key — which is `Search.order_by`'s argument arriving by a different
  route: without the tie-break, two rows at the same distance would swap between calls and page 2
  would be an unrelated draw rather than the continuation of page 1. With it, `offset` means what it
  means everywhere else and `hasMore` is computed by the same function, so the surface has one
  paging vocabulary rather than one plus an exception.

- **The score sits beside the object, and the output names are made unique rather than reserved.**
  §7's namespace rule reaching the result: the object is the spec's vocabulary and `score` is Loom's,
  so one is never inside the other. Below that, in the one place they briefly share a namespace — the
  engine hands rows back as a dict keyed by output name — a spec that declared a property called
  `_loom_score` would not produce an error, it would produce two columns silently becoming one. The
  resolver lengthens its own name until it is not one the projection uses, which is three lines and
  removes the failure rather than documenting it.

- **Both of the ranked plane's own refusals are refusals rather than empty results**, which is
  `{"in": []}`'s argument met one plane over. Blank `text`, and a type whose sidecar has never been
  written: in each case the honest empty answer is one a caller cannot tell from *nothing was
  similar*, so it is a sentence naming what to do instead. The unembedded case is an ordinary state
  of an ordinary deployment — a spec may declare `semantic:` and be served before any reconcile has
  run — so it names `loom embed --type X` rather than surfacing a catalog error.

- **`Matcher` is a third binding, beside reads and writes, and the resolver takes a vector.** The two
  things a ranked read needs are the two things `Resolver` deliberately does not have: something that
  can call a model, and a handle on the lake. Keeping `Resolver.match(vector, model, …)` a pure
  function of the ontology and its arguments is what stops a plan from being built around a network
  round trip, and a vector is a *value* — like a filter's literal — so nothing a caller sends becomes
  a predicate, a column or a table.

  It also pays off a sentence slice 2 wrote in advance: `bind_matching` builds each `VectorStore`
  **without** a `VectorWriter`, so a serving process can rank a sidecar and cannot maintain one.
  `vector_writer_for()` is never called on the read plane, which keeps `build_server`'s claim about
  what a serving process holds true across the one port that can delete.

- **"Linear in the filtered set" was true of the arithmetic and not of the I/O, and the banner said
  the flattering half.** A filter narrows what has to be *measured*; it cannot narrow what has to be
  *read*, because the surviving keys are known only after the object side is scanned and
  `ScanRequest` carries a conjunction of equality pairs — a key set has no spelling in it, the way a
  range has none. So the vector column is materialized whole on every call, filtered or not, and the
  floor grows with the embedded rows rather than with the answer. Both halves are now said, in the
  banner and in §7.2, because an operator sizing a deployment acts on the larger one.

  What would fix it is not this slice's and is already named twice: the pushdown channel the
  range-pushdown backlog entry describes, or partitioning the sidecar on the object's own property —
  which is the move recorded when denormalised link columns were refused, and which optimises the
  read without changing what a ranking means.

- **`loom query --match` exists, and `--match --key` is refused.** The dev command mirrors the
  generated tools or the ontology has a back door, and this is the first time that rule was read from
  the other end: `bind_matching` answering `None` means *no tool was generated*, so the command has
  to say so rather than quietly doing something the surface cannot. A ranked read and a keyed one are
  different verbs — a similarity over the one row you already named is a number about nothing — so
  the combination is refused rather than resolved by precedence.

## Refused, permanently

- **Blending vectors across a link** — ranking Customer by the meaning of its Orders' text, via a
  mean of their vectors. A mean over a one-to-many denotes nothing in particular, and there is no
  honest answer to what "similar" would then mean. Rank the object that owns the text and traverse.
  The expansion step people build whole retrieval systems for is `traverse`, and it is already here:
  declared, deterministic and governed.
- **A staleness threshold that blocks a call.** Any number is a magic one.
- **An external vector store.** A second data plane governance cannot reach.

## What this leaves owing

**Erasure now has three targets, and the backlog entry names two.** An embedding is not a
fingerprint — it is a lossy, partially invertible copy, and inversion works best on exactly the
short text worth embedding. So a row erased from its table leaves recoverable text in the sidecar.
Not reachable through `match_`, since the join to a deleted row returns nothing, but readable by
anyone with warehouse access, and Loom is what put it there.

M10 does not build the general erasure command; it owed three small things to the slice that will,
and slice 2 has paid all three.

Slice 2's orphan prune is documented in `embed.store` as *the* vector erasure path, with its lag
stated: exactly the interval between reconciles. A `delete` action prunes that key's vector **before**
it deletes the row and **fails if it cannot** — the one place the best-effort rule does not apply,
because the two failures are not symmetric: a failed embed leaves a row briefly missing from search,
and a failed vector delete leaves personal data outliving the request that erased it. The ordering is
what makes that promise keepable; refusing after the row is gone would leave nothing to refuse, and
the inverse failure (prune lands, row delete loses its race) is the harmless direction, since a row
with no vector is what the reconcile exists to notice. And the backlog entry names three targets, so
the erasure slice does not ship correct against a world that stopped existing.

What it costs is a fourth port in the action runtime's reach, and that is named rather than glossed:
a `VectorWriter` can delete rows, which nothing the action runtime held could previously do. It is
bounded by the same property the log ports rely on — no verb takes a table name — so the whole of
what it reaches is `_loom_meta.vectors__<type>` for the type the action already writes.

---

[← M9](./m09-bulk-ingest.md) · [M11 →](./m11-on-ramp.md) · [backlog](./backlog.md)
