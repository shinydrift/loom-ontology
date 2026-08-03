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

from collections.abc import Sequence
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


# ---- a comparison, wherever it came from ---------------------------------------
#
# One node set for the six comparisons, read by both grammars above this layer — a caller's
# `filter` argument (`filters.py`) and a deployment's `rows:` predicate (`predicate.py`).
#
# **This block used to be two, and the correction is worth stating rather than quietly making.**
# v0 predicted "ranges arrive with the filter grammar"; M5 shipped ranges in `Compare` for
# governance and corrected the prediction to "the two are deliberately *not* one node set". Ranges
# then arrived a **second** time, in a caller's hands, which is where that correction turned out to
# have over-generalised from the one node it was true of. What the two grammars actually are:
#
#   - the filter grammar has `Contains` (ILIKE) and no negation and no composition;
#   - the policy grammar has `&& || !` and no ILIKE;
#   - they overlap **exactly on the six comparisons**, where they already agreed node for node —
#     v0's `Eq(col, None)` compiled to `IS NULL`, which is what `Compare('==', col, null)` compiles
#     to, and for a bound non-null parameter `=` and `IS NOT DISTINCT FROM` select the same rows.
#
# So the merge is the overlap, and the difference stays: `Contains` is filter-only, `And`/`Or`/`Not`
# are policy-only. What made a governance predicate un-advisory was never the node *type* — it is
# the **field**: a predicate hangs on `TableRef.predicate`, which an adapter compiles into `WHERE`,
# and only `Search.filters` yields `ScanRequest` hints. `pushdown_hints()` below is the one place
# that decides what may become one, and it cannot be handed a `TableRef`.
#
# One thing does not transfer with the merge: the *lowerable subset* rule. A policy's expression is
# answered twice (SQL and in process) and must mean the same thing both times; a caller's filter is
# answered once, which is why `Contains` may exist at all in a grammar that refuses `contains`.


@dataclass(frozen=True)
class ColumnRef:
    alias: str
    column: str


@dataclass(frozen=True)
class Const:
    """A literal — the policy author's, or the caller's, already coerced to the property's type.
    Distinct from `ColumnRef` so an adapter binds it as a parameter rather than interpolating it,
    and so `null` is recognisable at lowering time."""

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
    unknown, and `predicate.admits` is written to agree with that. A caller's filter inherits both
    answers and needs no second rule — see `filters.py` on why the two grammars cannot disagree
    about a null while one of them has no negation."""

    op: str
    left: Operand
    right: Operand


# ---- the filter surface --------------------------------------------------------


@dataclass(frozen=True)
class Contains:
    """Case-insensitive substring — the one thing a caller can say that a policy cannot.

    Filter-only, and it stays that way for the reason `predicate.NOT_LOWERABLE` gives: inside
    `rows:` it would need a second evaluator agreeing with it forever, and here it needs none,
    because nothing evaluates a caller's filter off the read path."""

    alias: str
    column: str
    value: str


Comparison = Compare | Contains


@dataclass(frozen=True)
class Eq:
    """A traverse's anchor: one column, one value, always the source row's primary key.

    **Not a filter node.** It was one in v0 and stopped being one when the comparisons merged, and
    it survives because an anchor is structurally narrower than a comparison — exactly one column
    against exactly one non-null key — which is what `_compile_traverse` reads it as. `Compare`
    could spell it and would also spell `Const == Const`, which is not a join anchor."""

    alias: str
    column: str
    value: object


def pushdown_hints(filters: Sequence[Comparison]) -> tuple[tuple[str, object], ...]:
    """The `(column, value)` equality hints a caller's filters imply, for `ScanRequest.predicates`.

    **The one place that decides what may be advisory**, and it takes filters rather than a plan or
    a table so that a governance predicate cannot reach it: that channel is a pushdown hint an
    adapter may ignore and the compiled `WHERE` re-applies, which is right for a caller's filter and
    wrong for a policy. `ir.TableRef` says a predicate is never handed to it; this is what makes
    that structural instead of remembered.

    Equality only, because the channel is a `(column, value)` pair by shape — a range has no
    spelling in it. That costs an Iceberg scan some pruning it could do and costs correctness
    nothing, since every filter is in the `WHERE` clause regardless. A null is included: the
    catalog's `_row_filter` maps it to `IsNull`, which is what `Compare('==', col, null)` means."""
    return tuple(
        (f.left.column, f.right.value)
        for f in filters
        if isinstance(f, Compare)
        and f.op == "=="
        and isinstance(f.left, ColumnRef)
        and isinstance(f.right, Const)
    )


# ---- the governance predicate --------------------------------------------------
#
# What a policy's `rows:` expression lowers to: `Compare` above, plus the composition a caller has
# no spelling for. Built only by `predicate.lower()`, never from anything a caller sent, and never
# handed to `ScanRequest.predicates` — see `pushdown_hints`.


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
    """A filtered set of objects. `filters` is a **conjunction** — that flat tuple is why AND costs
    the filter grammar no node, and why `or` is a shape this IR does not have yet rather than a
    spelling `filters.py` declined to offer."""

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
