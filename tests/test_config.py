from pathlib import Path

from loom.config import CatalogConfig, find_config, load_config
from loom.errors import Diagnostics

VALID = """
version: 0
catalogs:
  main:
    type: iceberg-rest
    uri: https://catalog.internal/api
    warehouse: s3://lake/warehouse
    auth: { token: abc123 }
engine:
  type: duckdb
  options: { threads: 4 }
mcp:
  name: retail
  transport: stdio
"""


def _load(tmp_path: Path, text: str, name: str = "loom.yaml"):
    p = tmp_path / name
    p.write_text(text)
    diag = Diagnostics()
    return load_config(p, diag), diag


def test_loads_the_spec_example(tmp_path: Path):
    cfg, diag = _load(tmp_path, VALID)
    assert diag.errors == []
    assert cfg.engine.type == "duckdb" and cfg.engine.options == {"threads": 4}
    assert cfg.mcp.name == "retail" and cfg.mcp.transport == "stdio"

    main = cfg.catalogs["main"]
    assert main == CatalogConfig(
        name="main",
        type="iceberg-rest",
        uri="https://catalog.internal/api",
        warehouse="s3://lake/warehouse",
        properties={"token": "abc123"},  # `auth` is opaque, merged for the catalog client
    )


def test_defaults_when_sections_are_absent(tmp_path: Path):
    cfg, diag = _load(tmp_path, "catalogs:\n  c: { type: iceberg-rest, uri: http://x }\n")
    assert diag.errors == []
    assert cfg.engine.type == "duckdb"
    assert cfg.mcp.name == "loom" and cfg.mcp.transport == "stdio"
    # A config that says nothing about writes serves none. Declaring an action and serving it to
    # every client that connects are different decisions, and only one of them is in the spec.
    assert cfg.mcp.writes is False and cfg.mcp.actor is None


def test_writes_and_actor_are_read_from_the_deployment_file(tmp_path: Path):
    cfg, diag = _load(
        tmp_path,
        "catalogs:\n  c: { type: iceberg-rest, uri: http://x }\n"
        "mcp: { writes: true, actor: 'agent:support-bot' }\n",
    )
    assert diag.errors == []
    assert cfg.mcp.writes is True and cfg.mcp.actor == "agent:support-bot"


def test_a_non_boolean_writes_is_refused_rather_than_coerced(tmp_path: Path):
    """`writes: "no"` is truthy in Python — the one misreading of this key that costs a row."""
    cfg, diag = _load(
        tmp_path, "catalogs:\n  c: { type: iceberg-rest, uri: http://x }\nmcp: { writes: 'no' }\n"
    )
    assert "'mcp.writes' must be true or false" in " | ".join(e.message for e in diag.errors)
    assert cfg.mcp.writes is False


def test_an_empty_actor_is_refused_rather_than_recorded(tmp_path: Path):
    """An actor is a claim about who writes. A blank one is not a smaller claim, it is a broken
    record, and `unknown` already exists to mean "nobody said"."""
    cfg, diag = _load(
        tmp_path, "catalogs:\n  c: { type: iceberg-rest, uri: http://x }\nmcp: { actor: '  ' }\n"
    )
    assert "'mcp.actor' must be a non-empty string" in " | ".join(e.message for e in diag.errors)
    assert cfg.mcp.actor is None


def test_reports_every_problem_in_one_pass(tmp_path: Path):
    cfg, diag = _load(
        tmp_path,
        """
        version: 1
        catalogs:
          bad: { type: iceberg-postgres, uri: x }
          nouri: { type: iceberg-rest }
        engine: { type: spark }
        mcp: { transport: grpc }
        """,
    )
    messages = " | ".join(e.message for e in diag.errors)
    assert "unsupported config version" in messages
    assert "unknown catalog type 'iceberg-postgres'" in messages
    assert "missing required key 'uri'" in messages
    assert "unknown engine type 'spark'" in messages
    assert "unsupported mcp transport 'grpc'" in messages
    assert cfg.catalogs == {}  # neither catalog was usable


def test_typo_in_a_key_gets_a_suggestion(tmp_path: Path):
    _, diag = _load(tmp_path, "catalogs:\n  c: { type: iceberg-rest, uri: x, warehous: y }\n")
    assert any(e.hint == "did you mean 'warehouse'?" for e in diag.errors)


def test_sql_catalog_requires_a_warehouse(tmp_path: Path):
    _, diag = _load(tmp_path, "catalogs:\n  c: { type: iceberg-sql, uri: 'sqlite:///x.db' }\n")
    assert any("requires 'warehouse'" in e.message for e in diag.errors)


def test_missing_catalogs_is_an_error(tmp_path: Path):
    _, diag = _load(tmp_path, "engine: { type: duckdb }\n")
    assert any("no 'catalogs' declared" in e.message for e in diag.errors)


def test_declared_governance_policies_refuse_to_start(tmp_path: Path):
    """Silently ignoring an access policy is worse than not booting."""
    _, diag = _load(
        tmp_path,
        """
        catalogs: { c: { type: iceberg-rest, uri: x } }
        governance:
          policies:
            - { on: Customer, where: "tier == 'gold'" }
        """,
    )
    assert any("not implemented yet" in e.message and "refusing to start" in e.message for e in diag.errors)


def test_empty_governance_block_is_fine(tmp_path: Path):
    _, diag = _load(
        tmp_path, "catalogs: { c: { type: iceberg-rest, uri: x } }\ngovernance: { policies: [] }\n"
    )
    assert diag.errors == []


def test_local_paths_resolve_against_the_config_file_not_the_cwd(tmp_path: Path):
    """A relative warehouse must mean the same directory wherever loom is invoked from."""
    project = tmp_path / "project"
    project.mkdir()
    cfg, diag = _load(
        project,
        "catalogs:\n  local: { type: iceberg-sql, uri: 'sqlite:///.warehouse/c.db', warehouse: 'file://.warehouse' }\n",
    )
    assert diag.errors == []
    local = cfg.catalogs["local"]
    assert local.uri == f"sqlite:///{project.resolve()}/.warehouse/c.db"
    assert local.warehouse == f"file://{project.resolve()}/.warehouse"


def test_absolute_and_remote_locations_are_left_alone(tmp_path: Path):
    cfg, _ = _load(
        tmp_path,
        "catalogs:\n  a: { type: iceberg-sql, uri: 'sqlite:////abs/c.db', warehouse: 's3://bucket/wh' }\n",
    )
    assert cfg.catalogs["a"].uri == "sqlite:////abs/c.db"
    assert cfg.catalogs["a"].warehouse == "s3://bucket/wh"


def test_find_config_prefers_inside_then_alongside(tmp_path: Path):
    (tmp_path / "ontology").mkdir()
    alongside = tmp_path / "loom.yaml"
    alongside.write_text("catalogs: {}\n")
    assert find_config(tmp_path / "ontology") == alongside

    inside = tmp_path / "ontology" / "loom.yaml"
    inside.write_text("catalogs: {}\n")
    assert find_config(tmp_path / "ontology") == inside


def test_unreadable_and_malformed_files_report_rather_than_raise(tmp_path: Path):
    cfg, diag = _load(tmp_path, "catalogs: [not, a, mapping]\n")
    assert any("'catalogs' must be a mapping" in e.message for e in diag.errors)

    cfg2, diag2 = _load(tmp_path, "- just\n- a\n- list\n", name="other.yaml")
    assert cfg2 is None
    assert any("must be a mapping" in e.message for e in diag2.errors)
