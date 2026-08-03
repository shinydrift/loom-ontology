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


# ---- the address, and the posture it decides ------------------------------------
#
# Every check below is an *error* rather than a warning, and lives here rather than in `cmd_serve`,
# so `loom validate` reports it and the server never starts. That is the posture `cmd_serve` already
# takes when a catalog will not open — and nobody reads the third line of a banner on a server that
# came up anyway.


def _mcp(tmp_path: Path, body: str):
    return _load(tmp_path, f"catalogs:\n  c: {{ type: iceberg-rest, uri: http://x }}\nmcp:\n{body}")


def test_http_is_a_transport_now_and_binds_to_loopback_by_default(tmp_path: Path):
    """The default that matters. `0.0.0.0` and `127.0.0.1` are the same posture question `loom
    apply` answers by refusing to run unattended, and the wrong answer is somebody's lake on the
    internet."""
    cfg, diag = _mcp(tmp_path, "  transport: http\n")
    assert diag.errors == []
    assert cfg.mcp.transport == "http"
    assert cfg.mcp.host == "127.0.0.1" and cfg.mcp.port == 8000 and cfg.mcp.path == "/mcp"
    assert cfg.mcp.is_loopback and cfg.mcp.address() == "http://127.0.0.1:8000/mcp"


def test_the_whole_address_is_config_including_the_port(tmp_path: Path):
    """No flags, for the reason the first M4 slice gave `writes` — a flag lets one invocation
    contradict the file an operator reviews. A port is the weakest case that argument has to carry
    and it goes here anyway: a file describing half an address does not describe the server."""
    cfg, diag = _mcp(tmp_path, "  transport: http\n  host: ::1\n  port: 9001\n  path: /ontology/\n")
    assert diag.errors == []
    assert (cfg.mcp.host, cfg.mcp.port, cfg.mcp.path) == ("::1", 9001, "/ontology")
    # A v6 literal is bracketed for the URL and the trailing slash is normalised away, so the
    # banner, the route and the Host allow-list cannot disagree about which endpoint this is.
    assert cfg.mcp.address() == "http://[::1]:9001/ontology"
    assert cfg.mcp.is_loopback


def test_a_bad_port_is_refused_rather_than_coerced(tmp_path: Path):
    """`isinstance(True, int)` is True, and `port: yes` is a plausible YAML typo for a number."""
    for value, in [("0",), ("70000",), ("'8000'",), ("yes",)]:
        cfg, diag = _mcp(tmp_path, f"  transport: http\n  port: {value}\n")
        assert any("'mcp.port' must be an integer" in e.message for e in diag.errors), value
        assert cfg.mcp.port == 8000


def test_a_path_that_is_not_a_path_is_refused(tmp_path: Path):
    cfg, diag = _mcp(tmp_path, "  transport: http\n  path: mcp\n")
    assert any("must be a string beginning with '/'" in e.message for e in diag.errors)
    assert cfg.mcp.path == "/mcp"


def test_an_address_under_stdio_is_refused_rather_than_ignored(tmp_path: Path):
    """`_check_governance`'s rule, applied to a second set of keys. A stdio server that quietly
    dropped `host: 0.0.0.0` would read, to whoever wrote it, exactly like one that honoured it."""
    _, diag = _mcp(tmp_path, "  transport: stdio\n  host: 0.0.0.0\n  port: 9000\n")
    error = next(e for e in diag.errors if "has no address" in e.message)
    assert error.message == "mcp.host, mcp.port set but transport is 'stdio', which has no address"
    assert "set 'transport: http'" in error.hint


def test_a_non_loopback_bind_must_say_what_hostnames_it_answers_to(tmp_path: Path):
    """DNS-rebinding protection stays on, and the allow-list is optional exactly where Loom can
    derive it. Off the loopback it cannot know the name the world reaches this by, so it asks."""
    _, diag = _mcp(tmp_path, "  transport: http\n  host: 0.0.0.0\n")
    assert any("'mcp.allowed_hosts' is unset" in e.message for e in diag.errors)

    cfg, diag = _mcp(tmp_path, "  transport: http\n  host: 0.0.0.0\n  allowed_hosts: [loom.internal:8000]\n")
    assert diag.errors == []
    assert cfg.mcp.host_allow_list() == ("loom.internal:8000",)


def test_a_loopback_bind_derives_the_three_names_it_can_be_reached_by(tmp_path: Path):
    cfg, diag = _mcp(tmp_path, "  transport: http\n  port: 9001\n")
    assert diag.errors == []
    assert cfg.mcp.host_allow_list() == ("127.0.0.1:9001", "localhost:9001", "[::1]:9001")


