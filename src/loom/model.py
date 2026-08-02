"""The typed Ontology Model — the in-memory result of loading + validating the YAML spec.

Everything downstream (migrations, resolver, action runtime, MCP registry) consumes *this*,
never the raw YAML. Structurally parsed by the loader; only a fully-validated Ontology is
handed out by ontology.build().
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

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
    display_name: str = ""
    description: str | None = None
    status: str = "active"
    loc: SourceLoc | None = None

    @property
    def pk_property(self) -> Property:
        return self.properties[self.primary_key]


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
