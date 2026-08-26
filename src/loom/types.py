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

# Widening pairs — is a value of the first *readable* as the second? Used when checking a
# property's declared type against its backing column, and when checking link join-property
# comparability. ("float", "double") is reachable only from the physical side: Loom has no `float`
# kind, so a float column can back a double property but no Loom type ever compares as one.
#
# **This is not Iceberg's ALTER-time promotion set**, and it was written as though it were until a
# probe ran one into a real catalog. Reading an `int` column as a `double` is fine everywhere Loom
# reads — DuckDB widens it, the values are exact — but asking Iceberg to *restore* the column as a
# double is a different question, and its answer is no. See `iceberg_alterable`.
_PROMOTIONS = frozenset(
    {("int", "long"), ("int", "double"), ("long", "double"), ("float", "double")}
)

# Iceberg's own schema-evolution promotions: what `UpdateSchema.update_column` will accept, which
# is a strictly smaller set than the readable-as pairs above. Iceberg promotes `int -> long` and
# `float -> double` and nothing else among the types Loom can name (decimal widens too, but Loom's
# physical type carries precision *and* scale and deliberately treats any decimal change as a
# retype — see `_NUMERIC_KINDS`).
#
# Kept beside `_PROMOTIONS` rather than folded into it because the two answer different questions
# and the migration planner needs this one: a plan that calls `int -> double` physical-safe is a
# plan `loom apply` cannot execute, and the operator meets the difference as a raw catalog error
# half way through a migration instead of as a classification before it starts.
_ICEBERG_ALTERS = frozenset({("int", "long"), ("float", "double")})

# Every kind that denotes a number, which is a different set from the widening pairs above:
# `decimal` is here and appears in no promotion, because nothing may widen *into* it or out of it
# without changing what the spec asked for. See `PropType.comparable_in_a_comparison`.
_NUMERIC_KINDS = frozenset({"int", "long", "float", "double", "decimal"})


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
            # "key of a X" read, to an agent, as a promise that a key naming no X would be refused.
            # It is not one, and only one objectRef per action is: the runtime reads the row an
            # effect's `key` addresses and refuses `object_not_found`, and every *other* objectRef
            # parameter is bound, type-checked and written as the string it is. `run_record_order`
            # accepted `customer: "c999"` and committed an Order whose `placedBy` traverses to
            # nothing.
            #
            # Said rather than checked, and deliberately. A reference check here could not be
            # carried into the write's own commit the way the snapshot assertion is — the referenced
            # row can be deleted between the check and the commit — so it would narrow the window
            # rather than close it, which is the thing §4.1 refuses to call optimistic concurrency.
            # An advisory check that reads like a guarantee is worse than a sentence that says which
            # one this is.
            return {
                "type": "string",
                "description": (
                    f"the primaryKey of a {self.object_type}, as the caller states it — Loom "
                    f"resolves it only where it addresses the row this action targets, so "
                    f"elsewhere a key naming no {self.object_type} is written rather than refused"
                ),
            }
        raise AssertionError(f"unhandled kind {k!r}")

    @staticmethod
    def of_literal(value: object) -> PropType | None:
        """The type of an expression literal, or None for `null` — which has every type and so
        constrains nothing.

        Here rather than in either of its readers because it is a statement about the type system:
        the validator infers it for a rule, `predicate.py` infers it for a policy, and a literal
        that means `long` in one place and `int` in the other would be two answers to one
        question."""
        if isinstance(value, bool):
            return PropType("boolean")
        if isinstance(value, int):
            return PropType("long")
        if isinstance(value, float):
            return PropType("double")
        if isinstance(value, str):
            return PropType("string")
        return None

    def comparable_to(self, other: PropType) -> bool:
        """Whether a value of one type may **stand in for** the other — a link's two join
        properties, an effect key against a primary key, an effect value against the property it is
        written to. Same kind, or a numeric widening pair. enum compares as its string storage.

        Deliberately not the rule for *comparisons*: standing in for a type is a claim about storage
        and a comparison is a question with a yes/no answer. See `comparable_in_a_comparison`."""
        a, b = self._numeric_base(), other._numeric_base()
        if a == b:
            return True
        return (a, b) in _PROMOTIONS or (b, a) in _PROMOTIONS

    def comparable_in_a_comparison(self, other: PropType) -> bool:
        """Whether `<`, `>`, `==` between these two types has an answer at run time.

        Wider than `comparable_to` by exactly one rule — **any two numeric kinds compare, decimal
        included** — and the reason it is a second method rather than a widening of the first is
        that `_PROMOTIONS` is also Iceberg's physical promotion set. A `decimal` may not back a
        `long` column and may not be joined to one; asking whether it is *less than* one is a
        different question, and `evaluate.py` has always answered it: numbers compare across their
        Python types, so a `Decimal('1299.99')` read out of a row compares against the `1000` an
        author wrote.

        Found by a probe. The offline check refused `object.total < 1000` in a governance policy —
        for a property whose *type* the read path advertises as a string and whose *filter* grammar
        accepts an integer as lossless — while the evaluator that would have run it, and the
        validator that checks the same expression language for an action, both permitted it. Three
        answers to one question, and the refusal was the odd one out: it made every `decimal`
        property structurally impossible to write a row policy over."""
        if self.kind in _NUMERIC_KINDS and other.kind in _NUMERIC_KINDS:
            return True
        return self.comparable_to(other)

    def _numeric_base(self) -> str:
        return "string" if self.kind == "enum" else self.kind


def promotable(from_iceberg: str, to_iceberg: str) -> bool:
    """Physical compatibility: is a value of `from_iceberg` readable as `to_iceberg`?
    Used at plan/serve time against live catalog introspection (deferred until we bind a
    catalog, but the rule lives here next to the type system it belongs to).

    A *read* question, and the reason `loom validate --physical` accepts an `int` column under a
    `double` property: every value in it is exactly representable and every engine widens it. Do
    not reach for this to decide whether a migration can run — `iceberg_alterable` is that one."""
    if from_iceberg == to_iceberg:
        return True
    return (from_iceberg, to_iceberg) in _PROMOTIONS


def iceberg_alterable(from_iceberg: str, to_iceberg: str) -> bool:
    """Can Iceberg change a live column's stored type from `from_iceberg` to `to_iceberg`?

    The DDL question, and deliberately not the same one as `promotable`. Iceberg's promotion set is
    the two pairs that are binary-compatible in the file formats it writes; everything else — an
    `int` column a spec now calls `double` included — needs the data rewritten, which is a
    migration Loom does not perform and therefore classifies as breaking rather than proposing."""
    if from_iceberg == to_iceberg:
        return True
    return (from_iceberg, to_iceberg) in _ICEBERG_ALTERS
