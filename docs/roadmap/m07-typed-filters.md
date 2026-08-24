[← Roadmap index](../ROADMAP.md)

# ✅ Done — M7: Fully typed object filters

*Goal: the query an agent actually wants — a date range — expressible at all.*

`DailySalesPerformance` shipped in M5's example as a precomputed daily table whose whole point is a
date range, and no caller could ask for one: a filter could say equality and `searchable` substring,
and `searchable` could not even name a `date`. Five things had to be settled first, and two ended in
refusals.

**1. The two comparison node sets become one, on the overlap — and a prediction is corrected.**

v0's `ir` predicted "ranges arrive with the filter grammar". M5 shipped ranges for *governance*, as
`Compare`, and corrected the prediction to *the two are deliberately not one node set*. Ranges then
arrived a **second** time, in a caller's hands, which is where that correction turns out to have
over-generalised from the one node it was true of. The two grammars are:

- the **filter** grammar: `Contains` (ILIKE), no negation, no composition;
- the **policy** grammar: `&& || !`, no ILIKE;
- overlapping **exactly on the six comparisons**, where they already agreed node for node — v0's
  `Eq(col, None)` compiled to `IS NULL`, which is what `Compare('==', col, null)` compiles to, and
  for a bound non-null parameter `=` and `IS NOT DISTINCT FROM` select the same rows.

So the overlap merges and the difference stays. What made a governance predicate un-advisory was
never the node **type** — it is the **field**: a predicate hangs on `TableRef.predicate`, which the
adapter compiles into `WHERE`, and only `Search.filters` yields `ScanRequest` hints. That is now
structural rather than remembered: `ir.pushdown_hints()` is the one function that decides what may
become a hint, it takes filters and cannot be handed a table, and a test drives a governed search and
asserts the predicate is in the `WHERE` and the hint channel is empty. `Eq` survives as one thing
only — a `Traverse` anchor, structurally narrower than a comparison — and says so.

One thing does **not** transfer with the merge, and it is why `Contains` can exist at all in a
grammar that refuses `contains`: the *lowerable subset* rule is about expressions answered **twice**.
A policy's is; a caller's filter is answered once, on the read path, by the engine.

**2. Null: the same three answers, and the refusal is the spelling rather than the semantics.**

M5 settled *admitted only on true* for a policy on the grounds that per row there is no channel and
per call the report is an oracle. A filter has a caller who asked, so all three of §5's readings were
open. The answer is that they collapse: **this grammar has no negation**, so the disagreement M5 had
to settle (`NOT undecided` failing open) cannot arise, and SQL's three-valued answer and *admitted
only on true* select the same rows. The rule generalises with a second reason rather than an
exception — a policy admits only on true because it has nobody to tell; a filter does because a
filter selects rows and an undecided row is not selected.

Reporting the undecided rows to the caller was considered and refused: it puts a third quantity
beside `hasMore`/`offset` on every page, and the information is already available *by asking* —
`{"eq": null}` returns exactly those rows.

**The refusal is `{"ltv": null}`, permanently.** JSON cannot distinguish a key a caller left blank
from one it meant as null, and an agent emitting null for *a value it did not have* is the likeliest
way this argument is ever malformed. v0 answered it as `ltv IS NULL` — a plausible, non-empty result
set for a question nobody asked, which is the failure `negotiate.py` calls worse than failing when it
refuses to compile `Contains` down to `Eq`. So null is legal only where the caller wrote the operator
too, and §5's *testable, not orderable* becomes visible in the generated schema: `eq`/`ne` admit a
null and the four ordering operators do not, with a test asserting the schema and the grammar admit
one in exactly the same places. **This is a break** — `search(X, {"p": None})` used to work — and it
is a refusal replacing a wrong answer, which is the direction this codebase breaks in.

**3. `searchable` keeps the gate and loses the substring job.**

