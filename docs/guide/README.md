# The Loom guide

The command-by-command tour, in reading order. Every page runs against `examples/retail`, which
builds a real local Iceberg warehouse with no services to start.

| | Page | What it covers |
|---|---|---|
| 1 | [Quickstart](./quickstart.md) | Install, seed a warehouse, query it through DuckDB, serve it as MCP tools over stdio and HTTP |
| 2 | [Drafting a spec from a file](./drafting-a-spec.md) | `loom infer` — parquet in, a draft objectType out |
| 3 | [Migrations](./migrations.md) | `loom plan` / `apply` / `rollback`, and `renamedFrom` |
| 4 | [Running an action](./actions.md) | `loom run` — the only thing in Loom that changes a row |
| 5 | [Loading data in bulk](./loading-data.md) | `loom ingest` and `loom sequence` |
| 6 | [An app on top of it](./dashboard.md) | The retail dashboard — the same ontology with a UI in front |

Two references sit beside this guide:

- [`spec-v0.md`](../spec-v0.md) — the full YAML grammar, the framework's public contract.
- [`ROADMAP.md`](../ROADMAP.md) — what is built and what is next, milestone by milestone.
  [`status.md`](../status.md) is the same story told as prose.
