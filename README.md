# Loom

**A declarative ontology framework over [Apache Iceberg](https://iceberg.apache.org/), wired to LLM agents via [MCP](https://modelcontextprotocol.io/).**

Loom is an open take on the idea behind Palantir Foundry's Ontology: a semantic layer that
turns raw Iceberg tables into a navigable graph of **objects**, **links**, and **actions** —
and exposes that graph to agents as typed, governed tools instead of raw SQL.

The premise: Iceberg is now a commodity (Foundry, Snowflake, Databricks, AWS, GCP all speak
it). The value isn't storage — it's the layers *on top*. Loom is those layers.

## The core idea

One declarative YAML spec is the single source of truth, and it compiles to four surfaces at
once — you never hand-write a migration, a SQL join, or an MCP tool:

```
                    ┌──────────────────────────┐
   ontology/*.yaml ─┤  Ontology Model (typed)   ├─┬──▶ Iceberg schema (DDL / migrations)
   (source of truth)└──────────────────────────┘ ├──▶ Query resolver (ops → engine SQL)
                                                   ├──▶ Action runtime (validate + writeback)
                                                   └──▶ MCP tools (auto-registered)
```

Think **dbt/Prisma**, but the generated "client" is an MCP tool surface for agents.

## Example

```yaml
# ontology/customer.yaml
objectType:
  apiName: Customer
  primaryKey: customerId
  title: name
  backing: { catalog: rest_main, table: crm.customers }
  properties:
    - { name: customerId, type: string, column: id, unique: true }
    - { name: name,       type: string, column: full_name }
    - { name: tier,       type: enum, values: [bronze, silver, gold], column: tier }
  searchable: [name, tier]
```
```yaml
# ontology/actions/upgrade-tier.yaml
action:
  apiName: upgradeTier
  description: Raise a customer to a higher membership tier   # becomes the MCP tool description
  targetObjectType: Customer
  operation: modify
  parameters:
    - { name: customer, type: objectRef, objectType: Customer }
    - { name: newTier,  type: enum, values: [silver, gold] }
  validation:
    - { rule: "newTier != object.tier", message: New tier must differ from current tier }
  effects:
    - modifyObject: { key: "{{ customer }}", set: { tier: "{{ newTier }}" } }
```

At serve time this exposes `get_customer`, `search_customer`, `list_customer`, and
`run_upgradeTier` to any MCP client — with input schemas derived from the property/parameter
types, and row/column governance enforced below the tool layer.

*Today the read tools and `traverse` are live; `run_<action>` and governance are the next two
milestones. See [Status](#status).*

## Architecture (5 layers)

```
5. Agent / MCP layer       ← LLMs call the ontology as typed verbs (never raw SQL)
4. Action & function layer ← single-object writeback via the Iceberg catalog, optimistic concurrency
3. SEMANTIC / ONTOLOGY layer ← object types, links, mappings   ★ the moat
------------------------------------------------------------
2. Query / compute engine  ← engine-agnostic IR → DuckDB / Trino / Spark adapters
1. Storage                 ← Apache Iceberg tables + REST catalog
```

Two decisions shape the framework:
- **Reads** flow through an engine-agnostic IR (`GetByKey` / `Search` / `Traverse` / `Project`)
  that per-engine adapters lower to dialect SQL.
- **Writes** are single-object and bypass the compute engine entirely — an equality-delete +
  append committed as one atomic Iceberg transaction — which keeps the write path uniform
  across engines.

## Status

Early, but the **read path works end to end**: a YAML spec over real Iceberg tables, served to an
MCP client as typed tools. The write path — migrations and actions — is next.

| Component | State |
|-----------|-------|
| Canonical type system (`types.py`) | ✅ |
| Expression mini-language (`expr.py`) | ✅ |
| Typed Ontology Model (`model.py`) | ✅ |
| YAML loader — structural validation (`loader.py`) | ✅ |
| Referential/semantic validator (`validator.py`) | ✅ |
| Project config — `loom.yaml` (`config.py`) | ✅ |
| Catalog port + pyiceberg impls (`catalog/`) | ✅ |
| Physical validation vs. live catalog | ✅ `loom validate --physical` |
| Query IR + `Engine` port (`query/`) | ✅ |
| DuckDB adapter | ✅ |
| Resolver — ontology ops → IR (`resolver.py`) | ✅ |
| MCP **read** tools + `loom serve` (`mcp/`) | ✅ |
| Migration engine (`plan` / `apply`) | ⏳ next |
| Action runtime — single-object writeback | ⏳ |
| MCP `run_<action>` tools + HTTP transport | ⏳ |
| Governance (row/column policies) | ⏳ |

`docs/spec-v0.md` is the full grammar — the framework's public contract.
`docs/ROADMAP.md` tracks what's next, milestone by milestone.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,iceberg,duckdb,mcp]"

pytest                              # 148 tests
loom validate tests/fixtures/valid  # → ok — 2 object type(s), 1 link type(s), 2 action(s)
```

Then run the whole stack against a real Iceberg table. `examples/retail` ships the worked example
plus a seed script that builds a local Iceberg warehouse — SQLite metastore, filesystem storage,
no services to start:

```bash
python examples/retail/seed.py                        # create + populate the Iceberg tables
loom validate --physical examples/retail/ontology     # check the spec against live metadata
loom query Customer examples/retail/ontology --key c1 # → one row, through DuckDB
loom query Customer examples/retail/ontology --key c2 --link orders   # → a link traversal
loom serve examples/retail/ontology                   # → 7 MCP tools over stdio
```

Point any MCP client at that last command and the ontology shows up as typed tools:

```
$ loom serve examples/retail/ontology
loom serve — 2 object type(s), 1 link type(s), 0 action(s) → 7 tool(s) over stdio
  get_customer  get_order  list_customer  list_order  search_customer  search_order  traverse
```

```jsonc
// traverse({"objectType": "Customer", "key": "c2", "link": "orders", "limit": 2})
{
  "targetObjectType": "Order", "cardinality": "many_to_one",
  "count": 2, "limit": 2, "offset": 0, "hasMore": true,
  "objects": [
    { "orderId": "o3", "customerId": "c2", "total": "89.95", "placedAt": "2026-02-14T12:00:00+00:00" },
    { "orderId": "o4", "customerId": "c2", "total": "2100.00", "placedAt": "2026-03-02T12:00:00+00:00" }
  ]
}
```

Swapping the local warehouse for a production lake is a `loom.yaml` edit — `type: iceberg-rest`
with a URI — not a spec or code change.

Three properties of that generated surface are worth naming, because they're enforced rather than
documented:

- **No raw SQL reaches the agent.** The resolver only emits plan nodes it built itself, so there is
  no code path from a tool call to arbitrary SQL. Asserted in `tests/test_mcp_registry.py`.
- **Every read is bounded and ordered.** There is no way to ask for an unbounded scan, and paging
  is stable because plans always carry an `ORDER BY` on the primary key.
- **Declared types are honored on the way in and out.** A key arriving as `"42"` for a `long`
  property is coerced before it becomes a predicate, and `decimal` values never pass through a
  float.

The validator accumulates every problem and reports them in one pass with source locations:

```
$ loom validate ./broken-ontology
3 problems in ontology spec:
  - customer.yaml · objectType 'Customer': unexpected key 'titel'
        hint: did you mean 'title'?
  - actions/up.yaml · action 'upgradeTier': expression references unknown parameter 'ghost'
  - actions/up.yaml · action 'upgradeTier': effect set 'bogus' is not a property of 'Customer'
```

## License

MIT — see [LICENSE](LICENSE).
