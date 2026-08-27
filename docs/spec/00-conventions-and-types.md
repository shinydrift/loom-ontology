[← Spec index](../spec-v0.md)

# Conventions and the canonical type system

## 0. Conventions

- **Files.** Ontology lives under `ontology/**/*.yaml`. Each file declares exactly **one**
  top-level kind: `objectType`, `linkType`, or `action`. Split freely across directories;
  the loader globs and merges.
- **Identifiers.** Everything references everything else by `apiName` (never by file path).
  - Object types: `PascalCase`, match `^[A-Z][A-Za-z0-9]*$`
  - Links / actions / properties / parameters: `camelCase`, match `^[a-z][A-Za-z0-9]*$`
  - `apiName` is **globally unique within its kind** (object namespace, link namespace,
    action namespace are separate).
  - These patterns are checked by `loom validate`, and the property half is load-bearing rather
    than cosmetic: §5's expression grammar can only *name* an identifier of this shape, so a
    property called `full Name` is one no `rows:` predicate and no action `validation:` rule could
    ever mention — while `mask:`, a list of strings rather than an expression, would still reach
    it. Uniqueness within a kind is not sufficient on its own either; see §7 on tool names.
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
| `objectRef`    | (referenced PK)   | `{type: string}`                           | **parameters only**; requires `objectType`; resolved only as a target key (§4.1) |

Deferred to a later version (call it out rather than pretend): `array<T>`, `struct`, `map`,
`geo`. Adding one = one new row here + adapter lowering; that's the extension shape.

**Type compatibility** (property `type` vs. its backing Iceberg column) follows Iceberg's own
promotion rules: `int→long`, `float→double`, `decimal(P,S)→decimal(P',S)` with `P'≥P` are
compatible; anything narrowing is a validation error.

---

[§2 `objectType` →](./02-object-type.md)
