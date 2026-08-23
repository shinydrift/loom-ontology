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
`run_upgrade_tier` to any MCP client — with input schemas derived from the property/parameter
types, and row/column governance enforced below the tool layer.

*Today the reads, `traverse` and the `run_` tools are all live over MCP. Writes are off by default:
`mcp.writes: true` in `loom.yaml` is what turns a declared action into a tool, because declaring one
and serving it to every client that connects are different decisions. Governance withholds both
halves now: a `governance.policies` entry withholds a property from every caller and filters the
rows every caller sees — `loom query` included, because the mask is applied to the projection and
the predicate to the table, below both of them. See [Status](#status).*

## Architecture (5 layers)

```
5. Agent / MCP layer       ← LLMs call the ontology as typed verbs (never raw SQL)
4. Action & function layer ← single-object writeback via the Iceberg catalog, optimistic concurrency, audited
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
  across engines. They also go through their own ports: one for a table's *shape*, one for its
  *rows*, and one for Loom's own record, so a migration cannot touch data and an action cannot
  touch DDL — nor name a table through the port it records itself with.

## Status

Early, but the **read path works end to end**: a YAML spec over real Iceberg tables, served to an MCP
client as typed tools. The **migration path** is complete — `loom plan` classifies what a spec change
would do to the physical tables, `loom apply` executes it, and `loom rollback` puts an earlier spec
back. The **action runtime** writes: one row, as one Iceberg commit, with the snapshot its read saw
asserted *inside* that commit, so a competing write refuses the run rather than silently losing to
it — and every run that named a row is recorded in `_loom_meta.edits`, **including the ones that
refused**. `loom serve` speaks **stdio and HTTP**, and serves writes only where a deployment asked
for them. **Governance** withholds columns and rows below the tool layer, so `loom query` is filtered
exactly as an agent is, and `mcp.auth` gives a policy an attested caller to name. Filters are **fully
typed**. A **bulk load** lands through a declared entry and the lake records it. **Semantic search**
is the milestone in progress.

Each of those sentences was a decision with an argument behind it, and the arguments are the point:
[`docs/status.md`](docs/status.md) is that story milestone by milestone.

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
| `renamedFrom` — column renames as field-id remaps | ✅ |
| Migration rollback | ✅ `loom rollback` |
| Action runtime — single-object writeback (`action/`) | ✅ `loom run` |
| Optimistic concurrency — snapshot check | ✅ asserted inside the commit |
| Edit-log (audit) table | ✅ `_loom_meta.edits` |
| MCP `run_<action>` tools | ✅ `mcp.writes: true` |
| HTTP transport | ✅ `mcp.transport: http` |
| Capability negotiation | ✅ refused at wiring, not narrowed |
| Governance — column masking | ✅ `governance.policies` |
| Governance — row predicates | ✅ `rows:`, compiled on one plane and evaluated on the other |
| Governance — the edit log under a policy | ✅ `governance.edit_log: required` (retention refused) |
| Attested identity over MCP | ✅ `mcp.auth` — a bearer token this deployment checked |
| Governance — policies that name the caller | ✅ `when:` and `principal.<claim>`, rows only |
| Fully typed object filters | ✅ `filter: {salesDate: {gte: …, lt: …}}`, scalars, ANDed |
| Membership filters | ✅ `filter: {tier: {in: [...]}}` — null-safe, empty list refused |
| Bulk ingest — a declared load, checked and recorded | ✅ `loom ingest`, `_loom_meta.loads` |
| Semantic search — the grammar | ✅ `semantic:`, `mcp.embedding`, `vector_search` negotiated |
| Semantic search — the vectors | ✅ `loom embed`, a sidecar per type in `_loom_meta` |
| Semantic search — a tool that ranks | ✅ `match_<object>(text, filter, page)`, brute force, filtered first |
| Semantic search — ranking across a link (`via`) | 🔨 M10 slice 4 |
| Drafting a spec from a file | ✅ `loom infer` — parquet, writes nothing, does not validate |
| Ordered loads | ✅ `sequences:` + `loom sequence` — stops at the first refusal, `_loom_meta.sequences` |

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,iceberg,duckdb,mcp]"

pytest                              # 1091 tests
loom validate tests/fixtures/valid  # → ok — 2 object type(s), 1 link type(s), 3 action(s)
```

Then run the whole stack against a real Iceberg warehouse — `examples/retail` ships the worked
example plus a seed script that builds one locally, with no services to start:

```bash
python examples/retail/seed.py                        # build the warehouse
loom query Customer examples/retail/ontology --key c1 # → one row, through DuckDB
loom run upgradeTier examples/retail/ontology \
  --param customer=c3 --param newTier=gold            # → one row rewritten, one row recorded
loom serve examples/retail/ontology                   # → 10 MCP tools over stdio
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

## Documentation

| | |
|---|---|
| [**The guide**](docs/guide/) | The command-by-command tour — [quickstart](docs/guide/quickstart.md), [drafting a spec](docs/guide/drafting-a-spec.md), [migrations](docs/guide/migrations.md), [actions](docs/guide/actions.md), [bulk loads](docs/guide/loading-data.md), [an app on top](docs/guide/dashboard.md) |
| [**`docs/spec-v0.md`**](docs/spec-v0.md) | The full YAML grammar — the framework's public contract |
| [**`docs/ROADMAP.md`**](docs/ROADMAP.md) | What is built and what is next, milestone by milestone |
| [**`docs/status.md`**](docs/status.md) | The same milestones as prose: what each one decided, and why |
| [**`examples/`**](examples/README.md) | The retail ontology, its seed script, and the dashboard in front of it |

## License

MIT — see [LICENSE](LICENSE).
