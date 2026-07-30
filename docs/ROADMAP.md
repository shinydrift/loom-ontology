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

## ⏳ M1 — Catalog + query slice (next)

*Goal: `get_customer("c1")` returns a real row from a local Iceberg table.* The first time you
can query the ontology.

- [ ] `catalog/` — Iceberg REST catalog client + table introspection (pyiceberg). Bind an
      `objectType.backing` to a live table; list columns + Iceberg types.
- [ ] Wire up **physical validation** — implement `validator.check_physical()` (the stub):
      table/column existence + type promotion-compatibility against the bound catalog.
- [ ] `query/ir.py` — the logical plan node set: `GetByKey`, `Search`, `Traverse`, `Project`.
- [ ] `query/engine.py` — the `Engine` port (`capabilities()` / `compile()` / `execute()`).
- [ ] `query/engines/duckdb.py` — first adapter; lowers IR → DuckDB SQL over Iceberg.
- [ ] `resolver.py` — ontology ops → IR; link `Traverse` → JOIN via from/to mapping (+ reverse).
- [ ] Local test harness: seed a tiny Iceberg table, assert `GetByKey` / `Search` round-trip.
- [ ] `loom serve` (partial) or a `loom query` dev command to exercise it by hand.

**Definition of done:** a test writes rows to a local Iceberg table and reads them back through
the resolver + DuckDB adapter, including a one-hop link traversal.

---

## ⏳ M2 — Migration engine (`plan` / `apply`)

*Goal: edit the YAML, run `loom plan`, see a classified diff; `loom apply` evolves Iceberg.*

- [ ] `_loom_meta` state store — serialized applied spec + version + content-hash + history.
- [ ] Diff engine — classify changes: safe/additive · physical-safe (Iceberg field-id) · breaking.
- [ ] `renamedFrom:` handling — treat as a field-id remap, not drop+add.
- [ ] `loom plan` — terraform-style dry-run of the classified diff.
- [ ] `loom apply` — execute physical DDL in an Iceberg transaction; bump version; idempotent.
- [ ] Rollback path — restore prior spec + point physical schema at an earlier snapshot.

---

## ⏳ M3 — Action runtime (single-object writeback)

*Goal: `run_upgradeTier(...)` mutates one row atomically.*

- [ ] Parameter binding + validation-rule evaluation (reuse `expr` AST → evaluator).
- [ ] Effect compiler → Iceberg **catalog-level** write (equality-delete on PK + append), one txn.
- [ ] Optimistic concurrency — version/snapshot check; conflict → typed retryable error.
- [ ] Edit-log (audit) table — actor, action, before/after, snapshot id.
- [ ] Tests: create / modify / delete happy paths + a concurrent-conflict path.

---

## ⏳ M4 — MCP server (`loom serve`)

*Goal: point any MCP client at the ontology and call typed tools.*

- [ ] `mcp/registry.py` — introspect the Ontology Model → tool set at boot.
- [ ] Per object: `get_<type>` / `search_<type>` / `list_<type>`; generic `traverse`.
- [ ] Per action: `run_<action>` with JSON Schema from parameters, description from the spec.
- [ ] Capability negotiation at serve — validate spec features vs. `engine.capabilities()`.
- [ ] Hard rule tests: no raw-SQL tool is ever exposed.
- [ ] `loom serve` over stdio (then HTTP).

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
- [ ] Docs site / expanded README once M1 lands
- [ ] Type-check (mypy) + lint (ruff) in CI alongside pytest
