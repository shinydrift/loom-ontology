"""CLI surface: exit codes, and what lands on stdout vs stderr."""

from __future__ import annotations

from pathlib import Path

import pytest

from loom import build
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
    assert "--filter expects PROP=VALUE or PROP.OP=VALUE" in capsys.readouterr().err


def test_a_property_given_both_spellings_at_once_is_refused(tmp_path, capsys):
    ontology = _project(tmp_path, config=UNREACHABLE_CONFIG)
    argv = ["query", "Customer", str(ontology), "--filter", "ltv=1", "--filter", "ltv.gte=1"]
    assert main(argv) == 1
    assert "both a bare value and operators" in capsys.readouterr().err


def test_an_operator_other_than_in_given_twice_is_refused(tmp_path, capsys):
    """Only `in` accumulates. Keeping the last value silently answers a filter nobody wrote —
    and now that one operator builds a list, the difference has to be said rather than guessed."""
    ontology = _project(tmp_path, config=UNREACHABLE_CONFIG)
    argv = ["query", "Customer", str(ontology), "--filter", "ltv.gte=1", "--filter", "ltv.gte=2"]
    assert main(argv) == 1
    assert "gives 'ltv.gte' twice" in capsys.readouterr().err


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


def _seeded(tmp_path: Path, config: str = LOCAL_CONFIG) -> Path:
    """The valid fixture applied to a real local warehouse, with one Customer in it.

    Seeded through `loom run` itself rather than through pyiceberg: an action is how a row gets
    into a Loom-managed table, and using anything else here would leave the CLI's own write path
    only half exercised."""
    pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
    ontology = _project(tmp_path, config)
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


def _parsed(argv: list[str], command: str = "cmd_run"):
    """The CLI's own parser, without running the command.

    `command` names the handler to intercept, so the same helper can ask what any subcommand's
    argument set is — which is the shape these tests assert, for `run` and for `ingest`."""
    import loom.cli as cli

    holder: dict = {}

    def capture(args):
        holder["args"] = args
        return 0

    original = getattr(cli, command)
    setattr(cli, command, capture)
    try:
        # `main` rebuilds the parser on each call, so patching the function is enough.
        cli.main(argv)
    finally:
        setattr(cli, command, original)
    return holder["args"]


def test_run_goes_through_the_same_runtime_entry_point_the_mcp_tool_will(tmp_path, monkeypatch):
    """One entry point, asserted rather than assumed. A second code path for the dev command is
    how a back door gets built without anyone deciding to build one."""
    from loom.action import ActionRuntime

    ontology = _seeded(tmp_path)
    calls: list[tuple] = []
    original = ActionRuntime.run

    def spy(self, name, params, *, actor=None, dry_run=False):
        calls.append((name, dict(params), dry_run))
        return original(self, name, params, actor=actor, dry_run=dry_run)

    monkeypatch.setattr(ActionRuntime, "run", spy)
    main(["run", "upgradeTier", str(ontology), "--param", "customer=c1",
          "--param", "newTier=gold", "--dry-run"])

    assert calls == [("upgradeTier", {"customer": "c1", "newTier": "gold"}, True)]


def test_run_names_the_actor_itself_rather_than_letting_the_runtime_guess(tmp_path, capsys, monkeypatch):
    """`default_actor()` lives at this call site and nowhere below it.

    Here it is honest — a person at a terminal, or a CI job that set `LOOM_ACTOR`. Over MCP the same
    call would name whoever started `loom serve`, so the runtime refuses to make it and takes an
    argument instead. This asserts the CLI actually fills it in, because a runtime that records
    `unknown` for every `loom run` would be the other way of getting this wrong."""
    from loom.action import EditLog
    from loom.catalog import open_catalogs
    from loom.config import find_config, load_config
    from loom.errors import Diagnostics

    monkeypatch.setenv("LOOM_ACTOR", "release-pipeline")
    ontology = _seeded(tmp_path)
    assert main(["run", "createOrder", str(ontology), "--param", "orderId=o9",
                 "--param", "customerId=c1", "--param", "total=1.00", "--yes"]) == 0

    err = capsys.readouterr().err
    assert "recorded in _loom_meta.edits as " in err

    diag = Diagnostics()
    config = load_config(find_config(ontology), diag)
    history = EditLog(catalog=open_catalogs(config)["rest_main"]).history()
    assert [r["actor"] for r in history] == ["release-pipeline"]
    assert history[0]["action"] == "createOrder" and history[0]["object_key"] == "o9"


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


