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


def test_apply_without_a_terminal_refuses_rather_than_assuming_yes(tmp_path, capsys):
    """The safety property of the confirmation prompt is what it does when nobody is there: an
    `apply` in a pipeline has to be a deliberate `--yes`, not a consequence of stdin being a pipe.
    pytest captures stdin, so this is exactly that situation."""
    pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
    ontology = _project(tmp_path)
    assert main(["apply", str(ontology)]) == 1
    captured = capsys.readouterr()
    assert "Plan: 2 to create" in captured.out, "the plan is still shown — you can read it, just not run it"
    assert "pass --yes" in captured.err
    assert "aborted" in captured.err


def test_apply_creates_the_tables_and_says_where_it_recorded_them(tmp_path, capsys):
    pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
    ontology = _project(tmp_path)
    assert main(["apply", str(ontology), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "+ rest_main.crm.customers — created · namespace 'crm' created" in out
    assert "Applied 2 table change(s)." in out
    assert "version 1 in `_loom_meta`" in out

    # ...and again: the second run has nothing to do, which is the idempotency claim end to end.
    assert main(["apply", str(ontology), "--yes"]) == 0
    assert "Already applied — nothing to do." in capsys.readouterr().out


def test_apply_refuses_a_breaking_plan_and_exits_nonzero(tmp_path, capsys):
    pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
    ontology = _project(tmp_path)
    assert main(["apply", str(ontology), "--yes"]) == 0
    capsys.readouterr()

    customer = ontology / "customer.yaml"
    customer.write_text(customer.read_text().replace("column: lifetime_value, nullable: true", "column: lifetime_value"))
    assert main(["apply", str(ontology), "--yes"]) == 1
    out = capsys.readouterr().out
    assert "refusing to apply: the plan contains breaking changes" in out
    assert "nothing was applied" in out


def test_plan_needs_a_config_like_every_other_catalog_command(capsys):
    assert main(["plan", str(VALID)]) == 1
    assert "no loom.yaml found" in capsys.readouterr().err


def test_plan_against_an_empty_warehouse_proposes_creations(tmp_path, capsys):
    """The end the read path can't reach: a project whose tables don't exist yet plans clean
    rather than erroring, which is the whole difference between `plan` and `validate --physical`."""
    pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
    ontology = _project(tmp_path)
    assert main(["plan", str(ontology)]) == 0
    out = capsys.readouterr().out
    assert "create table · Customer" in out
    assert "create table · Order" in out
    assert "Plan: 2 to create, 0 to change" in out


def test_plan_reports_a_missing_catalog_binding_rather_than_planning_around_it(tmp_path, capsys):
    """A plan built on an unresolved binding would be silently missing tables."""
    ontology = _project(tmp_path, config="catalogs: {}\n")
    assert main(["plan", str(ontology)]) == 1
    err = capsys.readouterr().err
    assert "catalog 'rest_main', which is not declared in loom.yaml" in err
    assert "Customer" in err  # the hint names what asked for it


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


# --- loom run -------------------------------------------------------------------------------


def _seeded(tmp_path: Path) -> Path:
    """The valid fixture applied to a real local warehouse, with one Customer in it.

    Seeded through `loom run` itself rather than through pyiceberg: an action is how a row gets
    into a Loom-managed table, and using anything else here would leave the CLI's own write path
    only half exercised."""
    pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
    ontology = _project(tmp_path)
    assert main(["apply", str(ontology), "--yes"]) == 0
    return ontology


def test_run_is_the_write_paths_query_and_takes_no_table_column_or_predicate():
    """`loom query` mirrors the generated read tools deliberately — if the dev command can do
    something the tools can't, the ontology has a back door. The same test, and a stronger one,
    because this command writes: `run` takes an action apiName and named parameters, which is
    exactly the shape M4's `run_<action>` tool takes."""
    args = vars(_parsed(["run", "upgradeTier", "ontology", "--param", "a=b"]))
    assert set(args) - {"command", "func"} == {"action", "path", "param", "dry_run", "yes"}
    for forbidden in ("table", "column", "filter", "where", "sql", "query", "predicate", "limit"):
        assert forbidden not in args


def _parsed(argv: list[str]):
    """The CLI's own parser, without running the command."""
    import loom.cli as cli

    holder: dict = {}

    def capture(args):
        holder["args"] = args
        return 0

    original = cli.cmd_run
    cli.cmd_run = capture
    try:
        # `main` rebuilds the parser on each call, so patching the function is enough.
        cli.main(argv)
    finally:
        cli.cmd_run = original
    return holder["args"]


def test_run_goes_through_the_same_runtime_entry_point_the_mcp_tool_will(tmp_path, monkeypatch):
    """One entry point, asserted rather than assumed. A second code path for the dev command is
    how a back door gets built without anyone deciding to build one."""
    from loom.action import ActionRuntime

    ontology = _seeded(tmp_path)
    calls: list[tuple] = []
    original = ActionRuntime.run

    def spy(self, name, params, *, dry_run=False):
        calls.append((name, dict(params), dry_run))
        return original(self, name, params, dry_run=dry_run)

    monkeypatch.setattr(ActionRuntime, "run", spy)
    main(["run", "upgradeTier", str(ontology), "--param", "customer=c1",
          "--param", "newTier=gold", "--dry-run"])

    assert calls == [("upgradeTier", {"customer": "c1", "newTier": "gold"}, True)]


def test_run_without_a_terminal_refuses_rather_than_assuming_yes(tmp_path, capsys):
    """The same safety property as `apply`, and for the same reason: a write in a pipeline has to
    be a deliberate `--yes`. The preview is still printed — you can read what it would do."""
    ontology = _seeded(tmp_path)
    assert main(["run", "createOrder", str(ontology), "--param", "orderId=o1",
                 "--param", "customerId=c1", "--param", "total=1.00"]) == 1
    captured = capsys.readouterr()
    assert "Loom run — createOrder" in captured.err
    assert "pass --yes" in captured.err and "aborted" in captured.err


def test_run_writes_a_row_and_prints_a_typed_result(tmp_path, capsys):
    import json

    ontology = _seeded(tmp_path)
    capsys.readouterr()  # drop the apply output the fixture produced
    assert main(["run", "createOrder", str(ontology), "--param", "orderId=o1",
                 "--param", "customerId=c1", "--param", "total=42.50", "--yes"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "applied" and result["key"] == "o1"
    # A decimal reaches the caller as a string, never through a float — the same rule the read
    # tools follow, because it is the same `json_safe`.
    assert result["after"]["total"] == "42.50"
    assert result["concurrency"] == "enforced — the write asserts the snapshot the read saw"
    assert result["attempts"] == 1

    assert main(["query", "Order", str(ontology), "--key", "o1"]) == 0
    assert '"total": "42.50"' in capsys.readouterr().out


def test_run_exits_nonzero_on_a_refusal_and_names_the_rule(tmp_path, capsys):
    import json

    ontology = _seeded(tmp_path)
    main(["run", "createOrder", str(ontology), "--param", "orderId=o1",
          "--param", "customerId=c1", "--param", "total=1.00", "--yes"])
    capsys.readouterr()

    assert main(["run", "createOrder", str(ontology), "--param", "orderId=o1",
                 "--param", "customerId=c1", "--param", "total=1.00", "--yes"]) == 1
    captured = capsys.readouterr()
    assert "! object_exists" in captured.err
    assert "nothing was written." in captured.err
    assert json.loads(captured.out)["failures"][0]["code"] == "object_exists"


def test_the_prompt_says_it_is_holding_nothing_while_you_decide(tmp_path, capsys):
    """The confirmation prompt sits inside the window this slice is about, and the honest thing to
    print above a `y/N` is that answering slowly is safe *because* nothing is reserved — not because
    it is.

    The run that follows does its own read and asserts that one. So what a person approves is the
    shape of the change, which is also the only thing `run_<action>` could be said to approve, since
    it has no prompt at all. A design where the checked snapshot came from the preview would be one
    the MCP caller could never join, and it would put a human's thinking time inside a transaction.
    """
    ontology = _seeded(tmp_path)
    main(["run", "createOrder", str(ontology), "--param", "orderId=o1",
          "--param", "customerId=c1", "--param", "total=1.00", "--yes"])  # so the table has one
    capsys.readouterr()

    assert main(["run", "createOrder", str(ontology), "--param", "orderId=o2",
                 "--param", "customerId=c1", "--param", "total=2.00", "--dry-run"]) == 0

    err = capsys.readouterr().err
    assert "previewed at snapshot" in err and "nothing is held" in err
    assert "the run reads again and asserts that read" in err
    # Never the word that would turn the number above it into a promise about the table.
    assert "recorded, not yet enforced" not in err


def test_run_names_an_unknown_action_without_a_traceback(tmp_path, capsys):
    ontology = _seeded(tmp_path)
    assert main(["run", "noSuchAction", str(ontology)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "upgradeTier" in err


def test_a_malformed_param_is_caught_before_any_catalog_is_opened(tmp_path, capsys):
    ontology = _project(tmp_path, config=UNREACHABLE_CONFIG)
    assert main(["run", "upgradeTier", str(ontology), "--param", "customer"]) == 1
    assert "--param expects NAME=VALUE" in capsys.readouterr().err
