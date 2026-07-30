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

Early. The **spec module** (this repo's current contents) is complete and tested:

| Component | State |
|-----------|-------|
| Canonical type system (`types.py`) | ✅ |
| Expression mini-language (`expr.py`) | ✅ |
| Typed Ontology Model (`model.py`) | ✅ |
| YAML loader — structural validation (`loader.py`) | ✅ |
| Referential/semantic validator (`validator.py`) | ✅ |
| `loom validate` CLI | ✅ |
| Physical validation vs. live catalog | ⏳ stub (`check_physical`) |
| Catalog + query resolver (DuckDB/Iceberg) | ⏳ next |
| Migration engine (`plan` / `apply`) | ⏳ |
| MCP server (`serve`) | ⏳ |

`docs/spec-v0.md` is the full grammar — the framework's public contract.
`docs/ROADMAP.md` tracks what's next, milestone by milestone.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest                              # 36 tests
loom validate tests/fixtures/valid  # → ok — 2 object type(s), 1 link type(s), 2 action(s)
```

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
