[← Spec index](../spec-v0.md)

# 2. `objectType`

A semantic entity bound to one Iceberg table.

```yaml
objectType:
  apiName: Customer              # required · PascalCase · unique
  displayName: Customer          # optional · default = apiName
  description: A buying account   # optional · surfaced to the LLM
  primaryKey: customerId          # required · must name a property below
  title: name                     # optional · property used as human label · default = primaryKey
  status: active                  # optional · active | deprecated | experimental · default active

  backing:                        # required
    catalog: rest_main            # required · a catalog declared in loom.yaml
    table: crm.customers          # required · Iceberg identifier "namespace.table"

  properties:                     # required · non-empty
    - name: customerId            # required · camelCase · unique within object
      type: string                # required · from §1
      column: id                  # required · physical Iceberg column
      nullable: false             # optional · default false
      unique: true                # optional · default false (advisory unless it's the PK)
      description: Stable id        # optional

    - name: name
      type: string
      column: full_name

    - name: tier
      type: enum
      values: [bronze, silver, gold]   # required iff type == enum · non-empty · unique
      column: tier

    - name: ltv
      type: double
      column: lifetime_value
      renamedFrom: legacy_ltv     # optional · the column `column` used to be — see below
      nullable: true

  searchable: [name, tier]        # optional · the properties search_<type> can filter on
  semantic: bio                   # optional · the one property match_<type> ranks by meaning
```

**Validation rules**

1. `apiName` matches the PascalCase pattern and is unique among object types.
2. `primaryKey` names an existing property; that property must be `nullable: false` **and**
   `unique: true` (the framework sets `unique: true` implicitly for the PK and errors if you
   declared it `false`).
3. `title` names an existing property.
4. Property `name`s are unique within the object; `column`s are unique within the object.
5. `enum` properties declare a non-empty, duplicate-free `values` list. `decimal` properties
   declare `precision` (≥1) and `scale` (0 ≤ scale ≤ precision).
6. `searchable` entries name existing properties. **Any type**, since §7.1: the list decides what
   `search_<type>` may filter on, and every scalar has comparisons worth offering. It was
   string-or-enum while a filter could only say equality and substring, and lifting that is a
   widening of what an author *may declare* rather than of any surface — a property still has to be
   listed here to be filterable at all, and the word still means "substring" for a string.
7. `semantic` (if set) names an existing property of type **`string`**, and it is a *name* rather
   than a list — one property, so the key says one. Narrower than `searchable` on purpose: rule 6
   widened that to every scalar because every scalar has comparisons worth offering, and the
   opposite is true here. An ordered type already has an order, so `gte` says exactly what a
   similarity score could only approximate; an `enum` is a closed set, so `eq`/`in` answer it
   exactly. Embedding either buys a fuzzy answer to a question that already has a precise one.
   The two lists are independent — a property may be searchable, semantic, both or neither.

   Declaring it demands `vector_search` of the engine (§6.3), and it is the *spec* that demands it:
   an ontology whose engine has no array arithmetic describes a surface that engine could never
   serve, whatever any deployment configures. It generates `match_<type>` (§7, §7.2) wherever the
   deployment also configures `mcp.embedding` — the spec declares the intent, the deployment
   declares the mechanism, and neither half alone is a ranked surface.
8. **Physical check** (at `loom plan` / `loom serve`, against catalog introspection): every
   `backing.table` exists, every `column` exists on it, and every property type is compatible
   with its column's Iceberg type per §1. Missing table/column → error; incompatible type →
   error; extra columns on the table that no property maps → warning (not an error).
9. `renamedFrom` (if set) is a non-empty column name and is **not** the property's own `column`.
10. No property's `renamedFrom` may equal any property's `column` **on the same table** — including
   columns contributed by a *different* declaration bound to that table. This is what makes
   renames independent of one another: neither a chain (`a→b` and `b→c`) nor a swap is
   expressible, which is in turn what lets the planner order edits per column.
11. Two columns on one table may not declare the same `renamedFrom`. One column cannot become two.
12. Two declarations mapping the same column may not name *different* `renamedFrom` sources.
    Silence is **no opinion**, not "there was no rename", so only one of them has to say it.

## 2.1 `renamedFrom` — a moved column, not a new one

Without it, changing a property's `column` reads to the planner as "add the new one" and leaves
the old one sitting there as unmanaged data. `renamedFrom` says the two are the same column, so
`apply` issues an Iceberg **rename** — the field id is unchanged, so no data file is rewritten and
nothing is stranded.

The spec says what it wants; **the live catalog decides what that currently means.** The same
property, unchanged, plans four different ways:

| live table has | `loom plan` |
|---|---|
| the old column only | the rename · **safe** |
| the new column only | nothing — the rename already landed |
| neither | a warning, then an ordinary `add` of the new column |
| **both** | the rename · **breaking**, and `apply` refuses the whole plan |

*Both* is the only loud one. A rename target that already exists is either a mistake or a
half-finished migration, and Loom cannot merge the two columns because merging means dropping one.
It is a breaking *change* rather than a load error on purpose: the spec is fine, the lake is in a
shape this plan can't resolve — so the rest of the diff still prints, and the refusal comes from
the same whole-plan machinery as every other breaking change. Fix it by moving the values across
and dropping `renamedFrom`, or by removing the old column out of band.

A rename is classified **safe**, not physical-safe. Physical-safe is the label that means *the
stored type moved, and existing files only still read because Iceberg addresses columns by field
id*. A rename moves nothing: field id, type and nullability all survive. What it does break is
readers **outside** the ontology that select the column by name — a contract change rather than a
data-safety one, so it is stated in the plan's reason line rather than raised to a louder severity.

If a rename is combined with a promotion or a loosening on the same column, they are one
`alter_table` — one Iceberg transaction — and the rename is ordered first.

**Lifetime.** `renamedFrom` outlives its migration and Loom will never tell you to remove it. One
spec is deployed to more than one lake, and after the rename ships to production, staging and every
fresh developer warehouse are still on the other side of it; "you can delete this now" would be
true of one catalog and false of another *from the same file*. So it stays, planning as a clean
no-op wherever it is spent. When it is safe to delete is a question `_loom_meta` answers (§9): it
records the version that performed the rename.

**Chains are deliberately not expressible.** `renamedFrom` is one hop. A chain `a→b→c` only ever
arises across separate applies — within a single edit, `column: c, renamedFrom: a` is how you say
it — and the lake that would need the chain is one that skipped an intermediate apply, where the
answer is to apply the intermediate versions rather than to teach one spec every name a column has
ever had. A list would also make the *both* case combinatorial. A lake that skipped a hop lands in
the *neither* row above: warned, not silently stranded.

---

[← §0–§1 Conventions and the type system](./00-conventions-and-types.md) · [§3 `linkType` →](./03-link-type.md)
