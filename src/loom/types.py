"""The canonical type system — §1 of the spec grammar.

The linchpin of the framework: every Loom type simultaneously knows its Iceberg type
(drives DDL + physical validation) and its JSON-Schema shape (drives the MCP tool
contract). Adding a type = one entry here + adapter lowering. Nothing else should hard-code
the list of types.
"""

from __future__ import annotations

from dataclasses import dataclass

# Scalars carry no extra config. enum/objectRef/decimal need auxiliary fields.
SCALAR_KINDS = frozenset(
    {"string", "boolean", "int", "long", "double", "date", "timestamp"}
)
ALL_KINDS = SCALAR_KINDS | {"decimal", "enum", "objectRef"}

# Canonical -> Iceberg physical type (for DDL + physical compatibility checks).
_ICEBERG = {
    "string": "string",
    "boolean": "boolean",
    "int": "int",
    "long": "long",
    "double": "double",
    "date": "date",
    "timestamp": "timestamptz",
    "enum": "string",
    "objectRef": None,  # resolves to the referenced object's primary-key iceberg type
}

# Iceberg's own promotion rules — widening only. Used when checking a property's declared
# type against its backing column, and when checking link join-property comparability.
_PROMOTIONS = frozenset({("int", "long"), ("int", "double"), ("long", "double")})


@dataclass(frozen=True)
class PropType:
    """A resolved property/parameter type: a kind plus whatever config it requires."""

    kind: str
    values: tuple[str, ...] | None = None  # enum
    object_type: str | None = None  # objectRef
    precision: int | None = None  # decimal
    scale: int | None = None  # decimal

    def iceberg_type(self) -> str | None:
        if self.kind == "decimal":
            return f"decimal({self.precision},{self.scale})"
        return _ICEBERG[self.kind]

    def json_schema(self) -> dict:
        """The JSON Schema fragment used to build MCP tool input contracts."""
        k = self.kind
        if k == "string":
            return {"type": "string"}
        if k == "boolean":
            return {"type": "boolean"}
        if k in ("int", "long"):
            return {"type": "integer", "format": "int32" if k == "int" else "int64"}
        if k == "double":
            return {"type": "number"}
        if k == "decimal":
            return {"type": "string", "description": f"decimal({self.precision},{self.scale})"}
        if k == "date":
            return {"type": "string", "format": "date"}
        if k == "timestamp":
            return {"type": "string", "format": "date-time"}
        if k == "enum":
            return {"type": "string", "enum": list(self.values or ())}
        if k == "objectRef":
            return {"type": "string", "description": f"key of a {self.object_type}"}
        raise AssertionError(f"unhandled kind {k!r}")

    def comparable_to(self, other: "PropType") -> bool:
        """Whether two types can be compared/joined (link join props, expr comparisons).
        Same kind, or a numeric widening pair. enum compares as its string storage."""
        a, b = self._numeric_base(), other._numeric_base()
        if a == b:
            return True
        return (a, b) in _PROMOTIONS or (b, a) in _PROMOTIONS

    def _numeric_base(self) -> str:
        return "string" if self.kind == "enum" else self.kind


def promotable(from_iceberg: str, to_iceberg: str) -> bool:
    """Physical compatibility: is a value of `from_iceberg` readable as `to_iceberg`?
    Used at plan/serve time against live catalog introspection (deferred until we bind a
    catalog, but the rule lives here next to the type system it belongs to)."""
    if from_iceberg == to_iceberg:
        return True
    return (from_iceberg, to_iceberg) in _PROMOTIONS
