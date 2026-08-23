[← Spec index](../spec-v0.md)

# 3. `linkType`

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
    renamedFrom:                  # optional · §2.1, per side
      fromColumn: order_ref       # `order_id` used to be `order_ref`
```

**Validation rules**

1. `from.objectType` and `to.objectType` exist.
2. `from.property` and `to.property` exist on their respective object types, and their
   canonical types are comparable (equal after promotion).
3. `through` is **required iff** `cardinality == many_to_many`, and **forbidden otherwise**.
   When present, its columns are physically checked like any backing table — and planned like
   one, which is why `through.renamedFrom` exists: a mapping table is a real table, and leaving it
   out would make it the one table Loom plans but cannot rename a column on. Its keys are exactly
   `fromColumn` / `toColumn`, both optional, each following §2.1 and its rules.
4. `reverseName` (if set) collides with nothing on the `to` object — not a property name, not
   another link's `apiName`, not another link's `reverseName`.
5. **Cardinality sanity** (advisory warnings, not errors): for `many_to_one`, `to.property`
   should be unique (it's the "one" side); for `one_to_many`, `from.property` should be unique.
   Loom warns on a likely-mismodeled join rather than silently producing fan-out.

---

[← §2 `objectType`](./02-object-type.md) · [§4 `action` →](./04-action.md)
