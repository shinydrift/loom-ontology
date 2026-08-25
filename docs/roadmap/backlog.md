[← Roadmap index](../ROADMAP.md)

# Backlog

Not yet scheduled into a milestone.

## Spec edges (from spec-v0 §"Open edges")

Consciously deferred in v0; each is a self-contained follow-up:

- [ ] Composite (multi-property) primary keys — ripples into `key` exprs + objectRef encoding
- [ ] Complex property types — `array` / `struct` / `map`
- [ ] Computed / derived properties — backed by an expression instead of a column
- [x] ~~**Fully typed object filters**~~ — **done** (M7, above). Everything this entry asked for,
      plus two refusals it did not anticipate: a bare `null` filter value, and a capability flag for
      range comparisons. What it left open and M7 closed by deciding: the comparison node sets merged
      on their overlap, `searchable` kept the gate and lost the substring job, and operator keys sit
      one level below a property name under a restated §7.
- [ ] **`or` / `not` in a caller's filter** — ~~`in`~~ **done** (M8's first slice, above), and it
      came out of this entry by contradicting its premise: this entry said all three "are not
      conjunctions" and therefore all three need a tree, which is true of a disjunction of
      *predicates* and not of a disjunction of *values*. The two that remain do need it: an IR shape
      (a tree rather than a tuple), an engine lowering and a `pushdown_hints` answer — a hint derived
      from one arm of an `or` is wrong unless the `WHERE` still re-applies the whole thing. Kleene
      propagation is settled already, so the null question does not reopen — but `not` does reopen
      the one thing M7 leaned on, that this grammar has no negation, and would have to answer it the
      way `predicate.py` does.
- [ ] **Range pushdown** — `ScanRequest.predicates` is a `(column, value)` pair by shape, so M7's
      ranges reach the `WHERE` clause and never the scan. An Iceberg-native adapter could prune on
      them; the channel would have to carry an operator, and it stays a *hint* either way.
      **M9 added a second caller for the same channel**: a `merge` reads its target with a full scan,
      because a batch of N keys has no spelling in a `(column, value)` pair either. A key-set
      predicate and a range predicate want the same widening, which is an argument for doing it once.
- [ ] **Multi-object actions** — the post-v1 feature the single-object boundary reserves room for
- [ ] **Edit-log erasure** — a command that *redacts* records in place (keep the row, empty
      `parameters`/`before`/`after`/`object_key`), never one that deletes them; a holder and a port
      of its own, so the action runtime still cannot reach a verb that rewrites `_loom_meta.edits`.
      M5's third slice decided the shape and deliberately did not build it: it is a command, a port
      and an erasure semantics, which is a slice rather than a coda to one.
      **Three targets, not two, since M10.** A row's text lives in its table, in `_loom_meta.edits`
      as `before`/`after` — and now in the vector sidecar, because an embedding is a lossy but
      *partially invertible* copy rather than an opaque token. A command that reaches two of the
      three is one that reports success while leaving recoverable text in the lake
- [ ] More engine adapters — Trino, Spark (+ route writes through native `MERGE` when
      `capabilities().native_merge`)

---

## Cross-cutting / infra

- [ ] `pyproject` extras for engine backends (`[duckdb]`, `[trino]`) and catalog clients
- [x] Example end-to-end project under `examples/` (seedable local Iceberg + a demo consumer of the
      served surface) — `examples/retail/` is the seedable half; `examples/retail/dashboard/` is the
      other half, and it turned out **not** to be an agent loop. What the box was reaching for is a
      consumer that cannot reach past the tool surface, and an LLM in the middle would have made
      that harder to see rather than easier: a loop's output is a function of the model, so a
      missing capability reads as a bad turn. The dashboard is deterministic, so what it cannot do
      is legible — and it is a *second deployment* of the same spec (socket, writes on, policies a
      two-line edit away), which is the claim `examples/` existed to demonstrate and could not with
      one deployment. A demo agent loop is still worth having; it is now a smaller box, because the
      surface it would drive is exercised.
- [ ] Docs site / expanded README now that M1 has landed
- [ ] Type-check (mypy) + lint (ruff) in CI alongside pytest
- [x] `examples/retail/dashboard`: a malformed JSON body to `POST /api/call` returned a bare
      Starlette 500 rather than a 400 naming the problem. Filed here by the whole-app probe on the
      grounds that it was reachable by curl and by nothing else — every call the UI makes is
      `JSON.stringify`d — and fixed the next time the example was open, as that note predicted. A
      body that is not JSON, and a body that is JSON but not an object, are each a 400 saying so.

---

[← M11](./m11-on-ramp.md)