def test_writes_over_a_network_are_refused_at_startup(tmp_path: Path):
    """**Writes over a socket are not the same decision as writes over a pipe**, and the difference
    is reachability rather than transport.

    `mcp.actor` lives in `loom.yaml`, so it always named a deployment rather than a session — three
    stdio clients reading one file already record one string. What a non-loopback bind changes is
    who is *permitted to be* one of those callers: over stdio, whoever can run the binary; here,
    whoever can reach the port. That is where `actor:` stops being a statement anybody checked, so
    the combination refuses rather than warns."""
    _, diag = _mcp(
        tmp_path, "  transport: http\n  host: 0.0.0.0\n  allowed_hosts: [x:8000]\n  writes: true\n"
    )
    error = next(e for e in diag.errors if "'mcp.writes' is true on a non-loopback bind" in e.message)
    assert "whoever can reach the port" in error.message
    assert "names a deployment, not a caller" in error.hint


def test_writes_on_a_loopback_bind_serve_normally(tmp_path: Path):
    """The limit is drawn on the bind, so the local case is untouched — and it is the case an
    agent runtime on somebody's laptop actually uses."""
    cfg, diag = _mcp(tmp_path, "  transport: http\n  writes: true\n  actor: agent:support-bot\n")
    assert diag.errors == []
    assert cfg.mcp.writes is True and cfg.mcp.is_loopback


def test_a_hostname_loom_cannot_prove_is_local_is_treated_as_remote(tmp_path: Path):
    """Fails closed. Two refusals hang off this answer and both should err towards not starting."""
    _, diag = _mcp(tmp_path, "  transport: http\n  host: loom.internal\n  writes: true\n")
    assert any("not loopback" in e.message for e in diag.errors)
    assert any("non-loopback bind" in e.message for e in diag.errors)


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


def test_governance_policies_are_parsed(tmp_path: Path):
    """The block that used to be refused wholesale now loads — see `test_governance.py` for what a
    policy may say. What survives from that refusal is the rule underneath it: a key Loom cannot
    enforce is still refused, one key at a time rather than one block at a time."""
    config, diag = _load(
        tmp_path,
        """
        catalogs: { c: { type: iceberg-rest, uri: x } }
        governance:
          policies:
            - { name: hide-pii, objectType: Customer, mask: [ssn] }
        """,
    )
    assert diag.errors == []
    assert [(p.name, p.object_type, p.mask) for p in config.policies] == [("hide-pii", "Customer", ("ssn",))]


def test_unenforceable_policy_keys_refuse_to_start(tmp_path: Path):
    """Silently ignoring an access policy is worse than not booting, and that did not change when
    enforcement landed — it moved down to the key. A clause Loom cannot honour reads, to whoever
    wrote it, exactly like one it obeyed.

    Both fates a reserved key can have are in this one config, and both have now happened. `rows`
    and `when` were reserved and are **enforced**, which is what a reservation is for: the config
    that named one was refused, so nobody was running one when the meaning arrived. `audit`
    **left** — see `governance.MOVED_KEYS` — because neither half of what it named is a policy.

    `RESERVED_KEYS` is gone with the last of its entries, so what a key that is neither enforced
    nor moved gets now is `check_keys`' own refusal, naming it."""
    _, diag = _load(
        tmp_path,
        """
        catalogs: { c: { type: iceberg-rest, uri: x } }
        governance:
          policies:
            - name: eu-only
              objectType: Order
              mask: [notes]
              rows: "object.region == 'EU'"
              audit: { retain: 30d }
              retain: 30d
        """,
    )
    assert any("'audit'" in e.message and "no longer a policy key" in e.message for e in diag.errors)
    assert any("'retain'" in e.message for e in diag.errors)
    assert not any("'rows'" in e.message for e in diag.errors)


def test_the_edit_log_posture_is_optional_unless_a_deployment_says_otherwise(tmp_path: Path):
    """`mcp.writes`' default, for `mcp.writes`' reason.

    An upgrade and a catalog that implements no edit-log port are two things that happen for
    unrelated reasons, and a deployment that never asked for this posture is not asking to stop
    working."""
    config, diag = _load(tmp_path, "catalogs: { c: { type: iceberg-rest, uri: x } }")
    assert diag.errors == [] and config.edit_log == "optional"

    config, diag = _load(
        tmp_path,
        """
        catalogs: { c: { type: iceberg-rest, uri: x } }
        governance:
          edit_log: required
        """,
    )
    assert diag.errors == [] and config.edit_log == "required"


