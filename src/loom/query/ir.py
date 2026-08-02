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
    """A physical table, the alias it gets in the compiled query, and the rows of it this
    deployment will read.

    That last clause is M5's, and it is why the governance predicate hangs *here* rather than on
    the three source nodes. `Resolver._table` is the one place an object type becomes a table, so
    attaching the predicate there governs the anchor end and the landing end of a `Traverse` with
    the same line of code, and governs `GetByKey` without anybody remembering to. The alternative —
    a `predicate` field on `GetByKey`, `Search` and `Traverse` — is three places to remember and,
    for a traverse, two ends to forget one of. *You cannot search a customer but you can traverse to
    one* is then not a rule anybody has to keep: there is nowhere to write it.

    A `ThroughRef`'s mapping table carries none, and correctly: it stands for no object type, so no
    policy names it.

    A `TableRef` with a predicate is a **view**, which is the read-path twin of a projection that
    never selects a masked column: rows are withheld by not being in the table, properties by not
    being asked for."""

    catalog: str
    table: str
    alias: str
    predicate: Predicate | None = None


@dataclass(frozen=True)
class Column:
    """A projected column: physical `alias.column` surfaced under an ontology property name."""

    alias: str
    column: str
    output: str


# ---- the filter surface --------------------------------------------------------
#
# Two are enough for v0: equality (the only one a catalog can push down) and case-insensitive
# substring match (what `searchable` means in practice).
#
# This set used to predict that "ranges arrive with the filter grammar". They arrived with
# governance instead, as `Compare` below, and the two are deliberately *not* one node set. These
# two are what a **caller's** `filter` argument compiles to: `Eq` is pushdownable as a
# `ScanRequest` hint, and `Contains` exists because `searchable` on a string property means
# substring. That is a property of the filter surface and not of the expression language — the
# same `name == 'x'` written in a policy is equality and never `ILIKE`.


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


# ---- the governance predicate --------------------------------------------------
#
# What a policy's `rows:` expression lowers to. Built only by `predicate.lower()`, never from
# anything a caller sent, and never handed to `ScanRequest.predicates` — that channel is documented
# as a pushdown *hint* an adapter may ignore, and a governance filter must not be advisory anywhere.


@dataclass(frozen=True)
class ColumnRef:
    alias: str
    column: str


@dataclass(frozen=True)
class Const:
    """A literal the policy author wrote. Distinct from `ColumnRef` so an adapter binds it as a
    parameter rather than interpolating it, and so `null` is recognisable at lowering time."""

    value: object


Operand = ColumnRef | Const


@dataclass(frozen=True)
class Compare:
    """One comparison of the expression language, in the expression language's own spelling.

    **`==` and `!=` on this node are null-safe.** This is not SQL's `=`: `Compare('==', x, null)`
    is true when `x` is null, because §5 says null is a value and a policy is written in §5. An
    adapter that lowers it to `=` will pass every test whose data has no nulls and be wrong exactly
    where governance matters, so the differential test in `test_predicate.py` fixes it against an
    in-process evaluator over a table full of them.

    The four ordering operators are *not* lifted: §5 refuses to order a null and SQL yields
    unknown, and `predicate.admits` is written to agree with that."""

    op: str
    left: Operand
    right: Operand


@dataclass(frozen=True)
class And:
    left: Predicate
    right: Predicate


@dataclass(frozen=True)
class Or:
    left: Predicate
    right: Predicate


@dataclass(frozen=True)
class Not:
    term: Predicate


Predicate = Compare | And | Or | Not


def predicate_columns(pred: Predicate | None) -> set[tuple[str, str]]:
    """Every `(alias, column)` a predicate reads.

    An adapter needs it because a governed column has to be *scanned* even when it is not
    projected: a policy may legitimately filter on a property it also masks — that is Loom
    filtering rather than the caller, and the result is an absent row rather than an answer."""
    if pred is None:
        return set()
    if isinstance(pred, Compare):
        return {(o.alias, o.column) for o in (pred.left, pred.right) if isinstance(o, ColumnRef)}
    if isinstance(pred, Not):
        return predicate_columns(pred.term)
    return predicate_columns(pred.left) | predicate_columns(pred.right)


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


def tables_of(source: Source) -> tuple[TableRef, ...]:
    """Every table a source node reads, in the order it names them.

    One function so an adapter has one way to ask "what am I reading?", which is what makes *every
    governed end of every plan is filtered* a claim a compiler can keep rather than a list a
    compiler author has to. A `Traverse` names two, and forgetting the anchor end is precisely the
    hole this exists to close."""
    if isinstance(source, GetByKey):
        return (source.table,)
    if isinstance(source, Search):
        return (source.table,)
    tables = (source.to_table, source.from_table)
    return tables + ((source.through.table,) if source.through is not None else ())


@dataclass(frozen=True)
class Project:
    """The root of every plan: physical columns -> ontology property names."""

    source: Source
    columns: tuple[Column, ...]


Plan = Project
