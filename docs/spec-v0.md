# Loom Ontology Spec — v0 Grammar

> The framework's public contract. Every Loom surface — Iceberg schema/migrations, the
> query resolver, the action runtime, and the MCP tool registry — is **compiled from these
> files**. If it isn't expressible here, the framework can't do it. Keep this small and honest.

---

## 0. Conventions

- **Files.** Ontology lives under `ontology/**/*.yaml`. Each file declares exactly **one**
  top-level kind: `objectType`, `linkType`, or `action`. Split freely across directories;
  the loader globs and merges.
- **Identifiers.** Everything references everything else by `apiName` (never by file path).
  - Object types: `PascalCase`, match `^[A-Z][A-Za-z0-9]*$`
  - Links / actions / properties / parameters: `camelCase`, match `^[a-z][A-Za-z0-9]*$`
  - `apiName` is **globally unique within its kind** (object namespace, link namespace,
    action namespace are separate).
- **Unknown keys are errors**, not ignored. A typo like `primryKey` fails `loom validate`
  with a "did you mean" — a spec language that silently drops fields rots.
- **Everything compiles or nothing loads.** `loom validate` runs the full ruleset below and
  either produces a consistent Ontology Model or exits non-zero. There is no partial load.

---

## 1. Canonical type system

The one table the whole framework leans on: each Loom type simultaneously knows its Iceberg
type (drives DDL + physical validation) and its JSON-Schema shape (drives the MCP tool
contract). A property/parameter `type` is one of:

| Loom type      | Iceberg type      | JSON Schema (MCP)                          | Notes |
|----------------|-------------------|--------------------------------------------|-------|
| `string`       | `string`          | `{type: string}`                           | |
| `boolean`      | `boolean`         | `{type: boolean}`                          | |
| `int`          | `int`             | `{type: integer, format: int32}`           | |
| `long`         | `long`            | `{type: integer, format: int64}`           | |
| `double`       | `double`          | `{type: number}`                           | |
| `decimal`      | `decimal(P,S)`    | `{type: string, pattern: …}`               | requires `precision`, `scale` |
| `date`         | `date`            | `{type: string, format: date}`             | |
| `timestamp`    | `timestamptz`     | `{type: string, format: date-time}`        | tz-aware; UTC on the wire |
| `enum`         | `string` + check  | `{type: string, enum: [...]}`              | requires `values` |
| `objectRef`    | (referenced PK)   | `{type: string}`                           | **parameters only**; requires `objectType` |

Deferred to a later version (call it out rather than pretend): `array<T>`, `struct`, `map`,
`geo`. Adding one = one new row here + adapter lowering; that's the extension shape.

**Type compatibility** (property `type` vs. its backing Iceberg column) follows Iceberg's own
promotion rules: `int→long`, `float→double`, `decimal(P,S)→decimal(P',S)` with `P'≥P` are
compatible; anything narrowing is a validation error.

---

## 2. `objectType`

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
      nullable: true

  searchable: [name, tier]        # optional · property names powering search_<type>
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
6. `searchable` entries name existing properties whose type is `string` or `enum`.
7. **Physical check** (at `loom plan` / `loom serve`, against catalog introspection): every
   `backing.table` exists, every `column` exists on it, and every property type is compatible
   with its column's Iceberg type per §1. Missing table/column → error; incompatible type →
   error; extra columns on the table that no property maps → warning (not an error).

---

## 3. `linkType`

A relationship between two object types. FK-style links need no physical table (they compile
to a JOIN); many-to-many needs a `through` mapping table.

```yaml
linkType:
  apiName: placedBy               # required · camelCase · unique among links
  displayName: Placed by
  description: The customer who placed this order
  cardinality: many_to_one        # required · one_to_one | one_to_many | many_to_one | many_to_many
  from:
    objectType: Order             # required · existing object type
    property: customerId          # required · join property on `from`
  to:
    objectType: Customer          # required · existing object type
    property: customerId          # required · join property on `to`
  reverseName: orders             # optional · backref traversal exposed on the `to` object
  status: active

  # required ONLY for cardinality: many_to_many — otherwise must be absent
  through:
    catalog: rest_main
    table: crm.order_customer
    fromColumn: order_id
    toColumn: customer_id
```

**Validation rules**

1. `from.objectType` and `to.objectType` exist.
2. `from.property` and `to.property` exist on their respective object types, and their
   canonical types are comparable (equal after promotion).
3. `through` is **required iff** `cardinality == many_to_many`, and **forbidden otherwise**.
   When present, its columns are physically checked like any backing table.
4. `reverseName` (if set) collides with nothing on the `to` object — not a property name, not
   another link's `apiName`, not another link's `reverseName`.
