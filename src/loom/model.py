"""The typed Ontology Model — the in-memory result of loading + validating the YAML spec.

Everything downstream (migrations, resolver, action runtime, MCP registry) consumes *this*,
never the raw YAML. Structurally parsed by the loader; only a fully-validated Ontology is
handed out by ontology.build().
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from .errors import Diagnostics, SourceLoc
from .expr import Expr
from .types import PropType


@dataclass(frozen=True)
class Property:
    name: str
    type: PropType
    column: str
    nullable: bool = False
    unique: bool = False
    description: str | None = None
    # The column `column` used to be. Migration scaffolding: it turns a rename into a field-id
    # remap instead of an add next to stranded data. Outlives its migration on purpose — see
    # spec-v0 §2 — because the same spec is deployed to lakes that are at different versions.
    renamed_from: str | None = None
    loc: SourceLoc | None = None


@dataclass(frozen=True)
class ObjectType:
    api_name: str
    primary_key: str
    title: str
    backing_catalog: str
    backing_table: str
    properties: dict[str, Property]  # keyed by name, insertion-ordered
    searchable: tuple[str, ...] = ()
    # The one property this type may be searched by *meaning* rather than by value. A scalar
    # because it names one, the way `primaryKey` and `title` do and unlike `searchable` — see
    # spec-v0 §2. Declaring it demands `vector_search` of the engine; whether a tool appears for
    # it is a question about the deployment, not this.
    semantic: str | None = None
    display_name: str = ""
    description: str | None = None
    status: str = "active"
    loc: SourceLoc | None = None

    @property
    def pk_property(self) -> Property:
        return self.properties[self.primary_key]

    @property
    def semantic_property(self) -> Property | None:
        """The declared semantic property, or None. Resolved rather than looked up at each use, so
        the two places that ask (negotiation and, later, the sidecar) cannot disagree about what a
        spec that names an undeclared property means — the validator has already refused it."""
        if self.semantic is None:
            return None
        return self.properties.get(self.semantic)


@dataclass(frozen=True)
class LinkEnd:
    object_type: str
    property: str


@dataclass(frozen=True)
class ThroughTable:
    catalog: str
    table: str
    from_column: str
    to_column: str
    # Per-side `renamedFrom`, same meaning as `Property.renamed_from`. A mapping table is planned
    # by the same machinery as a backing table, so it can be renamed by the same one.
    from_renamed_from: str | None = None
    to_renamed_from: str | None = None


@dataclass(frozen=True)
class LinkType:
    api_name: str
    cardinality: str  # one_to_one | one_to_many | many_to_one | many_to_many
    frm: LinkEnd
    to: LinkEnd
    reverse_name: str | None = None
    through: ThroughTable | None = None
    display_name: str = ""
    description: str | None = None
    status: str = "active"
    loc: SourceLoc | None = None


@dataclass(frozen=True)
class Parameter:
    name: str
    type: PropType
    required: bool = True
    default: object | None = None
    description: str | None = None


@dataclass(frozen=True)
class ValidationRule:
    expr: Expr
    message: str
    raw: str


@dataclass(frozen=True)
class Effect:
    op: str  # createObject | modifyObject | deleteObject
    key: Expr | None  # None for create
    set_values: dict[str, Expr] = field(default_factory=dict)  # empty for delete


@dataclass(frozen=True)
class Action:
    api_name: str
    target_object_type: str
    operation: str  # create | modify | delete
    parameters: dict[str, Parameter]  # keyed by name, ordered
    effect: Effect
    validation: tuple[ValidationRule, ...] = ()
    display_name: str = ""
    description: str = ""
    status: str = "active"
    loc: SourceLoc | None = None


@dataclass(frozen=True)
class Ontology:
    """A fully-validated ontology. Guaranteed internally consistent."""

    object_types: dict[str, ObjectType]
    link_types: dict[str, LinkType]
    actions: dict[str, Action]

    def summary(self) -> str:
        return (
            f"{len(self.object_types)} object type(s), "
            f"{len(self.link_types)} link type(s), "
            f"{len(self.actions)} action(s)"
        )


def properties_in_play(action: Action, target: ObjectType) -> set[str]:
    """The declared properties an action reads in a rule or writes in an effect.

    `object.<prop>` in a validation rule or in an effect value is the action saying that property is
    part of its reasoning; a `set` key is it saying the property is part of its outcome. Anything
    else on the row is a neighbour.

    A fact about a spec, so it lives with the spec's own types and takes no runtime: the conflict
    detail asks it of a run in flight (which of the properties that moved does this action care
    about) and `governance.bind_policies` asks it of a deployment that is not running yet (can this
    action stand beside a policy that withholds one). Two readers, one definition — the alternative
    is two that agree until somebody edits one."""
    names = set(action.effect.set_values)
    exprs = [rule.expr for rule in action.validation]
    exprs += list(action.effect.set_values.values())
    if action.effect.key is not None:
        exprs.append(action.effect.key)
    for expr in exprs:
        names.update(ref.path[1] for ref in expr.refs() if len(ref.path) == 2 and ref.path[0] == "object")
    return names & set(target.properties)


def physical_type(prop_type: PropType, object_types: Mapping[str, ObjectType]) -> str | None:
    """The Iceberg type a property's values are actually stored as.

    Only `objectRef` needs the wider map: it travels as the referenced object type's primary key,
    so its storage type is that key's. Returns None for an objectRef whose target is unknown —
    the referential pass reports that, and callers here just skip the column.

    Shared by physical validation (declared type vs. an existing column) and the migration
    planner (declared type vs. the column it wants to exist), so the two can never disagree."""
    if prop_type.kind == "objectRef":
        ref = object_types.get(prop_type.object_type or "")
        return ref.pk_property.type.iceberg_type() if ref is not None else None
    return prop_type.iceberg_type()


def coerce_value(
    prop_type: PropType,
    value: object,
    object_types: Mapping[str, ObjectType],
    ctx: str = "value",
) -> object:
    """Bring a caller-supplied value to the Python type a declared Loom type is carried as.

    One function for both directions, because both directions have the same problem and must not
    answer it differently. On the way *in*, an LLM will happily send `"42"` for a `long` key: JSON
    can't tell those apart, and the mismatch wouldn't fail loudly — it would push down as an
    Iceberg predicate matching nothing, and the agent would be told the object doesn't exist. On
    the way *out*, the same `"42"` has to become an int before it is written, or the row now holds
    a string where the schema promised a number. A second implementation of this would be a second
    set of answers to "is `42.0` a long?".

    The value domain it produces is the one the read path already promises: `Decimal` for decimal
    (never a float — that is the whole reason a spec writes `decimal(12,2)`), tz-aware `datetime`
    for timestamp, `date`, and plain `str` / `int` / `float` / `bool` / `None` elsewhere.

    Raises `ValueError`, fully worded, for callers to wrap in whatever their layer's failure is.
    """
    if value is None:
        return None
    kind = prop_type.kind
    if kind == "objectRef":
        # An objectRef travels as the referenced object's primary key, so it coerces as that key's
        # type — not as a string.
        ref = object_types.get(prop_type.object_type or "")
        if ref is not None:
            return coerce_value(ref.pk_property.type, value, object_types, ctx)
        return str(value)
    try:
        if kind in ("int", "long"):
            return _as_integer(value)
        if kind == "double":
            return float(value)  # type: ignore[arg-type]
        if kind == "decimal":
            return _as_decimal(prop_type, value)
        if kind == "boolean":
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered not in ("true", "false"):
                    raise ValueError(f"expected 'true' or 'false', got {value!r}")
                return lowered == "true"
            return bool(value)
        if kind == "date":
            return _as_date(value)
        if kind == "timestamp":
            return _as_timestamp(value)
        if kind == "enum":
            text = str(value)
            if prop_type.values and text not in prop_type.values:
                raise ValueError(f"'{text}' is not one of: {', '.join(prop_type.values)}")
            return text
    except (TypeError, ValueError, ArithmeticError) as e:
        raise ValueError(f"{ctx}: cannot read {value!r} as {kind} ({e})") from e
    return str(value)


def _as_integer(value: object) -> int:
    """Strict about floats. `int(42.7)` truncates silently, which on the read path turns a filter
    into one that matches the wrong rows and on the write path stores a different number than the
    caller sent. An integral float is fine; a fractional one is a mistake worth naming."""
    if isinstance(value, bool):  # bool is an int subclass, and True would silently become 1
        raise ValueError("boolean is not an integer")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("a fractional number is not an integer — it would be truncated")
        return int(value)
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise ValueError("a fractional number is not an integer — it would be truncated")
        return int(value)
    return int(value)  # type: ignore[arg-type]


def _as_decimal(prop_type: PropType, value: object) -> Decimal:
    """`Decimal(str(value))`, then checked against the declared precision and scale.

    Checked here rather than left to the storage layer because the storage layer's only options are
    to round or to raise something unreadable, and rounding money is exactly what declaring a
    `decimal` was meant to prevent."""
    if isinstance(value, float):
        raise ValueError("a float cannot be read as a decimal without losing precision — send a string")
    try:
        out = Decimal(str(value))
    except InvalidOperation as e:
        raise ValueError(str(e) or "not a decimal") from e
    if not out.is_finite():
        raise ValueError("not a finite decimal")
    precision, scale = prop_type.precision or 0, prop_type.scale or 0
    digits, exponent = out.as_tuple().digits, int(out.as_tuple().exponent)  # type: ignore[arg-type]
    spelling = f"decimal({precision},{scale})"
    if -exponent > scale:
        raise ValueError(f"has more than {scale} decimal place(s), which {spelling} cannot hold")
    # Significant digits once padded out to the declared scale: 1299.99 at scale 2 needs 6, and a
    # 13-digit integer at scale 2 needs 15.
    if len(digits) + scale + exponent > precision:
        raise ValueError(f"has more digits than {spelling} can hold")
    return out


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _as_timestamp(value: object) -> datetime:
    """Always tz-aware. §1 says `timestamp` is `timestamptz`, UTC on the wire, so a naive value —
    which is what `fromisoformat` produces for `2026-01-04T12:00:00` — is read as UTC rather than
    handed on to a storage layer that would either reject it or guess a zone."""
    out = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return out if out.tzinfo is not None else out.replace(tzinfo=UTC)


def check_renames(
    columns: Mapping[str, tuple[str | None, str]],
    diag: Diagnostics,
    loc: SourceLoc | None = None,
    ctx: str = "",
) -> None:
    """The two `renamedFrom` rules that need more than one column in scope (spec §2 rules 10-11).

    `columns` maps each mapped column name to `(renamedFrom or None, the declaration that wants
    it)`. Shared — like `physical_type` above — because it is applied at two scopes that must not
    drift: the validator applies it per declaration so `loom validate` catches the common case
    offline and with a source location, and the migration planner applies it again across every
    declaration bound to one table, which is the scope the rules are really about. Only the second
    has no `loc` to render, which is what `ctx` is for.

    The first rule is what makes renames independent of each other within a table, and therefore
    what lets the planner order edits per column instead of hoisting every rename to the front: no
    rename's source can be another rename's target, so neither a chain nor a swap is expressible."""
    prefix = f"{ctx}: " if ctx else ""
    renamed_by: dict[str, str] = {}
    for column, (old, source) in columns.items():
        if old is None:
            continue
        if old in columns:
            diag.error(
                f"{prefix}{source} renames column '{column}' from '{old}', which "
                f"{columns[old][1]} already maps",
                loc,
                "Loom never drops a column, so a rename cannot take one another property is live on",
            )
        if old in renamed_by:
            diag.error(
                f"{prefix}{renamed_by[old]} and {source} both rename from column '{old}'",
                loc,
                "one column cannot become two — only one of them is the rename",
            )
        else:
            renamed_by[old] = source
