[← Roadmap index](../ROADMAP.md)

# ✅ Done — M1: Read slice, end to end: catalog → query → MCP

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

[← M0](./m00-spec-module.md) · [M2 →](./m02-migration-engine.md) · [backlog](./backlog.md)