5. **Cardinality sanity** (advisory warnings, not errors): for `many_to_one`, `to.property`
   should be unique (it's the "one" side); for `one_to_many`, `from.property` should be unique.
   Loom warns on a likely-mismodeled join rather than silently producing fan-out.

---

## 4. `action`

The kinetic layer. **v1 is single-object**: an action mutates exactly one instance of its
`targetObjectType`, producing one atomic Iceberg commit. This constraint is enforced here, at
spec-load — not discovered at runtime.

```yaml
action:
  apiName: upgradeTier            # required · camelCase · unique among actions
  displayName: Upgrade tier
  description: Raise a customer to a higher membership tier   # required · this IS the MCP tool description
  targetObjectType: Customer      # required · existing object type
  operation: modify               # required · create | modify | delete
  status: active

  parameters:
    - name: customer              # required · camelCase · unique within action
      type: objectRef             # from §1
      objectType: Customer        # required iff type == objectRef
      required: true              # optional · default true
      description: Customer to upgrade
    - name: newTier
      type: enum
      values: [silver, gold]      # required iff enum
      required: true

  validation:                     # optional · preconditions, checked before the write
    - rule: "newTier != customer.tier"
      message: New tier must differ from the current tier

  effects:                        # required · exactly one entry (single-object)
    - modifyObject:
        key: "{{ customer }}"     # expression → PK of the target object
        set:
          tier: "{{ newTier }}"   # propertyName: valueExpr
```

**Effect grammar by `operation`** (exactly one effect entry, and its kind must match
`operation`):

```yaml
# operation: create
effects:
  - createObject:
      set: { <propertyName>: <expr>, ... }   # must cover primaryKey + every non-nullable, non-defaulted property

# operation: modify
effects:
  - modifyObject:
      key: <expr>                             # → target PK
      set: { <propertyName>: <expr>, ... }

# operation: delete
effects:
  - deleteObject:
      key: <expr>                             # → target PK
```

**Validation rules** (the boundary-keeping ones matter most)

1. `targetObjectType` exists. `operation` matches the single effect kind
   (`create→createObject`, `modify→modifyObject`, `delete→deleteObject`).
2. **Single-object boundary:** exactly one effect entry, and it targets `targetObjectType`
   only. Any effect that references a second object type is **rejected at load** — this is the
   v1 scope wall, made a hard error, not a convention.
3. Every `set` key names a real property on `targetObjectType`; each value `<expr>`
   type-checks to that property's type.
4. `create`: `set` must cover the `primaryKey` and every property that is `nullable: false`
   without a `default`. `modify`/`delete`: `key` `<expr>` resolves to the PK's type.
5. Parameters: `objectRef` params name an existing `objectType`; `enum` params declare
   `values`; a `default` (if present) type-checks and implies `required: false`.
6. `validation[].rule` and every `<expr>` reference **only** declared parameters and
   properties of the target object (see §5) — no free variables.
7. **Concurrency is implicit:** `modify`/`delete` are read-modify-write under optimistic
   concurrency (version/snapshot check). No YAML expresses it; the runtime always does it.

---

## 5. Expression mini-language

Deliberately tiny so it stays portable across engines and safe to evaluate. Used in
`validation[].rule` (must yield boolean) and effect value positions (must yield the target
property's type). Written inline or inside `{{ … }}`.

- **References:** a bare identifier `paramName` resolves to a parameter; `object.propName`
  resolves to the *current* value of the target object's property (for `modify`/`delete`).
- **Literals:** string `'...'`, number, `true`/`false`/`null`, and bare enum values.
- **Operators:** comparison `== != < <= > >=`, boolean `&& || !`, arithmetic `+ - * /`,
  string `+` (concat).
- **Function allow-list (only these):** `now()`, `lower(s)`, `upper(s)`, `len(s)`,
  `coalesce(a, b, …)`.
- **No** loops, lambdas, property assignment, external calls, or arbitrary code. Anything
  richer belongs in a future custom-function extension point, not the expression language.

---

## 6. Project config — `loom.yaml`

Not part of the ontology, but the grammar the ontology's `catalog:` / engine references
resolve against.

```yaml
version: 0
catalogs:
  rest_main:
    type: iceberg-rest            # v1 catalog type
    uri: https://catalog.internal/api
    warehouse: s3://lake/warehouse
    auth: { type: oauth2, ... }   # opaque to the spec; passed to the catalog client
engine:
  type: duckdb                    # duckdb | trino | spark  (read path; §7 of the design)
  options: {}                     # engine-specific
mcp:
  name: loom
  transport: stdio                # stdio | http
governance:                       # optional · row/column policies enforced in the resolver
  policies: []                    # (grammar TBD — forward reference, not in v0)
```

---

## 7. What the grammar compiles to (deterministic)

So the contract is complete on both ends, here's the fixed mapping from spec → generated MCP
surface. Nothing here is hand-authored.

| Spec element                         | Generated MCP tool(s)                                   | Input schema source |
|--------------------------------------|---------------------------------------------------------|---------------------|
| `objectType Customer`                | `get_customer(key)`                                     | PK type |
|                                      | `search_customer(filter, page)`                         | `searchable` props + property types |
|                                      | `list_customer(page)`                                   | pagination |
| `linkType placedBy` (+ `reverseName`)| contributes `order` / `customer` directions to `traverse(object, link, direction)` | link mapping |
| `action upgradeTier`                 | `run_upgradeTier(params…)`                              | `parameters` → JSON Schema; `description` → tool description |

Two invariants the compiler guarantees: **the LLM never receives raw SQL** (only these
verbs), and **governance policies filter both the direct API and the MCP tools identically**
(enforced in the resolver, below the MCP layer).

---

## 8. Worked example — a complete, valid mini-ontology

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
    - { name: ltv,        type: double, column: lifetime_value, nullable: true }
  searchable: [name, tier]
```
```yaml
# ontology/order.yaml
objectType:
  apiName: Order
  primaryKey: orderId
  title: orderId
  backing: { catalog: rest_main, table: sales.orders }
  properties:
    - { name: orderId,    type: string, column: id, unique: true }
    - { name: customerId, type: string, column: customer_id }
    - { name: total,      type: decimal, precision: 12, scale: 2, column: total_amount }
    - { name: placedAt,   type: timestamp, column: created_at }
  searchable: [orderId]
```
```yaml
# ontology/links/placed-by.yaml
linkType:
  apiName: placedBy
  cardinality: many_to_one
  from: { objectType: Order,    property: customerId }
  to:   { objectType: Customer, property: customerId }
  reverseName: orders
```
```yaml
# ontology/actions/upgrade-tier.yaml
action:
  apiName: upgradeTier
  description: Raise a customer to a higher membership tier
  targetObjectType: Customer
  operation: modify
  parameters:
    - { name: customer, type: objectRef, objectType: Customer }
    - { name: newTier,  type: enum, values: [silver, gold] }
  validation:
    - { rule: "newTier != customer.tier", message: New tier must differ from current tier }
  effects:
    - modifyObject:
        key: "{{ customer }}"
        set: { tier: "{{ newTier }}" }
```

This validates clean, migrates two Iceberg tables, resolves `Customer.orders` as a reverse
JOIN, and exposes `get_customer`, `search_customer`, `list_customer`, `get_order`,
`search_order`, `list_order`, `traverse`, and `run_upgradeTier` over MCP — all from ~40 lines
of YAML.

---

## 9. `_loom_meta` — what `apply` recorded

Part of the contract because it is a table in *your* lake, not an implementation detail: anything
with an Iceberg client can read it, and a later Loom must keep it readable.

One table per catalog the spec binds, created by the first `loom apply` that touches that catalog:

```
_loom_meta.applied
  version       long         required   # global to the spec, not to the catalog — see below
  applied_at    timestamptz  required
  content_hash  string                  # sha256 of the spec source, canonicalized
  spec          string                  # {relative path: file text} as JSON — what a rollback restores
  summary       string                  # JSON: the tables this apply created/altered in this catalog
  status        string                  # applied | partial
  loom_version  string
  actor         string                  # $LOOM_ACTOR, else the OS user
```

**Append-only.** The current state is the row with the highest `version`; everything before it is
history. Nothing rewrites a row.

**It is not the planner's input.** The diff is always taken against the live catalog, so a table
someone changed out of band shows up honestly instead of being masked by a state file that says
otherwise. This table answers a narrower question — *has this exact spec already been applied
here, and what did that apply do?*

**`version` counts applies of the spec, not of a catalog.** A spec spanning two catalogs writes a
row to each, both carrying the same version and the same `content_hash`, and each summarizing only
its own tables. There is no central place to hold that counter, so it is derived: one past the
highest version any bound catalog holds. A catalog added to a project at version 7 starts its
history at 7.

**`content_hash` covers the spec's YAML only** — not `loom.yaml`, which is deployment config, so
the same spec hashes identically against staging and production. An edit that changes no column
still records a new version: the stored `spec` is what a rollback restores, so it has to track the
file text, not just the physical shape.

Each managed table additionally carries three Iceberg table properties, set in the same
transaction as its schema change: `loom.managed`, `loom.spec_hash`, `loom.applied_version`. They
duplicate what this table records on purpose — a table should be self-describing without the
reader knowing `_loom_meta` exists.

---

## Open edges (v0 → v1)

Named deliberately so they're conscious deferrals, not gaps:

- **`governance.policies` grammar** — row/column predicates; forward-referenced in §6, not yet
  specified.
- **Composite primary keys** — v0 assumes a single-property PK. Multi-column PKs touch `key`
  expressions and `objectRef` encoding.
- **Complex types** — `array`/`struct`/`map` (see §1).
- **Computed / derived properties** — properties backed by an expression rather than a column.
- **Multi-object actions** — the explicit post-v1 feature the §4 boundary reserves room for.
