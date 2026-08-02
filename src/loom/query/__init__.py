"""The query layer — layer 2. Engine-agnostic logical plans and the adapters that lower them.

The IR is the seam that keeps the semantic layer above from ever learning SQL. The resolver
builds plans out of `ir` nodes; an `Engine` lowers them to a dialect. Adding Trino or Spark is a
new module under `engines/`, with nothing above this package changed.
"""

from __future__ import annotations

from .engine import Capabilities, CompiledQuery, Engine, EngineError, ScanRequest
from .ir import (
    And,
    Column,
    ColumnRef,
    Compare,
    Comparison,
    Const,
    Contains,
    Eq,
    GetByKey,
    Not,
    Or,
    Plan,
    Predicate,
    Project,
    Search,
    TableRef,
    Traverse,
    tables_of,
)

__all__ = [
    "And",
    "Capabilities",
    "Column",
    "ColumnRef",
    "Compare",
    "Comparison",
    "CompiledQuery",
    "Const",
    "Contains",
    "Engine",
    "EngineError",
    "Eq",
    "GetByKey",
    "Not",
    "Or",
    "Plan",
    "Predicate",
    "Project",
    "ScanRequest",
    "Search",
    "TableRef",
    "Traverse",
    "tables_of",
]
