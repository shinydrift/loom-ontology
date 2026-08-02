"""Governance — the grammar, the binding, and the two paths that must filter identically.

The claim this file exists to hold down is M5's own: *a direct call and an agent call are filtered
the same way*. So almost every assertion below is made twice — once against a `Resolver` (which is
what `loom query` holds) and once against the generated tool that wraps it — and the interesting
ones are the structural pair: a masked property is never *selected*, and no layer above the
resolver can put it back.

The write path is here too, and not as an afterthought. A mask enforced on reads alone would be one
`dryRun` away from being read out of an action's `before`, which is why `_Run._project` withholds
the same set and why a policy that an action contradicts is refused before anything runs.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from loom import build
from loom.action import PREVIEWED, EditLog, build_runtime
from loom.config import LoomConfig, McpConfig
from loom.errors import Diagnostics, SourceLoc
from loom.governance import (
    ENFORCED_KEYS,
    POLICY_KEYS,
    RESERVED_KEYS,
    Policy,
    PolicyError,
    PolicySet,
    bind_policies,
    parse_policies,
)
from loom.mcp.registry import build_tools
from loom.mcp.server import build_server
from loom.query.engine import Capabilities, CompiledQuery
from loom.resolver import Resolver, ResolverError

# The runtime's fakes, not a second pair of them — see `test_mcp_registry`'s note.
from test_action import CUSTOMERS, FakeRowCatalog

VALID = Path(__file__).parent / "fixtures" / "valid"


class RecordingEngine:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.plans = []

    def capabilities(self):
        return Capabilities(name="recording")

    def compile(self, plan):
        self.plans.append(plan)
        return CompiledQuery(sql="<recorded>")

    def execute(self, compiled):
        return self.rows


@pytest.fixture
def ontology():
    ont, _ = build(VALID)
    return ont


def _bound(ontology, *policies: Policy) -> PolicySet:
    return bind_policies(ontology, policies)


def _resolver(ontology, rows=(), policies=()):
    return Resolver(
        ontology=ontology, engine=RecordingEngine(rows), policies=bind_policies(ontology, policies)
    )


def _parse(entries):
    diag = Diagnostics()
    return parse_policies(entries, SourceLoc("loom.yaml"), diag), diag


HIDE_LTV = Policy(name="hide-ltv", object_type="Customer", mask=("ltv",))
HIDE_NAME = Policy(name="hide-name", object_type="Customer", mask=("name",))


# ---- the grammar ----------------------------------------------------------------


def test_every_policy_key_is_either_enforced_or_reserved():
    """`negotiate.NEGOTIATED`'s device, applied to a grammar instead of a dataclass.

    A key that is neither is the third kind Loom has been bitten by once already — accepted,
    unenforced, and silent about it, which is how `loom.managed` was written by `apply` and read by
    nothing for two milestones. Adding one to `POLICY_KEYS` fails here until somebody says which it
    is."""
    assert ENFORCED_KEYS.isdisjoint(RESERVED_KEYS)
    assert ENFORCED_KEYS | set(RESERVED_KEYS) == POLICY_KEYS


def test_a_reserved_key_is_refused_with_the_reason_it_is_reserved():
    """Refused, never ignored — the rule the whole `governance:` block used to be refused under."""
    _, diag = _parse([{"name": "p", "objectType": "Customer", "mask": ["ltv"], "rows": "x == 1"}])
    assert any("'rows'" in e.message and "not enforced yet" in e.message for e in diag.errors)


def test_a_principal_conditioned_policy_says_what_is_missing_and_what_to_do_instead():
    """`when:` is the clause an attested caller turns on, and until then the honest answer is not
    "ignored" and not "supported": it is *this deployment cannot tell callers apart*, plus the
    posture that works today — one deployment per audience."""
    _, diag = _parse([{"name": "p", "objectType": "Customer", "mask": ["ltv"], "when": "principal.id == 'x'"}])
    (problem,) = [e for e in diag.errors if "'when'" in e.message]
    assert "attest" in problem.message
    assert "one deployment per audience" in (problem.hint or "")


def test_a_policy_that_withholds_nothing_is_refused():
    """The failure this module exists against: a config that reads like protection and enforces
    none. A no-op policy is indistinguishable, in a review, from one that is working."""
    _, diag = _parse([{"name": "p", "objectType": "Customer"}])
    assert any("withholds nothing" in e.message for e in diag.errors)

    _, diag = _parse([{"name": "p", "objectType": "Customer", "mask": []}])
    assert any("withholds nothing" in e.message for e in diag.errors)


def test_policies_need_names_because_a_refusal_names_them():
    _, diag = _parse([{"objectType": "Customer", "mask": ["ltv"]}])
    assert any("non-empty 'name'" in e.message for e in diag.errors)

    _, diag = _parse(
        [
            {"name": "dup", "objectType": "Customer", "mask": ["ltv"]},
            {"name": "dup", "objectType": "Customer", "mask": ["name"]},
        ]
    )
    assert any("duplicate policy name" in e.message for e in diag.errors)


def test_the_grammar_accumulates_rather_than_stopping_at_the_first_problem():
    """The bargain every other Loom grammar makes with whoever is writing it."""
    _, diag = _parse([{"name": "a"}, {"name": "b", "objectType": "Customer"}, "not-a-mapping"])
    assert len(diag.errors) == 3


# ---- binding a policy to an ontology --------------------------------------------


def test_a_policy_naming_something_the_spec_does_not_declare_is_refused(ontology):
    """A mask is the config whose typo is invisible in the output it produces: a policy protecting
    `sssn` withholds nothing and looks exactly like one that works."""
    with pytest.raises(PolicyError) as e:
        _bound(ontology, Policy(name="p", object_type="Custumer", mask=("ltv",)))
    assert "does not declare" in str(e.value) and "did you mean 'Customer'" in str(e.value)

    with pytest.raises(PolicyError) as e:
        _bound(ontology, Policy(name="p", object_type="Customer", mask=("lvt",)))
    assert "not a declared property" in str(e.value) and "did you mean 'ltv'" in str(e.value)


def test_a_primary_key_cannot_be_masked(ontology):
    """Every surface addresses a row by it, so withholding it withholds the object rather than a
    property — and it is what guarantees a projection is never empty."""
    with pytest.raises(PolicyError) as e:
        _bound(ontology, Policy(name="p", object_type="Customer", mask=("customerId",)))
    assert "primary key" in str(e.value)


def test_a_property_a_link_joins_on_cannot_be_masked(ontology):
    """`Order.customerId` is `placedBy`'s own end: the value is the link's whole meaning."""
    with pytest.raises(PolicyError) as e:
        _bound(ontology, Policy(name="p", object_type="Order", mask=("customerId",)))
    assert "linkType 'placedBy'" in str(e.value)


def test_a_property_an_action_reads_or_writes_cannot_be_masked(ontology):
    """The refusal that is about a *combination*: the spec is fine, the policy is fine, and the
    deployment of the two together cannot stand.

    A validation rule reading a withheld property is an oracle the caller drives — `upgradeTier`
    refuses when `newTier == object.tier`, so a caller learns a masked tier in three calls. An
    effect writing one destroys data this deployment says the caller may not see. Both are static
    facts about the spec, so both are settled before anything runs."""
    with pytest.raises(PolicyError) as e:
        _bound(ontology, Policy(name="p", object_type="Customer", mask=("tier",)))
    assert "action 'upgradeTier' writes it" in str(e.value)

    with pytest.raises(PolicyError) as e:
        _bound(ontology, Policy(name="p", object_type="Order", mask=("placedAt",)))
    assert "action 'createOrder' writes it" in str(e.value)


def test_every_problem_is_reported_at_once(ontology):
    """`check_capabilities`' bargain: somebody reconciling a policy file with a spec learns the
    whole of what disagrees in one reading."""
    with pytest.raises(PolicyError) as e:
        _bound(
            ontology,
            Policy(name="a", object_type="Nope", mask=("x",)),
            Policy(name="b", object_type="Customer", mask=("customerId", "tier")),
        )
    text = str(e.value)
    assert "policy 'a'" in text and "primary key" in text and "upgradeTier" in text


def test_a_bound_policy_set_lists_masks_in_the_spec_s_own_order(ontology):
    """What a caller sees withheld should read like the spec it is withheld from, not like the
    order two policies happened to be written in."""
    bound = _bound(
        ontology,
        Policy(name="b", object_type="Customer", mask=("ltv",)),
        Policy(name="a", object_type="Customer", mask=("name",)),
    )
    assert bound.masked("Customer") == ("name", "ltv")
    assert bound.masked_by("Customer", "ltv") == "b"
    assert bound.masked_by("Customer", "tier") is None


def test_an_unbound_policy_set_refuses_to_exist():
    """The `loom.managed` failure, made unreachable rather than documented: a `PolicySet` holding
    policies that withhold nothing would enforce nothing while every layer above reported that a
    policy was in force."""
    with pytest.raises(ValueError) as e:
        PolicySet(policies=(HIDE_LTV,))
    assert "bind_policies" in str(e.value)


def test_policies_subtract_and_never_add(ontology):
    """The invariant that lets `mcp.writes` stay a switch of its own and makes composition total:
    adding a policy can only ever withhold more, so declaration order cannot matter."""
    one = _bound(ontology, HIDE_LTV)
    two = _bound(ontology, HIDE_LTV, HIDE_NAME)
    reversed_ = _bound(ontology, HIDE_NAME, HIDE_LTV)
    assert set(one.masked("Customer")) <= set(two.masked("Customer"))
    assert set(two.masked("Customer")) == set(reversed_.masked("Customer"))
    # And nothing a policy can say adds a property the spec did not declare.
    assert set(two.masked("Customer")) <= set(ontology.object_types["Customer"].properties)


# ---- the read path: withheld by never being selected -----------------------------


def test_a_masked_property_is_never_asked_for(ontology):
    """The strongest form available, and the reason enforcement is on the *projection*: the column
    is not in the plan, so it is not in the result set, so there is no layer above that could return
    it by forgetting to drop it."""
    r = _resolver(ontology, policies=(HIDE_LTV,))
    r.get("Customer", "c1")
    projected = {c.column for c in r.engine.plans[-1].columns}
    assert "lifetime_value" not in projected
    assert projected == {"id", "full_name", "tier"}


def test_every_read_verb_withholds_the_same_set(ontology):
    """Four verbs, one projection. A mask that reached three of them would be a mask an agent walks
    around by using the fourth."""
    r = _resolver(ontology, policies=(HIDE_LTV,))
    for call in (
        lambda: r.get("Customer", "c1"),
        lambda: r.list("Customer"),
        lambda: r.search("Customer", {"tier": "gold"}),
        lambda: r.traverse("Order", "o1", "placedBy"),
    ):
        call()
        assert "lifetime_value" not in {c.column for c in r.engine.plans[-1].columns}


def test_a_traverse_is_filtered_by_where_it_lands(ontology):
    """`traverse` projects the objects at the *other* end, so the mask that applies is the target's.
    Reading it from the source would leave every link a way around one."""
    r = _resolver(ontology, policies=(HIDE_LTV,))
    r.traverse("Customer", "c1", "orders")  # lands on Order, which nothing withholds
    assert {c.column for c in r.engine.plans[-1].columns} == {"id", "customer_id", "total_amount", "created_at"}


def test_filtering_on_a_masked_property_is_refused_rather_than_answered(ontology):
    """An empty result would be an oracle: a substring filter on a withheld column binary-searches
    its value in a handful of calls, and an exact one confirms a guess. The refusal gives away only
    what the mask already announced, which is the rule — the schema is public, the data is not."""
    r = _resolver(ontology, policies=(HIDE_NAME,))
    with pytest.raises(ResolverError) as e:
        r.search("Customer", {"name": "Ada"})
    assert "withheld by governance policy 'hide-name'" in str(e.value)
    # An unmasked property is untouched.
    r.search("Customer", {"tier": "gold"})


def test_masking_nothing_changes_nothing(ontology):
    """Every construction of a resolver that predates M5 still means what it meant."""
    plain = _resolver(ontology)
    plain.get("Customer", "c1")
    assert {c.column for c in plain.engine.plans[-1].columns} == {"id", "full_name", "tier", "lifetime_value"}
    assert plain.masked("Customer") == ()


# ---- the same answer, whichever caller asks --------------------------------------


def _served(ontology, policies, writes=False):
    """A whole deployment: config, catalogs, resolver, tool set, out of one `build_server`.

    The fake catalog cannot be handed to DuckDB, so nothing here executes a read — that version of
    the claim is in `test_e2e_iceberg`, against a real warehouse with nothing stubbed. What this
    builds is everything up to the query."""
    config = LoomConfig(
        mcp=McpConfig(name="loom", writes=writes, actor="ci" if writes else None),
        policies=policies,
    )
    catalogs = {"rest_main": FakeRowCatalog(rows=CUSTOMERS)}
    server, resolver = build_server(ontology, config, catalogs)
    return server, resolver


def _tools(ontology, rows=(), policies=()):
    resolver = _resolver(ontology, rows, policies)
    return resolver, {t.name: t for t in build_tools(resolver)}


def test_a_direct_call_and_an_agent_call_are_filtered_identically(ontology):
    """M5's whole claim, through a real dispatch.

    The tool is not asserted to be *equal to* the resolver's answer by coincidence — it *is* the
    resolver's answer, which is the design: enforcement is one rung below the surface, so there is
    nothing for the surface to agree or disagree with. What this asserts is that nothing above the
    resolver can put a masked property back, on either path. (Over a real engine and a real
    warehouse: `test_e2e_iceberg.test_a_policy_withholds_from_both_callers_alike`.)"""
    resolver, tools = _tools(ontology, rows=[{"customerId": "c1", "name": "Ada", "tier": "gold"}],
                             policies=(HIDE_LTV,))
    direct = resolver.get("Customer", "c1")
    served = tools["get_customer"].handler({"key": "c1"})
    assert "ltv" not in direct
    assert served["object"] == direct


def test_the_surface_says_what_it_withholds(ontology):
    """A mask announces itself: the property names are already in the spec and in the schema, so
    naming them as withheld tells a caller nothing the surface did not. A row predicate will not do
    this, and the asymmetry is the rule rather than an inconsistency."""
    _, tools = _tools(ontology, policies=(HIDE_LTV,))
    assert "Withheld by governance policy: ltv." in tools["get_customer"].description
    assert tools["list_customer"].handler({})["masked"] == ["ltv"]
    assert tools["get_customer"].handler({"key": "c1"})["masked"] == ["ltv"]

    # Always present, so "this deployment governs nothing" and "this Loom is too old to say" are
    # distinguishable.
    _, plain = _tools(ontology)
    assert plain["list_customer"].handler({})["masked"] == []


def test_a_traverse_reports_the_mask_of_where_it_landed(ontology):
    """One tool spans every route, so this one is read per call rather than bound per build."""
    _, tools = _tools(ontology, policies=(HIDE_LTV,))
    assert tools["traverse"].handler({"objectType": "Order", "key": "o1", "link": "placedBy"})["masked"] == ["ltv"]
    assert tools["traverse"].handler({"objectType": "Customer", "key": "c1", "link": "orders"})["masked"] == []


def test_a_masked_property_leaves_the_filter_schema(ontology):
    """The resolver refuses the filter whatever the schema says — that is the enforcement, and it is
    below MCP where `loom query` meets it too. Removing it from the schema is the second rule:
    `cmd_serve` already refuses to advertise a tool that fails on every call, and an argument that
    fails on every call is the same thing one size down."""
    _, tools = _tools(ontology, policies=(HIDE_NAME,))
    search = tools["search_customer"]
    assert set(search.input_schema["properties"]["filter"]["properties"]) == {"tier"}
    assert "Search Customer by tier." in search.description


def test_a_description_never_misreports_the_spec(ontology):
    """"No properties are declared searchable" would be a lie where a policy is the reason: they are
    declared, and this deployment withholds them. A description that misreports the spec is worse
    than one that reports a policy.

    The fixture cannot reach that state on its own — `Customer.tier` is searchable and unmaskable
    (`upgradeTier` writes it), and `Order.orderId` is a primary key — so the object type is varied
    rather than the policy: same bound mask, a spec whose only searchable property is the one being
    withheld."""
    from loom.mcp.registry import _search_tool

    resolver = _resolver(ontology, policies=(HIDE_NAME,))
    only_name = replace(ontology.object_types["Customer"], searchable=("name",))
    assert "every searchable property is withheld here" in _search_tool(resolver, only_name).description
    assert "no properties are declared searchable" in _search_tool(
        resolver, replace(ontology.object_types["Customer"], searchable=())
    ).description


# ---- the write path --------------------------------------------------------------


def test_an_action_result_withholds_what_a_read_withholds(ontology):
    """The hole a read-only mask would have left: `before` and `after` are declared properties, and
    `dryRun` returns them without changing anything, so a masked `ltv` would have been one preview
    away from any caller with an action on that type."""
    runtime = build_runtime(
        ontology,
        LoomConfig(policies=(HIDE_LTV,)),
        {"rest_main": FakeRowCatalog(rows=CUSTOMERS)},
    )
    result = runtime.run("upgradeTier", {"customer": "c1", "newTier": "silver"}, dry_run=True)
    assert result.status == PREVIEWED
    assert "ltv" not in result.before and "ltv" not in result.after
    assert "tier" in result.before  # only what the policy names is withheld


def test_the_masked_column_is_carried_across_the_write_not_dropped(ontology):
    """spec-v0's open edge, answered: a masked column is *carried*, or the write destroys exactly
    the data the policy was protecting. Withheld from the account of the write, not from the write.

    The physical row keeps `lifetime_value` after a modify that never reported it."""
    catalog = FakeRowCatalog(rows=CUSTOMERS)
    runtime = build_runtime(ontology, LoomConfig(policies=(HIDE_LTV,)), {"rest_main": catalog})
    result = runtime.run("upgradeTier", {"customer": "c1", "newTier": "silver"})
    assert result.ok
    row = catalog.row("crm.customers", "id", "c1")
    assert row["tier"] == "silver"
    assert row["lifetime_value"] == 48210.5


def test_the_edit_log_records_no_more_than_the_surface_shows(ontology):
    """`before`/`after` reach `_loom_meta.edits` through the same projection, so the log inherits the
    mask rather than being excepted from it — an append-only table that outlives the row is the last
    place to keep the copy a policy just refused to show.

    What that costs nothing: the log's own guarantee is unchanged word for word. *What the record
    does not name, the run did not change* is still true of a masked property, because a policy that
    an action could write is refused before the deployment starts."""
    catalog = FakeRowCatalog(rows=CUSTOMERS)
    runtime = build_runtime(ontology, LoomConfig(policies=(HIDE_LTV,)), {"rest_main": catalog})
    runtime.run("upgradeTier", {"customer": "c1", "newTier": "silver"}, actor="ci")
    (record,) = EditLog(catalog=catalog).history()
    assert "ltv" not in record["before"] and "lifetime_value" not in record["before"]
    assert '"tier":"silver"' in record["after"].replace(" ", "")


def test_a_dev_command_and_a_tool_are_governed_by_one_function(ontology):
    """`loom run` is the direct caller M5's claim is about on the write side, so it cannot build its
    own ungoverned runtime. Both it and `build_server` go through `build_runtime`, which binds."""
    config = LoomConfig(mcp=McpConfig(writes=True, actor="ci"), policies=(HIDE_LTV,))
    catalogs = {"rest_main": FakeRowCatalog(rows=CUSTOMERS)}
    direct = build_runtime(ontology, config, catalogs)
    server, _ = build_server(ontology, config, catalogs)
    text, _ = server.call("run_upgrade_tier", {"parameters": {"customer": "c1", "newTier": "silver"}, "dryRun": True})
    assert direct.policies.masked("Customer") == ("ltv",)
    assert "ltv" not in text and "lifetime_value" not in text


def test_a_policy_that_does_not_fit_the_spec_stops_every_caller(ontology):
    """One refusal, three commands. `build_resolver` and `build_runtime` are the two places a spec
    and a deployment are paired, so there is no entry point that reads or writes past a policy it
    could not bind."""
    config = LoomConfig(
        mcp=McpConfig(writes=True), policies=(Policy(name="p", object_type="Customer", mask=("tier",)),)
    )
    catalogs = {"rest_main": FakeRowCatalog(rows=CUSTOMERS)}
    with pytest.raises(PolicyError):
        build_server(ontology, config, catalogs)
    with pytest.raises(PolicyError):
        build_runtime(ontology, config, catalogs)


def test_a_run_tool_announces_what_its_result_will_withhold(ontology):
    """`before`/`after` come back masked, so the description says so — the same reason a deprecation
    is labelled rather than hidden: an agent reads descriptions afresh every session."""
    server, _ = _served(ontology, (HIDE_LTV,), writes=True)
    assert "Withheld by governance policy: ltv." in server.tools["run_upgrade_tier"].description
    assert "Withheld" not in server.tools["run_create_order"].description  # Order is ungoverned
