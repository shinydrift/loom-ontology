"""The logical plan node set — the engine-agnostic read IR.

Deliberately *not* a general relational algebra. There are exactly three shapes a read can take
in Loom — fetch one object by key, filter a set of objects, walk one link — and each is a node
here. That bound is the point: it's what guarantees an engine adapter is a few hundred lines
rather than a query planner, and it's what makes "the LLM never gets raw SQL" a property of the
type system instead of a promise.

Every plan is rooted in a `Project`, which is where ontology *property* names re-enter — nodes
below it speak only physical columns.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TableRef:
    """A physical table plus the alias it gets in the compiled query."""

    catalog: str
    table: str
    alias: str


@dataclass(frozen=True)
class Column:
    """A projected column: physical `alias.column` surfaced under an ontology property name."""

    alias: str
    column: str
    output: str


# ---- predicates ----------------------------------------------------------------
#
# Two are enough for v0: equality (the only one a catalog can push down) and case-insensitive
# substring match (what `searchable` means in practice). Ranges arrive with the filter grammar.


@dataclass(frozen=True)
class Eq:
    alias: str
    column: str
    value: object


@dataclass(frozen=True)
class Contains:
    alias: str
    column: str
    value: str


Comparison = Eq | Contains


# ---- source nodes --------------------------------------------------------------


@dataclass(frozen=True)
class GetByKey:
    """Exactly one row, addressed by primary key. Distinct from a `Search` with an equality
    filter because it is the one read an engine can always answer with a pruned single-row scan,
    and because "no such object" is a meaningful result rather than an empty set."""

    table: TableRef
    key_column: str
    key_value: object


@dataclass(frozen=True)
class Search:
    table: TableRef
    filters: tuple[Comparison, ...] = ()
    # Physical columns on the projected table. Not cosmetic: LIMIT/OFFSET without a total order
    # makes page 2 of a paginated tool call unrelated to page 1, so the resolver always sets this.
    order_by: tuple[str, ...] = ()
    limit: int | None = None
    offset: int = 0


@dataclass(frozen=True)
class Traverse:
    """One hop along a link: rows of `to_table` joined to the `from_table` row(s) identified by
    `anchor`. `through` carries the many-to-many mapping table when the link declares one.

    Only one hop, by design — multi-hop graph walking is an agent composing calls, not a plan
    the engine builds, which keeps the cost of any single tool call bounded and predictable.
    """

    from_table: TableRef
    to_table: TableRef
    from_column: str
    to_column: str
    anchor: Eq
    through: ThroughRef | None = None
    order_by: tuple[str, ...] = ()  # physical columns on `to_table`; see Search.order_by
    limit: int | None = None
    offset: int = 0


@dataclass(frozen=True)
class ThroughRef:
    table: TableRef
    from_column: str
    to_column: str


Source = GetByKey | Search | Traverse


@dataclass(frozen=True)
class Project:
    """The root of every plan: physical columns -> ontology property names."""

    source: Source
    columns: tuple[Column, ...]


Plan = Project
