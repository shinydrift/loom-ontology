from pathlib import Path

from loom import build

VALID = Path(__file__).parent / "fixtures" / "valid"


def test_worked_example_builds():
    ontology, diag = build(VALID)
    assert set(ontology.object_types) == {"Customer", "Order"}
    assert set(ontology.link_types) == {"placedBy"}
    # One action per operation, so the fixture exercises all three effect kinds.
    assert set(ontology.actions) == {"upgradeTier", "createOrder", "forgetCustomer"}
    assert diag.warnings == []  # many_to_one target is unique → no fan-out warning
    assert "2 object type(s)" in ontology.summary()


def test_object_model_shape():
    ontology, _ = build(VALID)
    cust = ontology.object_types["Customer"]
    assert cust.primary_key == "customerId"
    assert cust.pk_property.unique and not cust.pk_property.nullable
    assert cust.title == "name"
    assert cust.properties["tier"].type.kind == "enum"
    assert cust.properties["tier"].type.values == ("bronze", "silver", "gold")
    assert cust.properties["ltv"].nullable is True


def test_type_system_mappings():
    ontology, _ = build(VALID)
    order = ontology.object_types["Order"]
    assert order.properties["total"].type.iceberg_type() == "decimal(12,2)"
    assert order.properties["placedAt"].type.iceberg_type() == "timestamptz"
    tier = ontology.object_types["Customer"].properties["tier"].type
    assert tier.json_schema() == {"type": "string", "enum": ["bronze", "silver", "gold"]}


def test_link_and_actions():
    ontology, _ = build(VALID)
    link = ontology.link_types["placedBy"]
    assert link.cardinality == "many_to_one"
    assert link.frm.object_type == "Order" and link.to.object_type == "Customer"
    assert link.reverse_name == "orders"

    upgrade = ontology.actions["upgradeTier"]
    assert upgrade.effect.op == "modifyObject"
    assert "tier" in upgrade.effect.set_values
    assert upgrade.description  # required, non-empty (becomes the MCP tool description)

    create = ontology.actions["createOrder"]
    assert create.effect.op == "createObject"
    assert set(create.effect.set_values) >= {"orderId", "customerId", "total", "placedAt"}