Making every unmasked property filterable was the tempting simplification and is a **widening of the
surface**: it would expose as queryable what a spec never marked queryable, and §7's *the tool set is
a function of the spec* cuts the other way — a spec that declares nothing searchable is saying
something. So `searchable` still decides what appears in the `filter` schema, and what it loses is
the invisible second job: substring is now an operator a caller can see.

What that costs is stated rather than hidden: **§2 rule 6 widens** from string-or-enum to any type
(a widening of what an author may *declare*, never of an existing spec's surface), and the shipped
example gains `searchable: [salesDate, grossSales, sourceTable]` — the acceptance case's honest
price. Every spec already written keeps exactly the filters it had, with the same meanings, because
**the bare spelling keeps its type-directed meaning**: substring for a searchable string, exact for
everything else. Rewriting it as a plain `eq` would return fewer rows to every filter already written
against a searchable string, with nothing raising.

`searchable` also gates one thing below the surface, and this is new: the `contains` **operator**
requires it, because `negotiate.py` demands `case_insensitive_like` of an engine for exactly the
searchable string properties. Emitting a `Contains` for any other property would ask an engine for
something no requirement checked it could do. `loom query`'s long-standing ability to filter on a
non-searchable property is otherwise untouched — it reveals no row and no property the served surface
withholds, so it is a surface asymmetry rather than the back door the CLI is careful not to be.

**4. Operators live one level below a property name, and §7's rule is restated rather than bent.**

`filter: {salesDate: {gte: …}}` puts Loom's vocabulary inside the object §7 reserves for the spec's.
The rule survives because it was never "Loom's words appear once": it is that **each level of the
argument tree belongs entirely to one vocabulary, and they alternate** — top level Loom's, `filter`
the spec's, per-property Loom's. A property name never appears where an operator does, so a spec may
declare a property called `gte`, and a test asserts the two key sets are disjoint.

Three shapes rejected: `salesDate_gte` mixes vocabularies inside one name and collides with a
property actually called that; `{gte: {salesDate: …}}` puts Loom's words at the spec's level; and a
list of `{property, op, value}` triples turns property names into *values*, so JSON Schema stops
typing them — the untyped bag §7 refuses when it argues one tool per action.

The schema is an `anyOf` of the two spellings, which is what keeps v0's payloads valid and roughly
quintuples each property's fragment. `loom query` gets `--filter PROP.OP=VALUE` beside `PROP=VALUE`
so it still mirrors the tools; a null filter remains inexpressible there, because every CLI value is
a string.

**5. No new negotiated capability — the second refusal.**

A `range_comparisons` flag would be one **no adapter could ever set false**: every dialect that can
say `WHERE c = ?` can say `WHERE c >= ?`. That is the shape this codebase has paid for twice —
`loom.managed`, written and never read, and `native_merge`, a flag no spec can demand — and
`negotiate.py`'s own rule already decides it: *a requirement is something a spec can demand and an
engine can fail.* `case_insensitive_like` stays exactly as it was, demanded by a searchable **string**
property; a searchable `date` demands nothing new. `ScanRequest.predicates` also stays equality-only:
a range has no spelling in a `(column, value)` pair, it costs an Iceberg scan some pruning and costs
correctness nothing, since every filter is in the `WHERE` clause regardless.

Composition is **AND only** — between operators on one property and between properties — which costs
no IR, because `Search.filters` was already a conjunction. `or`, `in` and `not` stay deferred; `in`
is the one most likely to arrive next, and its absence costs a caller N calls.

**Scope:** scalars only. Array and struct properties are their own backlog entry, which also means
§5's `contains` stays unusable in an ontology — and this milestone **spends that word a second
time**, as the filter operator for substring. It is a real collision, taken deliberately: they are
different vocabularies, an agent writes `contains` for substring without being taught, and when
`array` lands its membership operator needs a name of its own.

---

[← M6](./m06-attested-identity.md) · [M8 →](./m08-in-filter.md) · [backlog](./backlog.md)
