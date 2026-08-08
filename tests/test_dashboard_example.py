"""The shipped dashboard, held to the two claims its README makes.

`examples/retail/dashboard` is an app rather than a spec, so most of what it does is not this
repo's business to assert. Two things are:

**It is a second deployment of the same spec, not a second spec.** The directory's whole premise is
that pointing a socket, writes and (optionally) a policy at `examples/retail/ontology` needs no
ontology edit. That is a claim about a pairing, and a pairing is exactly what `build_server` checks
— so the test is to build the served surface from *that* config and the *shared* ontology and find
the same object types with the action tools added.

**Its browser-facing data plane is one passthrough.** The README's stronger claim is that the
dashboard has no privileged access: every number on the page comes through a tool call, because
there is nowhere else for one to come from. That is a property of the route table, and it stops
being true the moment somebody adds `/api/customers` for convenience. So it is asserted here, where
adding one fails a test instead of quietly widening the surface.

Nothing here starts a server or opens a browser. What needs a live socket is already covered by
`test_mcp_http.py`, against the same transport this app connects to.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from loom.config import load_config
from loom.errors import Diagnostics

pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
pytest.importorskip("duckdb", reason="needs the [duckdb] extra")
pytest.importorskip("mcp", reason="needs the [mcp] extra")

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "retail"
DASHBOARD = EXAMPLE / "dashboard"

# The one route the browser reads data through, and the two that are not it. `/api/refresh` is in
# this list on purpose: it is the ingestion-side recompute, it is *not* a tool call, and the README
# says so out loud — so it belongs in an inventory of what the page can reach rather than hidden by
# an assertion that only counts tool routes.
EXPECTED_ROUTES = {"/", "/api/surface", "/api/call", "/api/refresh"}


@pytest.fixture(scope="module")
def app_module(tmp_path_factory):
    """`app.py` imported as a module, from a copy with a seeded warehouse under it.

    Copied rather than imported in place because importing it is the cheap half; the config it
    loads names `../.warehouse`, and a test that made one next to the shipped example would leave a
    warehouse in the working tree."""
    target = tmp_path_factory.mktemp("dashboard") / "retail"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns(".warehouse"))

    seed_spec = importlib.util.spec_from_file_location("dashboard_seed", target / "seed.py")
    seed = importlib.util.module_from_spec(seed_spec)
    seed_spec.loader.exec_module(seed)
    seed.seed(target)

    spec = importlib.util.spec_from_file_location("dashboard_app", target / "dashboard" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, target


def test_the_dashboard_is_a_deployment_of_the_shipped_ontology(app_module):
    """No spec edit, and `app.py` says which loom.yaml it means rather than discovering one."""
    module, target = app_module
    ontology, config = module.load_project(target / "dashboard" / "loom.yaml")

    assert sorted(ontology.object_types) == ["Customer", "DailySalesPerformance", "Order"]
    assert sorted(ontology.actions) == ["forgetCustomer", "recordOrder", "upgradeTier"]
    # The same spec `../loom.yaml` serves, loaded through the ordinary discovery path.
    shipped, _ = module.load_project(target / "loom.yaml")
    assert sorted(shipped.object_types) == sorted(ontology.object_types)


def test_the_two_deployments_differ_only_in_the_deployment(app_module):
    """stdio + read-only beside http + writes, over one ontology. This is the directory's premise."""
    _, target = app_module
    diag = Diagnostics()
    shipped = load_config(target / "loom.yaml", diag)
    dashboard = load_config(target / "dashboard" / "loom.yaml", diag)
    diag.raise_if_errors()

    assert shipped.mcp.transport == "stdio" and not shipped.mcp.writes
    assert dashboard.mcp.transport == "http" and dashboard.mcp.writes
    # Writes are legal here only because the bind is loopback — the config refuses the combination
    # anywhere else, and this is the assertion that notices if somebody "just" changes the host.
    assert dashboard.mcp.is_loopback
    # Declared, never inferred: a served run has to record something truthful about this deployment.
    assert dashboard.mcp.actor


def test_the_dashboard_surface_is_the_spec_plus_writes(app_module):
    """Building the served surface from this config is what proves the pairing holds."""
    module, target = app_module
    ontology, config = module.load_project(target / "dashboard" / "loom.yaml")
    server, _resolver = module.build_loom_server(ontology, config)

    assert sorted(server.tools) == [
        "get_customer",
        "get_daily_sales_performance",
        "get_order",
        "list_customer",
        "list_daily_sales_performance",
        "list_order",
        "run_forget_customer",
        "run_record_order",
        "run_upgrade_tier",
        "search_customer",
        "search_daily_sales_performance",
        "search_order",
        "traverse",
    ]


def test_the_browser_reaches_the_data_through_exactly_one_route(app_module):
    """The claim that the dashboard has no privileged access, as a property of the route table.

    A per-panel endpoint is where an ontology gets bypassed: a filter the spec never declared, a
    join it never linked, a column a policy withholds. One passthrough leaves nowhere to put any of
    that — so a new route here is a decision that should have to argue with a test."""
    module, target = app_module
    ontology, config = module.load_project(target / "dashboard" / "loom.yaml")

    class _StubClient:
        url = "http://127.0.0.1:8765/mcp"
        server_name = "loom-retail-dashboard"
        tools: list = []

    app = module.build_app(_StubClient(), config, writes=True)
    assert {route.path for route in app.routes} == EXPECTED_ROUTES


def test_the_page_names_no_endpoint_the_app_does_not_serve(app_module):
    """The other half: the HTML must not reach for a route that isn't there — or one that is, but
    outside the inventory above. Catches a panel wired to a URL somebody meant to add and didn't."""
    import re

    _, target = app_module
    page = (target / "dashboard" / "index.html").read_text()
    referenced = set(re.findall(r'fetch\(\s*"(/[^"]*)"', page))
    assert referenced
    assert referenced <= EXPECTED_ROUTES
