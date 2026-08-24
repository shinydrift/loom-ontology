# Loom Ontology Spec — v0 Grammar

> The framework's public contract. Every Loom surface — Iceberg schema/migrations, the
> query resolver, the action runtime, and the MCP tool registry — is **compiled from these
> files**. If it isn't expressible here, the framework can't do it. Keep this small and honest.

This page is the index. Each numbered section is a page under [`spec/`](./spec/), and the
section numbers are stable — code and comments cite this grammar as "§6" or "§2 rule 7", and
those references resolve through the table below.

---

## §0–§1 — [Conventions and the canonical type system](./spec/00-conventions-and-types.md)

How a spec is laid out on disk and what a `type` may be — the one table the whole framework
leans on, where each Loom type knows both its Iceberg type and its JSON-Schema shape.

- [§0 Conventions](./spec/00-conventions-and-types.md#0-conventions)
- [§1 Canonical type system](./spec/00-conventions-and-types.md#1-canonical-type-system)

## §2 — [`objectType`](./spec/02-object-type.md)

The core declaration: a table, its primary key, the properties mapped onto its columns, and
which of them are `searchable` or `semantic`.

- [§2.1 `renamedFrom` — a moved column, not a new one](./spec/02-object-type.md#21-renamedfrom-a-moved-column-not-a-new-one)

## §3 — [`linkType`](./spec/03-link-type.md)

How two object types are joined, and what a `traverse` is allowed to be.

## §4 — [`action`](./spec/04-action.md)

Parameters, validation rules, and effects — the only thing in the grammar that changes a row.

- [§4.1 What running an action actually does](./spec/04-action.md#41-what-running-an-action-actually-does)

## §5 — [Expression mini-language](./spec/05-expressions.md)

The one expression language, shared by validation rules, effect values and governance predicates.

- [§5.1 `{{ … }}` is punctuation, not a second language](./spec/05-expressions.md#51-is-punctuation-not-a-second-language)
- [§5.2 What the evaluator does with values](./spec/05-expressions.md#52-what-the-evaluator-does-with-values)

## §6 — [Project config — `loom.yaml`](./spec/06-project-config.md)

Everything that is a fact about a *deployment* rather than about the ontology: catalogs, the MCP
surface, governance, bulk loads, embeddings.

- [§6.1 `governance`](./spec/06-project-config.md#61-governance)
- [§6.2 `ingest` — how rows get in, in bulk](./spec/06-project-config.md#62-ingest-how-rows-get-in-in-bulk)
- [§6.3 `mcp.embedding` — where a vector comes from](./spec/06-project-config.md#63-mcpembedding-where-a-vector-comes-from)

## §7 — [What the grammar compiles to](./spec/07-compilation.md)

The deterministic mapping from a spec to the tool surface, and the filter grammar those tools take.

- [§7.1 The filter grammar — what `search_<type>` takes](./spec/07-compilation.md#71-the-filter-grammar-what-search_type-takes)
- [§7.2 `match_<type>` — ranking, and why it is not an operator in §7.1](./spec/07-compilation.md#72-match_type-ranking-and-why-it-is-not-an-operator-in-71)
  - [`via` — narrowing by a linked object](./spec/07-compilation.md#via-narrowing-by-a-linked-object)

## §8 — [Worked example](./spec/08-worked-example.md)

A complete, valid mini-ontology: two object types, one link, one action.

## §9 — [`_loom_meta` — what Loom recorded](./spec/09-loom-meta.md)

The lake's own record of every migration, edit and load Loom made.

- [§9.1 `rollback` — what this table is *for*](./spec/09-loom-meta.md#91-rollback-what-this-table-is-for)
- [§9.2 `edits` — what an action did](./spec/09-loom-meta.md#92-edits-what-an-action-did)
- [§9.3 `loads` — what an ingest did](./spec/09-loom-meta.md#93-loads-what-an-ingest-did)

## [Open edges (v0 → v1)](./spec/open-edges.md)

Deferred on purpose, each with the reason it was deferred. Tracked as work in
[`roadmap/backlog.md`](./roadmap/backlog.md).
