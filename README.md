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
MCP client as typed tools. The **migration path** now runs too — `loom plan` classifies what a spec
change would do to the physical tables and `loom apply` executes it, bootstrapping an empty
warehouse from nothing but a spec. The action runtime (row-level writeback) is next.

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
| Migration diff + dry run (`migrate/`) | ✅ `loom plan` |
| Migration executor + `_loom_meta` state store | ✅ `loom apply` |
| `renamedFrom` remap · rollback | ⏳ |
| Action runtime — single-object writeback | ⏳ |
| MCP `run_<action>` tools + HTTP transport | ⏳ |
| Governance (row/column policies) | ⏳ |

`docs/spec-v0.md` is the full grammar — the framework's public contract.
`docs/ROADMAP.md` tracks what's next, milestone by milestone.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,iceberg,duckdb,mcp]"

pytest                              # 217 tests
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

## Planning a schema change

`loom plan` is the write path's dry run: it derives the tables the spec wants, diffs them against
the live catalog, and classifies every difference. Run it before seeding anything and the whole
warehouse is a creation:

```
$ loom plan examples/retail/ontology
Loom plan — examples/retail/ontology

  + local.crm.customers — create table · Customer
      + id              string required
      + full_name       string required
      + tier            string required
      + lifetime_value  double optional
...
Plan: 2 to create, 0 to change · 8 safe
```

The classification is the point. Iceberg will let you make a change that costs nothing, one that
rewrites the schema but not the data, and one that quietly invalidates existing rows — and all
three look identical in a YAML diff. Against a table already holding rows, they don't:

```
$ loom plan ./ontology
  ! local.demo.widgets — 3 change(s) · Widget
      ~ score     int -> double         physical-safe
          widening promotion applied by field id 2; existing data files are not rewritten
      ! label     optional -> required  breaking
          existing rows may already hold nulls, which the new constraint would not admit
      + nickname  string optional       safe

Plan: 0 to create, 1 to change · 1 safe, 1 physical-safe, 1 breaking
```

Two rules shape it. The **live catalog is the baseline** — no state file to drift out of sync, so
a table someone changed out of band shows up honestly. And **Loom never proposes a drop**: an
objectType maps a subset of a table's columns, so a column no property mentions is someone else's
data, reported as unmanaged and left alone.

## Applying it

`loom apply` executes that same plan — it prints it first, then asks — and creates the namespaces
it needs along the way, so an empty warehouse becomes a working ontology with no seed script:

```
$ loom apply examples/retail/ontology
Loom plan — examples/retail/ontology
...
Plan: 2 to create, 0 to change · 8 safe

Apply these changes? [y/N] y

  + local.crm.customers — created · namespace 'crm' created
  + local.sales.orders — created · namespace 'sales' created
Applied 2 table change(s). Recorded as version 1 in `_loom_meta` (local).
```

Run it again and it has nothing to do — the diff is re-derived from the live catalog every time,
so idempotency isn't a bookkeeping trick, it's the same mechanism that makes `plan` honest:

```
$ loom apply examples/retail/ontology
No changes — the catalog already matches the ontology.

Already applied — nothing to do. Recorded as version 1 in `_loom_meta` (local).
```

Three rules shape the executor:

- **A breaking plan is refused whole**, and nothing runs — not even the safe tables in it. The
  fix for a breaking change is a data migration (add the column nullable, backfill, then tighten),
  and there is no `--force`, because forcing it wouldn't make it safe.
- **One table, one Iceberg transaction.** That is Iceberg's unit of atomicity, so it is Loom's:
  a table's column changes and its provenance properties commit together. Across tables the run
  is sequential and stops at the first failure, and says exactly which tables landed — an honest
  partial beats a pretend-atomic one.
- **Writes go through their own port.** The resolver, the query engines and `loom serve` hold a
  read-only `Catalog` and could not execute DDL if they tried; only `apply` asks for a
  `CatalogWriter`.

Every apply appends to `_loom_meta.applied`, an ordinary Iceberg table in the lake: the spec's
source, its content hash, a version, who ran it, and what it did. It lives in the lake rather than
beside the YAML because a state file only ever describes the checkout it sits in — and it is
history, never the planner's input.

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
