# Loom Roadmap

Living plan. The spec module (the compiler front-end) is done; everything below turns the
validated Ontology Model into a running system. Ordered so each milestone is independently
demoable — you can *see* the ontology do something new at the end of each.

Design decisions these build on live in [`spec-v0.md`](./spec-v0.md): one YAML spec compiles to
four surfaces; reads go through an engine-agnostic IR; writes are single-object via the Iceberg
catalog.

This file is the index. Each milestone is a page under [`roadmap/`](./roadmap/) holding the same
record it always held — the scope, the decisions made while it was built, and what it left owing.

---

| | Milestone | |
|---|---|---|
| ✅ | [**M0** — Spec module](./roadmap/m00-spec-module.md) | The compiler front-end: types, expressions, model, loader, validator, `loom validate` |
| ✅ | [**M1** — Read slice, end to end](./roadmap/m01-read-slice.md) | Catalog → query → MCP: a real row from a real Iceberg table, through a tool |
| ✅ | [**M2** — Migration engine](./roadmap/m02-migration-engine.md) | `loom plan` / `apply` / `rollback`, and `renamedFrom` as a field-id remap |
| ✅ | [**M3** — Action runtime](./roadmap/m03-action-runtime.md) | Single-object writeback, concurrency asserted inside the commit, `_loom_meta.edits` |
| ✅ | [**M4** — MCP write surface](./roadmap/m04-mcp-write-surface.md) | `run_<action>` tools, HTTP transport, capability negotiation |
| ✅ | [**M5** — Governance](./roadmap/m05-governance.md) | `governance.policies`: column masks, row predicates, the edit log under a posture |
| ✅ | [**M6** — A per-caller identity](./roadmap/m06-attested-identity.md) | `mcp.auth` attests a caller; `when:` lets a policy name one |
| ✅ | [**M7** — Fully typed filters](./roadmap/m07-typed-filters.md) | `filter: {salesDate: {gte: …, lt: …}}` — operators a property's type deserves |
| ✅ | [**M8** — `in`](./roadmap/m08-in-filter.md) | First slice: a disjunction of *values* the conjunction could already hold. `or` / `not` still open |
| ✅ | [**M9** — Bulk ingest](./roadmap/m09-bulk-ingest.md) | `loom ingest`: a batch becomes rows, and `_loom_meta.loads` says so |
| 🔨 | [**M10** — Semantic search](./roadmap/m10-semantic-search.md) | `semantic:`, `loom embed`, `match_<object>` — slice 4 (`via`) in progress |
| ✅ | [**M11** — The on-ramp](./roadmap/m11-on-ramp.md) | `loom infer` drafts a spec from a file; `sequences:` runs loads in order |
| | [**Backlog**](./roadmap/backlog.md) | Spec edges from spec-v0's "Open edges", plus cross-cutting infra |

Prose version of the same story — what each milestone decided, and why — is
[`status.md`](./status.md).