def test_serve_refuses_an_engine_that_cannot_serve_the_surface(tmp_path, capsys, monkeypatch):
    """A capability mismatch stops the server rather than starting a degraded one.

    It reaches `cmd_serve` as an error and not a traceback for the same reason an unopenable
    catalog does — better to refuse to start than to advertise tools that fail. The shape is worse
    here, though, and the test names it: an engine missing OFFSET would serve page 1 of every
    query and fail page 2, so it works until it doesn't, by which time a client holds the tool
    list.

    Nothing is spawned. The refusal happens inside `build_server`, before `cmd_serve` reaches a
    transport to block on."""
    pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
    from loom.query.engine import Capabilities

    class Pageless:
        def capabilities(self):
            return Capabilities(name="pageless", offset=False)

        def compile(self, plan):  # pragma: no cover - never reached
            raise AssertionError("refused before anything compiled")

        def execute(self, compiled):  # pragma: no cover - never reached
            raise AssertionError("refused before anything executed")

    monkeypatch.setattr("loom.query.engines.open_engine", lambda cfg, cats: Pageless())
    ontology = _project(tmp_path)
    assert main(["serve", str(ontology)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "engine 'pageless' cannot serve this ontology" in err
    assert "offset" in err


def test_the_serve_banner_says_whether_this_server_can_write(tmp_path):
    """"How many tools" does not answer "can this change my lake", so the banner answers it
    separately, in both modes.

    Asserted against the line builder rather than by spawning a server, because `cmd_serve` blocks
    on a transport immediately afterwards — `tests/test_mcp_stdio.py` is where the served surface
    itself is checked."""
    from loom.cli import _write_mode
    from loom.config import LoomConfig, McpConfig

    ontology, _ = build(VALID)  # three declared actions

    off = "\n".join(_write_mode(LoomConfig(), ontology))
    assert "read-only" in off and "3 declared action(s) are not exposed" in off
    # And the thing that is *not* switched off, said out loud so nobody reads this as the runtime
    # being unavailable.
    assert "`loom run` still reaches them" in off

    named = "\n".join(_write_mode(LoomConfig(mcp=McpConfig(writes=True, actor="ci")), ontology))
    assert "writes enabled · 3 action(s) exposed" in named and "actor 'ci'" in named

    anonymous = "\n".join(_write_mode(LoomConfig(mcp=McpConfig(writes=True)), ontology))
    assert "actor 'unknown'" in anonymous and "set mcp.actor" in anonymous


def test_the_serve_banner_says_where_vectors_come_from(tmp_path):
    """The same gap `_write_mode` exists to explain, for the other half of M10's first slice.

    A spec can declare `semantic:` and a deployment can configure no provider, and the tool count
    alone cannot tell that apart from a spec declaring none. This is also the line that keeps
    `mcp.embedding` from being a key nothing reads until slice 3 — the `loom.managed` shape, where a
    field is written and never looked at, which this codebase has already been caught by once."""
    import dataclasses

    from loom.cli import _semantic_mode
    from loom.config import EmbeddingConfig, LoomConfig, McpConfig

    ontology, _ = build(VALID)
    assert _semantic_mode(LoomConfig(), ontology) == []  # nothing declared, nothing said

    customer = dataclasses.replace(ontology.object_types["Customer"], semantic="name")
    declared = dataclasses.replace(
        ontology, object_types={**ontology.object_types, "Customer": customer}
    )

    off = "\n".join(_semantic_mode(LoomConfig(), declared))
    assert "semantic search off" in off and "Customer.name" in off and "mcp.embedding" in off

    on = "\n".join(
        _semantic_mode(
            LoomConfig(mcp=McpConfig(embedding=EmbeddingConfig(model="bge-small"))), declared
        )
    )
    # The model, not just the provider: it is folded into every stored vector's hash, so two
    # servers on one spec with different models do not share a warehouse's vectors.
    assert "Customer.name" in on and "local/bge-small" in on


# ---- ingest --------------------------------------------------------------------


INGEST_CONFIG = LOCAL_CONFIG + """
ingest:
  - name: customers
    objectType: Customer
    mode: append
    format: ndjson
governance:
  ingest: allowed
"""

BATCH = '{"customerId": "c9", "name": "Alan Turing", "tier": "bronze", "ltv": 12.5}\n'


def test_ingest_takes_an_entry_and_a_file_and_nothing_that_describes_the_load():
    """The load's shape lives in `loom.yaml` because a load is a fact about a deployment, and a
    flag that could contradict the reviewed file is what `mcp.writes` refused to be. What the
    command line carries is which file, and three operator decisions."""
    args = vars(_parsed(["ingest", "customers", "b.ndjson", "ontology"], "cmd_ingest"))
    assert set(args) - {"command", "func"} == {
        "entry", "source", "path", "dry_run", "load_id", "reject_to", "yes",
    }
    for forbidden in ("mode", "format", "table", "columns", "objectType", "object_type"):
        assert forbidden not in args


def test_ingest_dry_run_writes_nothing_and_reports_what_would_land(tmp_path, capsys):
    ontology = _seeded(tmp_path, INGEST_CONFIG)
    (tmp_path / "batch.ndjson").write_text(BATCH)

    assert main(["ingest", "customers", str(tmp_path / "batch.ndjson"), str(ontology),
                 "--dry-run"]) == 0
    out = capsys.readouterr()
    assert '"status": "previewed"' in out.out
    assert '"rowsWritten": 1' in out.out
    assert "Loom ingest — customers" in out.err
    assert "+ append 1 row(s) into Customer" in out.err


def test_ingest_loads_and_names_the_record(tmp_path, capsys):
    ontology = _seeded(tmp_path, INGEST_CONFIG)
    (tmp_path / "batch.ndjson").write_text(BATCH)

    assert main(["ingest", "customers", str(tmp_path / "batch.ndjson"), str(ontology), "--yes"]) == 0
    out = capsys.readouterr()
    assert '"status": "applied"' in out.out
    assert "recorded in _loom_meta.loads as" in out.err
    assert "applied · 1 row(s) into crm.customers" in out.err


def test_ingest_refuses_the_second_run_of_one_file(tmp_path, capsys):
    ontology = _seeded(tmp_path, INGEST_CONFIG)
    batch = tmp_path / "batch.ndjson"
    batch.write_text(BATCH)

    assert main(["ingest", "customers", str(batch), str(ontology), "--yes"]) == 0
    assert main(["ingest", "customers", str(batch), str(ontology), "--yes"]) == 1
    assert "duplicate_load" in capsys.readouterr().err


def test_ingest_names_the_actor_itself_rather_than_letting_the_runtime_guess(
    tmp_path, capsys, monkeypatch
):
    """`default_actor()` lives at this call site and nowhere below it, so a future surface passing
    its own attested identity does not have to un-inherit one."""
    monkeypatch.setenv("LOOM_ACTOR", "ci:nightly")
    ontology = _seeded(tmp_path, INGEST_CONFIG)
    (tmp_path / "batch.ndjson").write_text(BATCH)
    main(["ingest", "customers", str(tmp_path / "batch.ndjson"), str(ontology), "--yes"])

    from loom.catalog import open_catalogs
    from loom.config import find_config, load_config
    from loom.errors import Diagnostics
    from loom.ingest import LoadLog

    diag = Diagnostics()
    config = load_config(find_config(ontology), diag)
    history = LoadLog(catalog=open_catalogs(config)["rest_main"]).history()
    assert [r["actor"] for r in history] == ["ci:nightly"]


def test_ingest_on_a_deployment_that_refuses_says_so_and_exits_nonzero(tmp_path, capsys):
    ontology = _seeded(tmp_path, INGEST_CONFIG.replace("ingest: allowed", "ingest: refused"))
    (tmp_path / "batch.ndjson").write_text(BATCH)

    assert main(["ingest", "customers", str(tmp_path / "batch.ndjson"), str(ontology)]) == 1
    assert "deployment_refused" in capsys.readouterr().err


def test_ingest_warns_in_words_before_a_replace(tmp_path, capsys):
    """The one mode whose whole effect is on rows nobody named, and no other command in Loom
    destroys data it never read — so the prompt says it in words rather than in a mode name."""
    ontology = _seeded(tmp_path, INGEST_CONFIG.replace("mode: append", "mode: replace"))
    (tmp_path / "batch.ndjson").write_text(BATCH)

    main(["ingest", "customers", str(tmp_path / "batch.ndjson"), str(ontology), "--dry-run"])
    err = capsys.readouterr().err
    assert "replace empties this table first" in err
    assert "every row not in the batch is gone" in err


def test_ingest_reports_an_unknown_entry_without_touching_a_catalog(tmp_path, capsys):
    ontology = _project(tmp_path, INGEST_CONFIG)
    (tmp_path / "batch.ndjson").write_text(BATCH)

    assert main(["ingest", "nope", str(tmp_path / "batch.ndjson"), str(ontology)]) == 1
    assert "unknown ingest entry 'nope'" in capsys.readouterr().err


def test_ingest_reject_to_survives_the_preview_and_loads_the_good_rows(tmp_path, capsys):
    """`--reject-to` used to be dead on the only surface that exposes it: the mandatory preview ran
    without it, refused the batch over the bad row, and the command exited before the real load. The
    flag now reaches both calls."""
    ontology = _seeded(tmp_path, INGEST_CONFIG)
    batch = tmp_path / "batch.ndjson"
    batch.write_text(
        BATCH + '{"customerId": "c10", "name": "K J", "tier": "platinum", "ltv": 1.0}\n'
    )
    rejects = tmp_path / "rejects.ndjson"

    assert main(["ingest", "customers", str(batch), str(ontology),
                 "--reject-to", str(rejects), "--yes"]) == 0
    out = capsys.readouterr()
    assert '"status": "applied"' in out.out
    assert '"rowsWritten": 1' in out.out and '"rowsRejected": 1' in out.out
    assert "platinum" in rejects.read_text()
    assert "were rejected and written to" in out.err


def test_ingest_records_a_refusal_the_operator_actually_hit(tmp_path, capsys):
    """The command previews first and a preview records nothing, so a refused preview used to leave
    no trace at all — *who tried to replace this table* answerable only for loads that worked. When
    `--dry-run` was not asked for, the refusal is run for real so the log gets it."""
    ontology = _seeded(tmp_path, INGEST_CONFIG)
    batch = tmp_path / "batch.ndjson"
    batch.write_text('{"customerId": "c9", "name": "K", "tier": "bronze", "nope": 1}\n')

    assert main(["ingest", "customers", str(batch), str(ontology), "--yes"]) == 1
    assert "unmapped_column" in capsys.readouterr().err

    from loom.catalog import open_catalogs
    from loom.config import find_config, load_config
    from loom.errors import Diagnostics
    from loom.ingest import LoadLog

    diag = Diagnostics()
    config = load_config(find_config(ontology), diag)
    history = LoadLog(catalog=open_catalogs(config)["rest_main"]).history()
    assert [r["status"] for r in history] == ["refused"]
    assert history[0]["rows_written"] == 0


def test_ingest_dry_run_leaves_no_record_of_a_refusal(tmp_path, capsys):
    """...and the other side of it: asking whether a load would work must not write to the lake."""
    ontology = _seeded(tmp_path, INGEST_CONFIG)
    batch = tmp_path / "batch.ndjson"
    batch.write_text('{"customerId": "c9", "name": "K", "tier": "bronze", "nope": 1}\n')

    assert main(["ingest", "customers", str(batch), str(ontology), "--dry-run"]) == 1

    from loom.catalog import LOAD_LOG_TABLE, open_catalogs
    from loom.config import find_config, load_config
    from loom.errors import Diagnostics

    diag = Diagnostics()
    config = load_config(find_config(ontology), diag)
    assert not open_catalogs(config)["rest_main"].table_exists(LOAD_LOG_TABLE)


def test_ingest_names_the_actor_on_a_refusal_too(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LOOM_ACTOR", "ci:nightly")
    ontology = _seeded(tmp_path, INGEST_CONFIG)
    batch = tmp_path / "batch.ndjson"
    batch.write_text('{"customerId": "c9", "name": "K", "tier": "bronze", "nope": 1}\n')
    main(["ingest", "customers", str(batch), str(ontology), "--yes"])

    from loom.catalog import open_catalogs
    from loom.config import find_config, load_config
    from loom.errors import Diagnostics
    from loom.ingest import LoadLog

    diag = Diagnostics()
    config = load_config(find_config(ontology), diag)
    history = LoadLog(catalog=open_catalogs(config)["rest_main"]).history()
    assert [r["actor"] for r in history] == ["ci:nightly"]


# ---- embed ---------------------------------------------------------------------


EMBED_CONFIG = LOCAL_CONFIG + """
mcp:
  embedding:
    provider: local
    model: bge-small
"""


class _StubProvider:
    """Stands in for a real model, so these tests assert the *command* rather than fastembed."""

    model = "bge-small"
    dims = 3

    def embed(self, texts):
        return [(float(len(t)), 1.0, 2.0) for t in texts]


@pytest.fixture
def stub_provider(monkeypatch):
    monkeypatch.setattr("loom.embed.runtime.provider_for", lambda config: _StubProvider())


def _semantic_project(tmp_path: Path, config: str = EMBED_CONFIG) -> Path:
    """The valid fixture with `semantic: name` written into the YAML, applied to a real warehouse.

    Declared in a *copy* rather than in `fixtures/valid`, for the reason M10's first slice gave for
    leaving that fixture alone: it is shared by two dozen tests and by the governance suite that
    masks `Customer.name`."""
    ontology = _seeded(tmp_path, config)
    customer = ontology / "customer.yaml"
    customer.write_text(customer.read_text().replace("searchable:", "semantic: name\n  searchable:"))
    return ontology


def test_embed_takes_a_path_and_a_type_and_nothing_that_describes_the_model():
    """`cmd_ingest`'s rule on the embedding plane: the model lives in `loom.yaml`, because a flag
    that could contradict the reviewed file would write vectors the served surface cannot rank —
    and silently, since the model is folded into every stored hash."""
    args = vars(_parsed(["embed", "ontology", "--type", "Customer"], "cmd_embed"))
    assert set(args) - {"command", "func"} == {"object_type", "path", "dry_run", "remodel", "yes"}
    for forbidden in ("provider", "model", "dims", "batch_size", "table"):
        assert forbidden not in args


def test_embed_without_a_configured_provider_says_so_and_does_not_start(tmp_path, capsys):
    """Absent `mcp.embedding` withholds a tool rather than refusing a deployment — but there is
    nothing for *this* command to do, and saying so beats a traceback."""
    ontology = _semantic_project(tmp_path, LOCAL_CONFIG)

    assert main(["embed", str(ontology)]) == 1
    assert "no 'mcp.embedding'" in capsys.readouterr().err


def test_embed_on_a_spec_that_declares_no_semantic_property_says_so(tmp_path, capsys):
    ontology = _seeded(tmp_path, EMBED_CONFIG)

    assert main(["embed", str(ontology)]) == 1
    assert "nothing to embed" in capsys.readouterr().err


def test_embed_dry_run_reports_the_work_and_writes_nothing(tmp_path, capsys, stub_provider):
    ontology = _semantic_project(tmp_path)
    main(["run", "createOrder", str(ontology), "--param", "orderId=o1",
          "--param", "customer=c1", "--param", "total=5", "--yes"])

    assert main(["embed", str(ontology), "--dry-run"]) == 0
    out = capsys.readouterr()
    assert '"status": "previewed"' in out.out
    assert "loom embed" in out.err

    from loom.catalog import open_catalogs
    from loom.catalog.base import vector_table
    from loom.config import find_config, load_config
    from loom.errors import Diagnostics

    diag = Diagnostics()
    config = load_config(find_config(ontology), diag)
    assert not open_catalogs(config)["rest_main"].table_exists(vector_table("Customer"))


def test_embed_writes_the_sidecar_and_reports_the_model(tmp_path, capsys, stub_provider):
    ontology = _semantic_project(tmp_path)

    assert main(["embed", str(ontology), "--yes"]) == 0
    out = capsys.readouterr()
    assert '"status": "applied"' in out.out
    assert "embedded" in out.err and "bge-small/3d" in out.err


def test_embed_is_idempotent_from_the_command_line(tmp_path, capsys, stub_provider):
    """The second run is the one an operator will schedule, so it is the one worth pinning."""
    ontology = _semantic_project(tmp_path)
    assert main(["embed", str(ontology), "--yes"]) == 0
    capsys.readouterr()

    assert main(["embed", str(ontology), "--yes"]) == 0
    assert '"rowsEmbedded": 0' in capsys.readouterr().out


def test_embed_refuses_an_unknown_type_by_name(tmp_path, capsys, stub_provider):
    ontology = _semantic_project(tmp_path)

    assert main(["embed", str(ontology), "--type", "Ghost"]) == 1
    assert "not declared" in capsys.readouterr().err


def test_embed_refuses_a_masked_semantic_property_rather_than_filling_a_sidecar(tmp_path, capsys,
                                                                               stub_provider):
    """The back door this command must not be: a mask that stops the ranking but not the vector
    behind it withholds nothing. The refusal is `bind_policies`', reached through `bind_reads`."""
    masked = EMBED_CONFIG + """
governance:
  policies:
    - name: hide-name
      objectType: Customer
      mask: [name]
"""
    ontology = _semantic_project(tmp_path, masked)

    assert main(["embed", str(ontology)]) == 1
    err = capsys.readouterr().err
    assert "semantic property" in err and "gradient" in err


def test_a_vector_stamp_prints_as_utc_whatever_the_hosts_zone_is():
    """`loom embed` reads an `embedded_at` back through pyarrow and `loom query --match` reads the
    same value through DuckDB, which converts a `timestamptz` to the *host's* zone. Both print a
    trailing `Z`, so the letter has to be earned rather than assumed: on a machine in New York the
    unconverted form said `05:14Z` for a vector embedded at `09:14Z`, and the two commands disagreed
    about one value."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from loom.cli import _zulu

    when = datetime(2026, 8, 23, 9, 14, tzinfo=UTC)
    assert _zulu(when) == "2026-08-23 09:14Z"
    assert _zulu(when.astimezone(ZoneInfo("America/New_York"))) == "2026-08-23 09:14Z"
    assert _zulu(when.astimezone(ZoneInfo("Asia/Tokyo"))) == "2026-08-23 09:14Z"
