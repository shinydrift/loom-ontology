"""The `Engine` port — layer 2's boundary.

An engine does two things: turn a `Plan` into something executable (`compile`) and run it
(`execute`). The split is deliberate — `compile` is pure and therefore testable without any
storage, which is how the SQL an adapter generates gets asserted directly in tests instead of
only inferred from query results.

`capabilities()` is what the serve-time negotiation in M4 reads, and what lets the write path
later choose a native `MERGE` where one exists.
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
    name: str
    joins: bool = True
    offset: bool = True
    case_insensitive_like: bool = True
    native_merge: bool = False  # write path (M3+): can the engine MERGE, or must writes go via the catalog?


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
