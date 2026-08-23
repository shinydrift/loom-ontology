"""The `Engine` port — layer 2's boundary.

An engine does two things: turn a `Plan` into something executable (`compile`) and run it
(`execute`). The split is deliberate — `compile` is pure and therefore testable without any
storage, which is how the SQL an adapter generates gets asserted directly in tests instead of
only inferred from query results.

`capabilities()` is read by `negotiate.py`, which refuses to wire an ontology to an engine that
cannot serve the surface it generates — and it is *also* what lets the write path later choose a
native `MERGE` where one exists. Those are two different kinds of fact and the dataclass says which
is which.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .ir import Plan


class EngineError(RuntimeError):
    """A compile- or execution-time failure inside an engine adapter."""


@dataclass(frozen=True)
class Capabilities:
    """What an adapter can do — and two kinds of fact live here, which is worth saying because it
    looked for a while like one of them was in the wrong place.

    A **requirement** is something a spec can demand and an engine can therefore fail: declare a
    link and a traverse needs a join, declare a string property searchable and a filter needs a
    case-insensitive LIKE. `negotiate.py` reads those, and a mismatch refuses to start.

    A **routing hint** is something no spec can demand, because there is a path that works without
    it. `native_merge` is the only one: writes go through the catalog's `RowWriter` — which every
    catalog implements — so an engine that cannot `MERGE` is a slower way to serve an ontology and
    never a reason to refuse one. It sits on this dataclass rather than somewhere on the write path
    because it is a fact about the *engine*, and this is where an engine is asked what it is; that
    the engine only reads today does not make the question a read-path question.

    `negotiate.NEGOTIATED` / `NOT_NEGOTIATED` cover these fields exactly, under a test, so adding a
    flag here forces the choice between the two kinds rather than quietly making a third."""

    name: str
    joins: bool = True
    offset: bool = True
    case_insensitive_like: bool = True
    # **Defaults false, unlike the three above, and the difference is what a default asserts.**
    # Those three are floors: a dialect that can filter can join, offset and lower a string, so
    # defaulting them true says something almost every adapter would say anyway. Vector distance
    # over a fixed-width float array is not implied by being able to say `WHERE c = ?` — it needs
    # an array type and arithmetic over it, which plenty of dialects have neither of. So an
    # adapter claims this or it does not have it, and a new adapter that says nothing is described
    # correctly rather than optimistically.
    vector_search: bool = False
    native_merge: bool = False  # routing hint, never negotiated — see above


@dataclass(frozen=True)
class ScanRequest:
    """What the engine needs materialized before its SQL can run: one catalog table, bound to an
    alias, pruned to the columns and equality predicates the plan actually implies.

    This is the pushdown channel. An engine that reads Iceberg natively can ignore it; the DuckDB
    adapter uses it to avoid reading a whole table to answer a get-by-key.
    """

    alias: str
    catalog: str
    table: str
    columns: tuple[str, ...] = ()
    predicates: tuple[tuple[str, Any], ...] = ()
    limit: int | None = None


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    params: tuple[Any, ...] = ()
    scans: tuple[ScanRequest, ...] = field(default_factory=tuple)


@runtime_checkable
class Engine(Protocol):
    def capabilities(self) -> Capabilities: ...

    def compile(self, plan: Plan) -> CompiledQuery:
        """Lower a logical plan to dialect SQL. Pure — no catalog or network access."""
        ...

    def execute(self, compiled: CompiledQuery) -> Sequence[dict]:
        """Run a compiled query, returning rows keyed by the plan's output names."""
        ...
