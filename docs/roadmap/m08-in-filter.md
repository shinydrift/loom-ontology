[← Roadmap index](../ROADMAP.md)

# ✅ Done — M8 first slice: `in`, a disjunction the conjunction could already hold

*Goal: the query that cost a caller N calls.*

M7 left `or` / `in` / `not` in the backlog as **one** entry, on one argument: none of them is a
conjunction, so each needs "a tree rather than a tuple". That argument is true of two of the three.

**1. The correction — `in` disjoins values, not predicates.**

`ir.Search.filters` is a flat ANDed tuple. An `or` composes *predicates* and cannot live in one;
`in` composes *values*, against a single column, all of them constants — so it is **one element** of
that tuple, `ir.In`, sitting exactly where `Contains` sits: filter-only, no negation, no tree. Both
`filters.py` and §7.1 had written the sentence the other way (`in` is "sugar over an `or` that does
not exist yet"), which is the kind of claim that is right about the *word* and wrong about the
*shape*. Both now say so rather than quietly not saying it, which is the second time this codebase
has had to correct a prediction about the filter grammar and the second time the correction was
narrower than the prediction.

**2. Null-safe, because an abbreviation that selects different rows is a trap.**

`Compare('==')` is null-safe here — §5 says null is a value — so `{"in": [null]}` selects the rows
`{"eq": null}` selects, and a one-element list selects the rows the `eq` it abbreviates would. SQL's
own `IN` says neither: it never matches a null *element* (the list is compared with `=`) and it
answers unknown for a null *column*. So the adapter lifts the null out into its own disjunct —
`(c IN (?) OR c IS NULL)`, and `IS NULL` alone when the list is only nulls, which is byte-identical
to what `{"eq": null}` already compiled to. **This is the failure that would not have shown up in
testing**: the two spellings agree on every row of every table with no nulls in the filtered column,
which is most tables most of the time. It is asserted three ways — as nodes, as SQL, and as rows out
of the warehouse.

**3. `{"in": []}` is refused** — the same argument as M7's bare null, reached from the other end.
The empty list *has* an honest answer, and returning it is exactly what makes it a refusal: a caller
cannot tell "your list was empty" from "nothing matched", so an agent whose candidate set collapsed
to nothing is told, in the vocabulary of a result, that its question was answered. `minItems: 1` in
the generated schema is that refusal announced rather than only enforced. `{"in": null}` is refused
too, as a *shape* — the list is never null, though an element may be.

**4. No pushdown hint, and this one is correctness rather than a channel too narrow.** A range yields
no `ScanRequest.predicates` entry because a `(column, value)` pair has no spelling for one. An `In`
yields none for a stronger reason: the pairs are **ANDed** (`_row_filter` folds them with `And`), so
one hint per value would prune a membership test to the rows matching *every* value — an empty scan,
and a wrong answer arriving through a channel documented as advisory.

**5. No new negotiated capability** — the third time `negotiate.py`'s rule has decided one of these,
and the same answer M7 gave `range_comparisons`: *a requirement is something a spec can demand and an
engine can fail*, and no dialect that can say `WHERE c = ?` cannot say `WHERE c IN (?, ?)`. `in` is
offered wherever `eq` is, gated by no type test and no `searchable` declaration, because it **is**
`eq`.

**6. `loom query` repeats the flag** — `--filter tier.in=gold --filter tier.in=platinum` — rather
than splitting one value on a separator. A comma is a legal character inside a string value, so
`tier.in=a,b` would either forbid it or silently turn one value into two wrong ones, and this command
exists to mirror what the generated tool would do with the same filter. Repeating any *other*
operator is now an error; it used to keep the last value silently, which was a filter nobody wrote
being answered as if they had.

**Scope:** `or` and `not` are untouched and still need the tree. `not` is the expensive one — it
reopens the claim M7's whole null story rests on (*this grammar has no negation*, so SQL's
three-valued answer and Loom's select the same rows), and it would have to answer it the way
`predicate.py` does. Closing M8 means paying that.

---

[← M7](./m07-typed-filters.md) · [M9 →](./m09-bulk-ingest.md) · [backlog](./backlog.md)
