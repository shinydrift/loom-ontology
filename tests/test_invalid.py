"""Each case writes a minimal spec and asserts the validator reports the expected problem.
The validator accumulates all errors, so we assert a substring appears among them."""

import textwrap

import pytest

from loom import build
from loom.errors import SpecErrors

# A reusable, valid Customer other files can reference.
CUSTOMER = """
objectType:
  apiName: Customer
  primaryKey: customerId
  backing: { catalog: c, table: crm.customers }
  properties:
    - { name: customerId, type: string, column: id, unique: true }
    - { name: tier, type: enum, values: [bronze, gold], column: tier }
"""


def _build(tmp_path, files: dict[str, str]):
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body))
    return build(tmp_path)


def _expect_error(tmp_path, files, substring):
    with pytest.raises(SpecErrors) as ei:
        _build(tmp_path, files)
    messages = "\n".join(e.render() for e in ei.value.errors)
    assert substring in messages, f"expected {substring!r} in:\n{messages}"


def test_unknown_key(tmp_path):
    _expect_error(tmp_path, {"o.yaml": """
        objectType:
          apiName: Foo
          primryKey: id
          backing: { catalog: c, table: t.foo }
          properties: [{ name: id, type: string, column: id, unique: true }]
    """}, "unexpected key 'primryKey'")


def test_primary_key_not_a_property(tmp_path):
    _expect_error(tmp_path, {"o.yaml": """
        objectType:
          apiName: Foo
          primaryKey: missing
          backing: { catalog: c, table: t.foo }
          properties: [{ name: id, type: string, column: id, unique: true }]
    """}, "primaryKey 'missing' is not a declared property")


def test_primary_key_must_be_unique(tmp_path):
    _expect_error(tmp_path, {"o.yaml": """
        objectType:
          apiName: Foo
          primaryKey: id
          backing: { catalog: c, table: t.foo }
          properties: [{ name: id, type: string, column: id }]
    """}, "must be declared unique")


def test_primary_key_not_nullable(tmp_path):
    _expect_error(tmp_path, {"o.yaml": """
        objectType:
          apiName: Foo
          primaryKey: id
          backing: { catalog: c, table: t.foo }
          properties: [{ name: id, type: string, column: id, unique: true, nullable: true }]
    """}, "must not be nullable")


def test_enum_requires_values(tmp_path):
    _expect_error(tmp_path, {"o.yaml": """
        objectType:
          apiName: Foo
          primaryKey: id
          backing: { catalog: c, table: t.foo }
          properties:
            - { name: id, type: string, column: id, unique: true }
            - { name: kind, type: enum, column: kind }
    """}, "requires a non-empty 'values'")


def test_duplicate_column(tmp_path):
    _expect_error(tmp_path, {"o.yaml": """
        objectType:
          apiName: Foo
          primaryKey: id
          backing: { catalog: c, table: t.foo }
          properties:
            - { name: id, type: string, column: id, unique: true }
            - { name: other, type: string, column: id }
    """}, "mapped by both")


def test_link_unknown_object(tmp_path):
    _expect_error(tmp_path, {
        "customer.yaml": CUSTOMER,
        "link.yaml": """
        linkType:
          apiName: placedBy
          cardinality: many_to_one
          from: { objectType: Order, property: customerId }
          to: { objectType: Customer, property: customerId }
    """}, "from.objectType 'Order' does not exist")


def test_link_join_type_mismatch(tmp_path):
    _expect_error(tmp_path, {
        "customer.yaml": CUSTOMER,
        "order.yaml": """
        objectType:
          apiName: Order
          primaryKey: orderId
          backing: { catalog: c, table: sales.orders }
          properties:
            - { name: orderId, type: string, column: id, unique: true }
            - { name: custId, type: long, column: cust_id }
    """,
        "link.yaml": """
        linkType:
          apiName: placedBy
          cardinality: many_to_one
          from: { objectType: Order, property: custId }
          to: { objectType: Customer, property: customerId }
    """}, "join types differ")


def test_many_to_many_requires_through(tmp_path):
    _expect_error(tmp_path, {
        "customer.yaml": CUSTOMER,
        "link.yaml": """
        linkType:
          apiName: knows
          cardinality: many_to_many
          from: { objectType: Customer, property: customerId }
          to: { objectType: Customer, property: customerId }
    """}, "requires a 'through'")


def test_through_on_non_m2m(tmp_path):
    _expect_error(tmp_path, {
        "customer.yaml": CUSTOMER,
        "link.yaml": """
        linkType:
          apiName: knows
          cardinality: many_to_one
          from: { objectType: Customer, property: customerId }
          to: { objectType: Customer, property: customerId }
          through: { catalog: c, table: t.j, fromColumn: a, toColumn: b }
    """}, "only valid for many_to_many")


def test_action_unknown_target(tmp_path):
    _expect_error(tmp_path, {"a.yaml": """
        action:
          apiName: doThing
          description: does a thing
          targetObjectType: Ghost
          operation: modify
          parameters: [{ name: key, type: string }]
          effects: [{ modifyObject: { key: "{{ key }}", set: {} } }]
    """}, "targetObjectType 'Ghost' does not exist")


def test_action_multiple_effects_rejected(tmp_path):
    _expect_error(tmp_path, {
        "customer.yaml": CUSTOMER,
        "a.yaml": """
        action:
          apiName: doThing
          description: does a thing
          targetObjectType: Customer
          operation: modify
          parameters: [{ name: key, type: string }]
          effects:
            - { modifyObject: { key: "{{ key }}", set: {} } }
            - { modifyObject: { key: "{{ key }}", set: {} } }
    """}, "exactly one entry")


