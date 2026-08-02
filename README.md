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
MCP client as typed tools. The **migration path** is now complete — `loom plan` classifies what a
spec change would do to the physical tables, `loom apply` executes it (bootstrapping an empty
warehouse from nothing but a spec), and `loom rollback` puts an earlier spec back. The action
runtime (row-level writeback) is next.

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
| Action runtime — single-object writeback | ⏳ |
| MCP `run_<action>` tools + HTTP transport | ⏳ |
| Governance (row/column policies) | ⏳ |

`docs/spec-v0.md` is the full grammar — the framework's public contract.
`docs/ROADMAP.md` tracks what's next, milestone by milestone.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,iceberg,duckdb,mcp]"

pytest                              # 290 tests
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

## Renaming a column

Change a property's `column` and the planner has no way to know the old one and the new one are
the same column: it adds the new one and leaves the old sitting there full of data. `renamedFrom`
says they're the same column, and the migration becomes an Iceberg **rename** — the field id is
unchanged, so nothing is rewritten and nothing is stranded:

```yaml
- { name: ltv, type: double, column: ltv_usd, renamedFrom: lifetime_value, nullable: true }
```

```
$ loom plan ./ontology
  ~ local.crm.customers — 1 change(s) · Customer
      ~ ltv_usd  renamed from lifetime_value  safe
          the column keeps field id 4, so no data file is rewritten; readers outside the
          ontology that select 'lifetime_value' by name will need updating
```

The spec states the intent; **the live catalog decides what it currently means.** That same
property, unchanged, plans four ways: the rename if only the old column is there, *nothing at all*
if only the new one is, a warning and a plain add if neither is — and a refusal if both are.

That last one is a rename target that already exists: a mistake, or a migration somebody finished
by hand halfway. Loom can't merge the two columns, because merging means dropping one:

```
$ loom apply ./ontology
  ! local.crm.customers — 1 change(s) · Customer
      ! ltv_usd  renamed from lifetime_value  breaking
          'lifetime_value' and 'ltv_usd' both exist in 'crm.customers' — Loom never drops a
          column, so it cannot merge them; move the values across and drop 'renamedFrom', or
          remove 'lifetime_value' out of band

refusing to apply: the plan contains breaking changes
  nothing was applied — no table is left half-migrated
```

Two things follow from the second row of that table. Applying is **idempotent for free** — the
rename lands, and every plan after it is clean without anything being ticked off in the spec.  And
`renamedFrom` **stays in the file afterwards**; Loom will never tell you to remove it, because one
spec is deployed to more than one lake, and after a rename ships to production, staging is still on
the other side of it. "You can delete this now" would be true of one catalog and false of another
from the same file. `_loom_meta` records which version did the rename, which is the honest place
for that answer.

## Rolling back

An apply that went wrong needs an answer other than hand-editing the YAML back to what it was.
`loom rollback` restores the spec `_loom_meta` recorded, re-plans it against the live catalog, and
executes that — the same loop as `apply`, over an older spec:

```
$ loom rollback ./ontology --to 1
Loom rollback — ./ontology
Restoring the spec recorded at version 1 (from local).
Rows are untouched — `apply` only ever ran DDL, so this only reverses DDL.

  ~ local.crm.customers — 1 change(s) · Customer
      ~ ltv_usd  renamed from lifetime_value  safe
          the column keeps field id 2, so no data file is rewritten; readers outside the
          ontology that select 'lifetime_value' by name will need updating

Plan: 0 to create, 1 to change · 1 safe

Left in place — a rollback never drops, so these stay live and unmanaged:
  · local.crm.customers: region — added after version 1

Spec files:
  ~ customer.yaml — restored
```

**Only renames actually reverse, and it says so rather than pretending otherwise.** Of the four
things Loom can do to a column, a rename is the one that undoes itself: the same field id comes
back under the old name, and no data file is rewritten. An add reverses to a *drop* — and Loom
never drops — so `region` above stays live, the restored spec no longer maps it, and it is
unmanaged from here on. That is the honest report, which is why it's printed rather than left to be
found later. A table created since is left whole for the same reason.

Reversing a promotion is a narrowing and reversing a loosening is a tightening, and both are
breaking, so those rollbacks are refused whole like any other breaking plan. That isn't a hole:
once the column is a `long`, the spec that says `int` no longer describes this lake, and the way
out is forward.

Two more things follow from `_loom_meta` being history rather than state. **A rollback is an
append** — a new row carrying the restored spec's text and hash, never a deleted one — so the next
`loom apply` sees a spec that is already live and does nothing. And the **reverse rename comes out
of that history**: `renamedFrom` points forward, so the version-1 spec can't name the column it has
to be renamed back from, but version 2 recorded what it renamed and rollback inverts it (composing
the chain if there were several).

**It reverses DDL and only DDL.** `apply` never wrote a row, so `rollback` never deletes one — no
snapshot rollback, no expiry. Rows written since are not Loom's to throw away. Spec files are the
last thing it writes and only if the run wasn't refused, so a rollback you decline leaves the lake
*and* the working tree exactly as they were.

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
