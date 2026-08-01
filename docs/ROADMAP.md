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

## ✅ Done — M1: Read slice, end to end: catalog → query → MCP

*Goal: point an MCP client at a Loom ontology and ask it for a real row from a real Iceberg
table.* The first time the ontology does the thing the README claims.

**Scope note — why MCP moved up.** M1 was originally just the catalog + query slice, with the
MCP server deferred to M4 behind migrations and the action runtime. That ordering ends M1 at a
`loom query` dev command that only a test can see. The read-only half of the MCP surface is
~150 LOC on top of the resolver (registry introspection + two stdio handlers) and it's what
makes the milestone demoable to a person rather than a test suite. So M1 now covers the whole
**read** path top to bottom, and M4 keeps only what genuinely depends on M2/M3 — the `run_<action>`
tools and capability negotiation.

The write path is untouched by this: no migrations, no action runtime, no `_loom_meta`.

- [x] `config.py` — the `loom.yaml` project config from spec §6 (`catalogs`, `engine`, `mcp`),
      reusing the same accumulate-all-errors `Diagnostics` as the spec loader.
- [x] `catalog/` — a `Catalog` port + table introspection (columns → Iceberg types, field ids),
      with pyiceberg-backed implementations. Binds an `objectType.backing` to a live table.
- [x] Wire up **physical validation** — implement `validator.check_physical()` (the stub):
      table/column existence + type promotion-compatibility against the bound catalog.
      Surfaced as `loom validate --physical`.
- [x] `query/ir.py` — the logical plan node set: `GetByKey`, `Search`, `Traverse`, `Project`.
- [x] `query/engine.py` — the `Engine` port (`capabilities()` / `compile()` / `execute()`).
- [x] `query/engines/duckdb.py` — first adapter; lowers IR → DuckDB SQL over Iceberg.
- [x] `resolver.py` — ontology ops → IR; link `Traverse` → JOIN via from/to mapping (+ reverse).
- [x] `mcp/registry.py` — Ontology Model → read tool set (`get_` / `search_` / `list_` per
      object type, generic `traverse`), input schemas from `PropType.json_schema()`.
- [x] `loom serve` over stdio, read-only. Hard-rule test: no raw-SQL tool is ever exposed.
- [x] `examples/` — a seedable local Iceberg warehouse + the worked-example ontology, so the
      whole path is runnable by hand and in CI.

**Definition of done:** a test seeds rows into a local Iceberg table and reads them back through
the resolver + DuckDB adapter, including a one-hop link traversal — and `loom serve` exposes
that same ontology as MCP tools, driven end to end over stdio.

**Two decisions taken here:**
- **Local-first catalog for tests and examples.** pyiceberg's SQLite catalog over a
  filesystem warehouse (`type: iceberg-sql`) sits behind the same `Catalog` port as
  `iceberg-rest`, so CI needs no running services. A REST catalog is a config change, not a
  code change.
- **DuckDB reads Iceberg through pyiceberg, not the DuckDB Iceberg extension.** The adapter
  compiles IR to real DuckDB SQL, and `execute()` binds each referenced table as a named
  relation materialized by a pyiceberg scan — with the scan's own predicate/column pruning
  pushed down. This avoids a runtime extension install and keeps the adapter honest about
  dialect lowering.

---

## ⏳ M2 — Migration engine (`plan` / `apply`) — *in progress: `plan` has landed*

*Goal: edit the YAML, run `loom plan`, see a classified diff; `loom apply` evolves Iceberg.*

- [x] Diff engine (`migrate/`) — classify changes: safe/additive · physical-safe (Iceberg
      field-id) · breaking.
- [x] `loom plan` — terraform-style dry-run of the classified diff.
- [ ] `_loom_meta` state store — serialized applied spec + version + content-hash + history.
- [ ] `renamedFrom:` handling — treat as a field-id remap, not drop+add.
- [ ] `loom apply` — execute physical DDL in an Iceberg transaction; bump version; idempotent.
- [ ] Rollback path — restore prior spec + point physical schema at an earlier snapshot.

**Two decisions taken in the `plan` slice:**
- **The live catalog is the baseline, not a state file.** `Catalog.describe()` already returns
  column types and Iceberg field ids, which is everything a diff needs, so `plan` needs no
  `_loom_meta` to work — and diffing against one instead would make `plan` lie the moment
  somebody changed a table out of band. `_loom_meta` records what `apply` *did*; it is a
  history and an idempotency key, not the planner's source of truth. That's why it moved down
  the list into the `apply` slice.
- **Loom never proposes a drop.** An objectType maps a *subset* of a table's columns, so a
  column no property mentions is not a deleted property — it's someone else's data. Those are
  reported as unmanaged and left alone. It also means `plan` is not `validate --physical`: that
  pass treats a missing table as an error, which is exactly what a plan reports as a creation.

---

## ⏳ M3 — Action runtime (single-object writeback)

*Goal: `run_upgradeTier(...)` mutates one row atomically.*

- [ ] Parameter binding + validation-rule evaluation (reuse `expr` AST → evaluator).
- [ ] Effect compiler → Iceberg **catalog-level** write (equality-delete on PK + append), one txn.
- [ ] Optimistic concurrency — version/snapshot check; conflict → typed retryable error.
- [ ] Edit-log (audit) table — actor, action, before/after, snapshot id.
- [ ] Tests: create / modify / delete happy paths + a concurrent-conflict path.

---

## ⏳ M4 — MCP write surface + transport hardening

*Goal: the action runtime shows up as tools; serve over more than stdio.*

The read tools, the registry, and stdio `loom serve` land in M1 — what's left here is
everything that depends on M2/M3 or on a second transport.

- [ ] Per action: `run_<action>` with JSON Schema from parameters, description from the spec.
- [ ] Capability negotiation at serve — validate spec features vs. `engine.capabilities()`.
- [ ] HTTP transport alongside stdio.
- [ ] Structured tool errors — surface validation-rule failures and write conflicts as typed
      results an agent can act on, not opaque strings.

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
- [ ] Docs site / expanded README now that M1 has landed
- [ ] Type-check (mypy) + lint (ruff) in CI alongside pytest