def test_effect_set_unknown_property(tmp_path):
    _expect_error(tmp_path, {
        "customer.yaml": CUSTOMER,
        "a.yaml": """
        action:
          apiName: doThing
          description: does a thing
          targetObjectType: Customer
          operation: modify
          parameters: [{ name: key, type: string }]
          effects: [{ modifyObject: { key: "{{ key }}", set: { bogus: "{{ key }}" } } }]
    """}, "is not a property of 'Customer'")


def test_expr_unknown_parameter(tmp_path):
    _expect_error(tmp_path, {
        "customer.yaml": CUSTOMER,
        "a.yaml": """
        action:
          apiName: doThing
          description: does a thing
          targetObjectType: Customer
          operation: modify
          parameters: [{ name: key, type: string }]
          validation: [{ rule: "ghost == 1", message: nope }]
          effects: [{ modifyObject: { key: "{{ key }}", set: {} } }]
    """}, "unknown parameter 'ghost'")


def test_a_rule_naming_the_caller_is_refused_because_an_ontology_cannot_see_one(tmp_path):
    """**The one reference form an ontology may not use**, and the refusal is what keeps a spec
    deployment-blind.

    `principal.<claim>` is declared in `loom.yaml` (`mcp.auth.claims`), beside the issuer that mints
    it, so it is in scope for a governance policy there and nowhere else — the language's own rule
    that a reference is legal where its declaration is. A rule naming a caller would also put
    authorization inside a validation rule: what a caller may do is `mcp.writes` and `governance`,
    in the file a deployment is configured by."""
    _expect_error(tmp_path, {
        "customer.yaml": CUSTOMER,
        "a.yaml": """
        action:
          apiName: doThing
          description: does a thing
          targetObjectType: Customer
          operation: modify
          parameters: [{ name: key, type: string }]
          validation: [{ rule: "principal.dept == 'hr'", message: nope }]
          effects: [{ modifyObject: { key: "{{ key }}", set: {} } }]
    """}, "names the caller, which an ontology cannot see")


def test_contains_is_refused_in_an_ontology_because_no_property_is_a_list(tmp_path):
    """The operator the language gained for a list-valued *claim*, refused where it can never be
    satisfied.

    Inferring it as "unknown type" — which is this checker's optimistic default — would accept a
    rule that fails on every run with an `expression_error`, which is the accepted-and-unenforced
    shape this codebase refuses everywhere. It becomes writable here the day `array` lands as a
    property type."""
    _expect_error(tmp_path, {
        "customer.yaml": CUSTOMER,
        "a.yaml": """
        action:
          apiName: doThing
          description: does a thing
          targetObjectType: Customer
          operation: modify
          parameters: [{ name: key, type: string }]
          validation: [{ rule: "object.tier contains 'go'", message: nope }]
          effects: [{ modifyObject: { key: "{{ key }}", set: {} } }]
    """}, "'contains' cannot be used in action 'doThing'")


def test_object_ref_in_create_rejected(tmp_path):
    _expect_error(tmp_path, {"a.yaml": """
        objectType: &noop
          apiName: Widget
          primaryKey: id
          backing: { catalog: c, table: t.w }
          properties: [{ name: id, type: string, column: id, unique: true }]
    """,
        "create.yaml": """
        action:
          apiName: mk
          description: make a widget
          targetObjectType: Widget
          operation: create
          parameters: [{ name: id, type: string }]
          effects: [{ createObject: { set: { id: "object.id" } } }]
    """}, "no current object")


def test_create_missing_required(tmp_path):
    _expect_error(tmp_path, {
        "order.yaml": """
        objectType:
          apiName: Order
          primaryKey: orderId
          backing: { catalog: c, table: sales.orders }
          properties:
            - { name: orderId, type: string, column: id, unique: true }
            - { name: total, type: double, column: total }
    """,
        "a.yaml": """
        action:
          apiName: mk
          description: make an order
          targetObjectType: Order
          operation: create
          parameters: [{ name: orderId, type: string }]
          effects: [{ createObject: { set: { orderId: "{{ orderId }}" } } }]
    """}, "must set required properties: total")


def test_unknown_function(tmp_path):
    _expect_error(tmp_path, {
        "customer.yaml": CUSTOMER,
        "a.yaml": """
        action:
          apiName: doThing
          description: does a thing
          targetObjectType: Customer
          operation: modify
          parameters: [{ name: key, type: string }]
          validation: [{ rule: "frobnicate(key) == 1", message: nope }]
          effects: [{ modifyObject: { key: "{{ key }}", set: {} } }]
    """}, "unknown function 'frobnicate()'")


def test_non_boolean_validation_rule(tmp_path):
    _expect_error(tmp_path, {
        "customer.yaml": CUSTOMER,
        "a.yaml": """
        action:
          apiName: doThing
          description: does a thing
          targetObjectType: Customer
          operation: modify
          parameters: [{ name: key, type: string }]
          validation: [{ rule: "key", message: nope }]
          effects: [{ modifyObject: { key: "{{ key }}", set: {} } }]
    """}, "must be boolean")


def test_duplicate_api_name(tmp_path):
    _expect_error(tmp_path, {
        "a.yaml": CUSTOMER,
        "b.yaml": CUSTOMER,
    }, "duplicate objectType apiName 'Customer'")


def test_file_with_two_kinds_rejected(tmp_path):
    _expect_error(tmp_path, {"bad.yaml": """
        objectType:
          apiName: Foo
          primaryKey: id
          backing: { catalog: c, table: t.foo }
          properties: [{ name: id, type: string, column: id, unique: true }]
        linkType:
          apiName: l
          cardinality: many_to_one
          from: { objectType: Foo, property: id }
          to: { objectType: Foo, property: id }
    """}, "exactly one of")
