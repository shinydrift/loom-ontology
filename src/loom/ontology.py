"""Top-level entry: turn a directory of YAML spec files into a validated Ontology Model."""

from __future__ import annotations

from pathlib import Path

from .errors import Diagnostics
from .loader import load_dir
from .model import Ontology
from .validator import validate


def build(root: str | Path) -> tuple[Ontology, Diagnostics]:
    """Load + validate the ontology under `root`.

    Raises SpecErrors if any hard error is found (via diag.raise_if_errors). On success returns
    the Ontology and the Diagnostics (which may still carry advisory warnings)."""
    diag = Diagnostics()
    loaded = load_dir(root, diag)
    validate(loaded, diag)
    diag.raise_if_errors()
    ontology = Ontology(
        object_types=loaded.objects,
        link_types=loaded.links,
        actions=loaded.actions,
    )
    return ontology, diag
