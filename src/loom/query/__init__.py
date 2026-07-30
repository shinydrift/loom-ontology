"""The query layer — layer 2. Engine-agnostic logical plans and the adapters that lower them.

The IR is the seam that keeps the semantic layer above from ever learning SQL. The resolver
builds plans out of `ir` nodes; an `Engine` lowers them to a dialect. Adding Trino or Spark is a
new module under `engines/`, with nothing above this package changed.
"""

from __future__ import annotations

from .engine import Capabilities, CompiledQuery, Engine, EngineError, ScanRequest
from .ir import Column, Comparison, Contains, Eq, GetByKey, Plan, Project, Search, TableRef, Traverse

__all__ = [
    "Capabilities",
    "Column",
    "Comparison",
    "CompiledQuery",
    "Contains",
    "Engine",
    "EngineError",
    "Eq",
    "GetByKey",
    "Plan",
    "Project",
    "ScanRequest",
    "Search",
    "TableRef",
    "Traverse",
]
