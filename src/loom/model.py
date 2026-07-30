"""The typed Ontology Model — the in-memory result of loading + validating the YAML spec.

Everything downstream (migrations, resolver, action runtime, MCP registry) consumes *this*,
never the raw YAML. Structurally parsed by the loader; only a fully-validated Ontology is
handed out by ontology.build().
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import SourceLoc
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
