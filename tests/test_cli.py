"""CLI surface: exit codes, and what lands on stdout vs stderr."""

from __future__ import annotations

from pathlib import Path

import pytest

from loom.cli import main

VALID = Path(__file__).parent / "fixtures" / "valid"

# A local catalog, so the usage-error tests below never touch the network. The tables it points at
# don't exist, which is fine: every error asserted here is raised before any table is read.
LOCAL_CONFIG = """
catalogs:
  rest_main:
    type: iceberg-sql
    uri: 'sqlite:///.warehouse/catalog.db'
    warehouse: 'file://.warehouse'
"""

UNREACHABLE_CONFIG = """
catalogs:
  rest_main: { type: iceberg-rest, uri: 'http://127.0.0.1:1/api' }
"""


def _project(tmp_path: Path, config: str = LOCAL_CONFIG) -> Path:
    """A copy of the valid fixture with a loom.yaml beside it."""
    import shutil

    shutil.copytree(VALID, tmp_path / "ontology")
    (tmp_path / "loom.yaml").write_text(config)
    (tmp_path / ".warehouse").mkdir(exist_ok=True)
    return tmp_path / "ontology"


def test_validate_is_offline_and_needs_no_config(capsys):
    assert main(["validate", str(VALID)]) == 0
    assert "ok — 2 object type(s)" in capsys.readouterr().out


def test_validate_reports_a_broken_spec(tmp_path, capsys):
    (tmp_path / "bad.yaml").write_text("objectType:\n  titel: nope\n")
    assert main(["validate", str(tmp_path)]) == 1
    assert "missing required key 'apiName'" in capsys.readouterr().err


def test_physical_validation_without_a_config_says_so(capsys):
    assert main(["validate", "--physical", str(VALID)]) == 1
    err = capsys.readouterr().err
    assert "no loom.yaml found" in err
    assert "docs/spec-v0.md §6" in err


def test_spec_and_config_problems_are_reported_together(tmp_path, capsys):
    """Fixing one only to be shown the other on the next run is the failure mode here."""
    ontology = _project(tmp_path, config="catalogs: {}\nengine: { type: spark }\n")
    (ontology / "broken.yaml").write_text("objectType:\n  apiName: Ghost\n")
    assert main(["query", "Ghost", str(ontology)]) == 1
    err = capsys.readouterr().err
    assert "unknown engine type 'spark'" in err  # from loom.yaml
    assert "missing required key 'backing'" in err  # from the ontology


def test_query_rejects_an_undeclared_property(tmp_path, capsys):
    pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
    ontology = _project(tmp_path)
    assert main(["query", "Customer", str(ontology), "--filter", "nope=1"]) == 1
    assert "'Customer' has no property 'nope'" in capsys.readouterr().err


def test_a_malformed_filter_is_caught_before_any_catalog_is_opened(tmp_path, capsys):
    """A typo'd flag shouldn't need a reachable metastore to be reported."""
    ontology = _project(tmp_path, config=UNREACHABLE_CONFIG)
    assert main(["query", "Customer", str(ontology), "--filter", "justaname"]) == 1
    assert "--filter expects PROP=VALUE" in capsys.readouterr().err


def test_link_without_a_key_is_refused_before_any_catalog_is_opened(tmp_path, capsys):
    ontology = _project(tmp_path, config=UNREACHABLE_CONFIG)
    assert main(["query", "Customer", str(ontology), "--link", "orders"]) == 1
    assert "--link requires --key" in capsys.readouterr().err


def test_a_missing_local_warehouse_gets_an_actionable_hint(tmp_path, capsys):
    """SQLite's own message names neither the path nor the reason."""
    pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
    ontology = _project(tmp_path)
    (tmp_path / ".warehouse").rmdir()
    assert main(["query", "Customer", str(ontology), "--key", "c1"]) == 1
    err = capsys.readouterr().err
    assert "does not exist" in err and ".warehouse" in err


def test_an_unreachable_catalog_is_an_error_not_a_traceback(tmp_path, capsys):
    """pyiceberg's REST catalog connects while being constructed, so opening one is fallible."""
    pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
    ontology = _project(tmp_path, config=UNREACHABLE_CONFIG)
    assert main(["query", "Customer", str(ontology), "--key", "c1"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "rest_main" in err


@pytest.mark.parametrize("command", ["plan", "apply"])
def test_write_path_commands_are_still_stubs(command, capsys):
    assert main([command, str(VALID)]) == 2
    assert "not implemented yet" in capsys.readouterr().err


def test_warnings_go_to_stderr_so_stdout_stays_parseable(tmp_path, capsys):
    """`loom query` prints JSON on stdout; a warning mixed in would corrupt it."""
    (tmp_path / "c.yaml").write_text(
        """
objectType:
  apiName: C
  primaryKey: id
  title: id
  backing: { catalog: main, table: a.b }
  properties:
    - { name: id, type: string, column: id, unique: true }
    - { name: other, type: string, column: other }
"""
    )
    (tmp_path / "link.yaml").write_text(
        """
linkType:
  apiName: selfLink
  cardinality: many_to_one
  from: { objectType: C, property: id }
  to:   { objectType: C, property: other }
"""
    )
    assert main(["validate", str(tmp_path)]) == 0
    out, err = capsys.readouterr()
    assert "possible fan-out" in err
    assert "warning" not in out
