"""Loom — a declarative ontology framework over Apache Iceberg, wired to agents via MCP.

`build()` returns the validated Ontology that everything downstream consumes. The read path
(`Resolver`) and the agent surface (`loom.mcp`) are deliberately *not* re-exported here: they pull
in pyiceberg/duckdb, and importing `loom` to validate a spec should stay dependency-free.
"""

from .config import LoomConfig, find_config, load_config
from .errors import Diagnostics, SpecError, SpecErrors
from .model import Action, LinkType, ObjectType, Ontology
from .ontology import build

__all__ = [
    "build",
    "Ontology",
    "ObjectType",
    "LinkType",
    "Action",
    "Diagnostics",
    "SpecError",
    "SpecErrors",
    "LoomConfig",
    "find_config",
    "load_config",
]
