"""Fixtures shared by the tests that need a real Iceberg warehouse.

`project` lives here rather than in `test_apply_iceberg` because `test_rollback_iceberg` needs the
same starting point — the shipped example, pointed at an empty warehouse — and a fixture imported
across test modules is a fixture defined twice.
"""

from __future__ import annotations

import importlib.util
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


@pytest.fixture
def seeded(tmp_path):
    """The shipped example, *seeded* — real Iceberg tables with rows in them, no `loom apply`.

    `project`'s counterpart, and here for the same reason it is: four iceberg modules had defined
    this identically before a fifth wanted it, which is exactly the "a fixture imported across test
    modules is a fixture defined twice" this file opens with. A test that needs the warehouse empty
    takes `project`; one that needs something to act on takes this."""
    target = tmp_path / "retail"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns(".warehouse"))
    spec = importlib.util.spec_from_file_location("retail_seed", target / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.seed(target)

    diag = Diagnostics()
    config = load_config(find_config(target / "ontology"), diag)
    ontology, _ = build(target / "ontology")
    diag.raise_if_errors()
    return target, ontology, config
