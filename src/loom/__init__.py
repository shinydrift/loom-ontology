"""Loom — a declarative ontology framework over Apache Iceberg, wired to agents via MCP.

Public surface for the spec module. Everything downstream consumes the validated Ontology
returned by `build()`.
"""

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
]
