"""Fixtures shared by the tests that need a real Iceberg warehouse.

`project` lives here rather than in `test_apply_iceberg` because `test_rollback_iceberg` needs the
same starting point — the shipped example, pointed at an empty warehouse — and a fixture imported
across test modules is a fixture defined twice.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from loom import build
from loom.config import find_config, load_config
from loom.errors import Diagnostics

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "retail"


@pytest.fixture
def project(tmp_path):
    """The shipped example's spec and config, pointed at an *empty* warehouse — no seed step."""
    target = tmp_path / "retail"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns(".warehouse"))
    (target / ".warehouse").mkdir()

    diag = Diagnostics()
    config = load_config(find_config(target / "ontology"), diag)
    ontology, _ = build(target / "ontology")
    diag.raise_if_errors()
    return target, ontology, config