def test_an_edit_log_posture_loom_cannot_read_is_refused_rather_than_defaulted(tmp_path: Path):
    """The same rule the block itself follows: a config that is silently ignored reads, to whoever
    wrote it, exactly like one that was obeyed — and this is the key where being ignored means
    writing unrecorded rows into somebody's lake.

    A boolean is refused with the rest, and that is the naming decision showing through: this key
    is a posture about a deployment, not a switch that could be true. `edit_log: true` would have to
    mean "required", and a config that has to be interpreted is one this grammar declines to
    interpret."""
    _, diag = _load(
        tmp_path,
        """
        catalogs: { c: { type: iceberg-rest, uri: x } }
        governance:
          edit_log: requried
        """,
    )
    (problem,) = [e for e in diag.errors if "edit_log" in e.message]
    assert "optional, required" in problem.message
    assert "did you mean 'required'?" in (problem.hint or "")

    _, diag = _load(
        tmp_path,
        """
        catalogs: { c: { type: iceberg-rest, uri: x } }
        governance:
          edit_log: true
        """,
    )
    assert any("edit_log" in e.message for e in diag.errors)


def test_a_malformed_mask_is_reported_rather_than_crashing(tmp_path: Path):
    """The shape checks either side of "withholds nothing", which moved up a level when `rows:`
    gave a policy a second way to withhold something."""
    _, diag = _load(
        tmp_path,
        """
        catalogs: { c: { type: iceberg-rest, uri: x } }
        governance:
          policies:
            - { name: a, objectType: Customer, mask: ssn }
            - { name: b, objectType: Customer, mask: ["", "ok"] }
        """,
    )
    assert any("'mask' must be a list of property names" in e.message for e in diag.errors)
    assert any("must be non-empty property names" in e.message for e in diag.errors)


def test_a_policy_written_with_on_is_reported_not_crashed(tmp_path: Path):
    """`on:` is the obvious spelling for `objectType:` and YAML 1.1 resolves the bare key to the
    boolean `True`. It has to arrive as an unexpected key rather than as a `TypeError` out of
    difflib, which is what `check_keys` used to do with a key that was not a string."""
    _, diag = _load(
        tmp_path,
        """
        catalogs: { c: { type: iceberg-rest, uri: x } }
        governance:
          policies:
            - { name: p, on: Customer, mask: [ssn] }
        """,
    )
    assert any("unexpected key 'True'" in e.message for e in diag.errors)


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


# ---- mcp.auth ------------------------------------------------------------------------

AUTH_BASE = """
version: 0
catalogs:
  main: {type: iceberg-rest, uri: https://catalog.internal/api}
engine: {type: duckdb}
mcp:
  transport: http
%s
"""


def _auth(tmp_path: Path, body: str):
    return _load(tmp_path, AUTH_BASE % body)


def test_auth_needs_an_issuer_an_audience_and_a_key_set(tmp_path: Path):
    """All three required, none derived. Each names a thing only a deployment can answer."""
    _, diag = _auth(tmp_path, "  auth:\n    issuer: https://issuer.test\n")
    text = str(diag)
    assert "missing required key 'audience'" in text
    assert "missing required key 'jwks_uri'" in text


def test_auth_refuses_a_key_set_fetched_over_cleartext(tmp_path: Path):
    """Swap the key set in transit and you mint principals, so the fetch has to be protected."""
    _, diag = _auth(
        tmp_path,
        "  auth:\n    issuer: https://issuer.test\n    audience: loom\n"
        "    jwks_uri: http://issuer.test/jwks.json\n",
    )
    assert "not https" in str(diag)


def test_auth_allows_a_loopback_key_set_over_http(tmp_path: Path):
    """The one exception, and the same one `host` already makes: what cannot leave the machine
    cannot be intercepted off it. It is what makes a local test of the real path possible."""
    config, diag = _auth(
        tmp_path,
        "  auth:\n    issuer: https://issuer.test\n    audience: loom\n"
        "    jwks_uri: http://127.0.0.1:9999/jwks.json\n",
    )
    assert not diag.errors
    assert config.mcp.auth.jwks_uri == "http://127.0.0.1:9999/jwks.json"


def test_auth_bounds_the_clock_skew(tmp_path: Path):
    """Skew is for drift between two machines. A deployment wanting an hour is asking the wrong
    system for a longer session."""
    _, diag = _auth(
        tmp_path,
        "  auth:\n    issuer: https://issuer.test\n    audience: loom\n"
        "    jwks_uri: https://issuer.test/jwks.json\n    clock_skew: 3600\n",
    )
    assert "clock_skew" in str(diag)


