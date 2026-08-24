[← Spec index](../spec-v0.md)

# 8. Worked example — a complete, valid mini-ontology

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
    - { rule: "newTier != object.tier", message: New tier must differ from current tier }
  effects:
    - modifyObject:
        key: "{{ customer }}"
        set: { tier: "{{ newTier }}" }
```

This validates clean, migrates two Iceberg tables, resolves `Customer.orders` as a reverse
JOIN, and exposes `get_customer`, `search_customer`, `list_customer`, `get_order`,
`search_order`, `list_order`, `traverse` and — where the deployment sets `mcp.writes` —
`run_upgrade_tier` over MCP, all from ~40 lines of YAML.

---

[← §7 What the grammar compiles to](./07-compilation.md) · [§9 `_loom_meta` →](./09-loom-meta.md)