def test_auth_on_stdio_is_refused_rather_than_ignored(tmp_path: Path):
    """A spawned server carries no bearer token, so `auth:` there could only ever be ignored — and a
    config that is silently ignored reads, to whoever wrote it, exactly like one that was obeyed."""
    _, diag = _load(
        tmp_path,
        "version: 0\ncatalogs:\n  main: {type: iceberg-rest, uri: https://c.internal/api}\n"
        "engine: {type: duckdb}\nmcp:\n  transport: stdio\n  auth:\n"
        "    issuer: https://issuer.test\n    audience: loom\n    jwks_uri: https://issuer.test/jwks.json\n",
    )
    assert "carries no bearer token" in str(diag)


def test_writes_on_a_public_bind_stay_refused_without_auth(tmp_path: Path):
    """M4's refusal, unchanged where nothing attests a caller."""
    _, diag = _auth(tmp_path, "  host: 0.0.0.0\n  allowed_hosts: [loom.internal:8000]\n  writes: true\n")
    assert "refusing to serve a write surface" in str(diag)


def test_writes_on_a_public_bind_are_permitted_once_callers_are_attested(tmp_path: Path):
    """The refusal M6 narrowed, and the thing this milestone was for.

    spec-v0's open edge said it in as many words — "a loopback HTTP server may write today because
    its callers are the same set stdio's were; a public one may not, until this closes". `auth:` is
    what closes it: every caller is checked and recorded by name, so `actor` naming a deployment is
    no longer the only thing the log would hold."""
    config, diag = _auth(
        tmp_path,
        "  host: 0.0.0.0\n  allowed_hosts: [loom.internal:8000]\n  writes: true\n"
        "  auth:\n    issuer: https://issuer.test\n    audience: loom\n"
        "    jwks_uri: https://issuer.test/jwks.json\n",
    )
    assert not diag.errors
    assert config.mcp.writes and config.mcp.attests


def test_attests_is_false_for_stdio_whatever_auth_says(tmp_path: Path):
    """The predicate is about the *surface*, not about the auth block — which is why it is one
    property rather than a condition three call sites re-derive."""
    from loom.config import McpAuth, McpConfig

    auth = McpAuth(issuer="https://issuer.test", audience="loom", jwks_uri="https://issuer.test/jwks.json")
    assert not McpConfig(transport="stdio", auth=auth).attests
    assert McpConfig(transport="http", auth=auth).attests
    assert not McpConfig(transport="http").attests


def test_claims_are_declared_with_a_closed_type_vocabulary(tmp_path: Path):
    """**The declaration a policy's `principal.<claim>` is checked against.**

    In `loom.yaml` beside the issuer that mints them, never in an ontology: a spec describes what
    exists, and who is asking is a fact about a deployment. That placement is also what keeps the
    expression language's rule intact — *a reference is legal where its declaration is in scope*.

    A declaration and never a requirement: nothing here makes a token unbelievable. A caller whose
    token lacks a declared claim is attested exactly as before, and the policy naming it fails
    closed — requiring it would convert *withhold more from this caller* into *serve nobody*."""
    config, diag = _auth(
        tmp_path,
        "  auth:\n    issuer: https://issuer.test\n    audience: loom\n"
        "    jwks_uri: https://issuer.test/jwks.json\n"
        "    claims:\n      dept: string\n      groups: string[]\n      verified: boolean\n",
    )
    assert not diag.errors
    assert {name: t.spelling for name, t in config.mcp.auth.claims.items()} == {
        "dept": "string", "groups": "string[]", "verified": "boolean"
    }


def test_a_claim_type_outside_the_vocabulary_is_refused(tmp_path: Path):
    """There is no number in it: a JSON number's Loom type is ambiguous (`int`? `double`?) and
    nothing motivating needs one. The set may only ever grow, which accepts configs refused today
    and can change the meaning of none."""
    _, diag = _auth(
        tmp_path,
        "  auth:\n    issuer: https://issuer.test\n    audience: loom\n"
        "    jwks_uri: https://issuer.test/jwks.json\n    claims:\n      age: integer\n",
    )
    assert "'mcp.auth.claims.age' must be one of" in str(diag)


def test_the_claims_every_token_carries_cannot_be_redeclared(tmp_path: Path):
    """`sub` and `iss` are `require`d by the verifier. A config giving one another type describes a
    token this deployment would have refused, and the honest reading of the two statements together
    is that one of them is wrong."""
    _, diag = _auth(
        tmp_path,
        "  auth:\n    issuer: https://issuer.test\n    audience: loom\n"
        "    jwks_uri: https://issuer.test/jwks.json\n    claims:\n      sub: boolean\n",
    )
    assert "redeclares 'sub'" in str(diag)
