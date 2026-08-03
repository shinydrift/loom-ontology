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

import dataclasses
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from loom import build, governance
from loom.action import (
    APPLIED,
    OBJECT_EXISTS,
    OBJECT_NOT_FOUND,
    PREVIEWED,
    REFUSED,
    ActionError,
    EditLog,
    bind_writes,
    build_runtime,
)
from loom.auth import ClaimType, Principal
from loom.config import LoomConfig, McpAuth, McpConfig
from loom.errors import Diagnostics, SourceLoc
from loom.expr import parse as parse_expr
from loom.governance import (
    ENFORCED_KEYS,
    MOVED_KEYS,
    POLICY_KEYS,
    Policy,
    PolicyError,
    PolicySet,
    bind_policies,
    parse_policies,
)
from loom.mcp import registry
from loom.mcp.registry import build_tools
from loom.mcp.server import build_server
from loom.predicate import admits
from loom.query.engine import Capabilities, CompiledQuery
from loom.query.ir import tables_of
from loom.resolver import Resolver, ResolverError, build_resolver

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
    return bind_policies(ontology, policies).select(None)


def _resolver(ontology, rows=(), policies=()):
    return Resolver(
        ontology=ontology,
        engine=RecordingEngine(rows),
        policies=bind_policies(ontology, policies).select(None),
    )


def _parse(entries):
    diag = Diagnostics()
    return parse_policies(entries, SourceLoc("loom.yaml"), diag), diag


HIDE_LTV = Policy(name="hide-ltv", object_type="Customer", mask=("ltv",))
HIDE_NAME = Policy(name="hide-name", object_type="Customer", mask=("name",))


# ---- the grammar ----------------------------------------------------------------


def test_every_policy_key_a_config_may_carry_is_read_into_a_policy():
    """`negotiate.NEGOTIATED`'s device, and this is what it turned into when the last reservation
    was enforced.

    The partition it replaces was `ENFORCED_KEYS | RESERVED_KEYS == POLICY_KEYS`, which caught the
    third kind Loom has been bitten by — accepted, unenforced, and silent, which is how
    `loom.managed` was written by `apply` and read by nothing for two milestones. With `when:`
    enforced, `RESERVED_KEYS` is empty and that check has one live side, so it is replaced by the
    stronger statement it was standing in for: **every key this grammar accepts is read into a
    field of `Policy`**. A key added to `POLICY_KEYS` and never parsed fails here; a key nobody
    declared is already refused by `check_keys`."""
    fields = {f.name for f in dataclasses.fields(Policy)}
    # `objectType` is the YAML spelling of `object_type` — Loom's own vocabulary rather than the
    # obvious `on:`, which YAML 1.1 resolves to the boolean True.
    assert {"objectType" if k == "object_type" else k for k in fields} == set(POLICY_KEYS)
    assert POLICY_KEYS == ENFORCED_KEYS


def test_a_key_that_will_never_land_left_the_grammar_rather_than_sitting_in_it():
    """The fate of a key that the partition test could not see, and it still cannot.

    A key sitting reserved that no slice will ever turn on partitions the grammar perfectly and
    still lies to whoever reads `governance:` to see what it will one day hold. `audit` was that
    key — neither half of what it named is a policy — so it is gone from the grammar rather than
    parked in it. There is nothing parked in it any more: `when:` was the last reservation and it
    is enforced, so the `Reserved` machinery went with it rather than staying as a dataclass
    nothing constructs, which is the `loom.managed` shape one level up."""
    assert "audit" not in POLICY_KEYS
    assert set(MOVED_KEYS).isdisjoint(POLICY_KEYS)
    assert not hasattr(governance, "RESERVED_KEYS") and not hasattr(governance, "Reserved")


def test_a_moved_key_says_where_it_went_rather_than_that_it_is_unknown():
    """A reservation that ends in a departure still owes an answer to whoever wrote the config.

    Nobody was running one — a config that was refused is a config nobody deployed — but `audit:`
    was *advertised* as reserved for two slices, in §6.1 and in the refusal it produced. So it is
    reported once, by the branch that knows the destination, rather than falling through to
    `unexpected key 'audit'` with nothing behind it."""
    _, diag = _parse([{"name": "p", "objectType": "Customer", "mask": ["ltv"], "audit": {}}])
    (problem,) = [e for e in diag.errors if "'audit'" in e.message]
    assert "no longer a policy key" in problem.message
    # Both halves, because the key meant two things and they ended differently: one moved, one is
    # not coming back.
    assert "governance.edit_log: required" in (problem.hint or "")
    assert "no Loom command deletes" in (problem.hint or "")


def test_a_conditional_mask_is_refused_because_a_mask_announces_itself():
    """**The refusal M6's second slice is built around.**

    A mask is in the tool description, in the `filter` schema and in `masked` on every result, and
    §7 says the tool set and its argument namespaces are a function of the spec. Conditioning one on
    the caller has three spellings and all three are refused elsewhere in this codebase: a tool set
    assembled per caller (the surface becomes a function of the caller), the worst case announced to
    everyone (narrowing the surface to fit, which §6 will not do even for an engine), or an
    announcement that stops announcing. Rows carry no announcement at all, which is exactly why they
    may be conditioned at no cost to the surface — and why "HR sees ssn, nobody else does" keeps M5's
    answer: two deployments."""
    _, diag = _parse(
        [{"name": "p", "objectType": "Customer", "mask": ["ltv"], "when": "principal.dept == 'hr'"}]
    )
    (problem,) = [e for e in diag.errors if "'when:'" in e.message]
    assert "cannot vary per caller" in problem.message
    assert "function of the caller" in (problem.hint or "")
    assert "two deployments" in (problem.hint or "")


def test_a_guard_with_nothing_to_guard_is_refused():
    """A guard says *which callers* a policy applies to, never *what* it withholds. Without a
    `rows:` predicate there is no policy for it to guard, and a config that reads like a filter and
    filters nothing is the shape this module exists against."""
    _, diag = _parse([{"name": "p", "objectType": "Customer", "when": "principal.dept == 'hr'"}])
    assert any("withholds nothing" in e.message for e in diag.errors)


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

    # And the same statement read backwards: nothing is withheld except by a policy that says so,
    # or the banner reports "0 policies withhold ltv" — true, unattributable, unactionable.
    with pytest.raises(ValueError) as e:
        PolicySet(masks={"Customer": {"ltv": "ghost"}})
    assert "a policy this set does not declare" in str(e.value)

    # Both invariants again for rows, because a declared filter that is not in the resolution is
    # the same failure as a declared mask that is not.
    live = Policy(name="live-only", object_type="Customer", rows=parse_expr("object.ltv != null"))
    with pytest.raises(ValueError) as e:
        PolicySet(policies=(live,))
    assert "declares a row filter that is not in the resolution" in str(e.value)

    with pytest.raises(ValueError) as e:
        PolicySet(filters={"Customer": (("ghost", live.rows),)})
    assert "filtered by a policy this set does not declare" in str(e.value)


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


# ---- rows: withheld by not being in the table this deployment reads --------------

LIVE_ONLY = Policy(name="live-only", object_type="Customer", rows=parse_expr("object.ltv != null"))
GOLD_ONLY = Policy(name="gold-only", object_type="Customer", rows=parse_expr("object.tier == 'gold'"))


def test_a_policy_may_withhold_rows_columns_or_both():
    """`mask` alone stopped being the only way to withhold something, so "withholds nothing" moved
    up a level rather than being dropped: a policy with neither is still the shape this module
    exists to prevent, because it reads exactly like one that is protecting something."""
    policies, diag = _parse(
        [
            {"name": "cols", "objectType": "Customer", "mask": ["ltv"]},
            {"name": "rows", "objectType": "Customer", "rows": "object.tier == 'gold'"},
            {"name": "both", "objectType": "Customer", "mask": ["ltv"], "rows": "object.tier == 'gold'"},
        ]
    )
    assert diag.errors == []
    assert [(p.name, bool(p.mask), p.rows is not None) for p in policies] == [
        ("cols", True, False),
        ("rows", False, True),
        ("both", True, True),
    ]

    _, diag = _parse([{"name": "empty", "objectType": "Customer"}])
    assert any("withholds nothing" in e.message for e in diag.errors)
    assert any("a 'rows' predicate" in (e.hint or "") for e in diag.errors)


def test_a_rows_predicate_is_the_one_expression_language():
    """Not a second grammar with its own parser: `rows:` goes through `expr.parse`, so the braces an
    effect value may carry are stripped here too and nothing downstream sees one. What is narrower
    is what it may *contain* — `predicate.py` — and that is checked against the ontology at bind."""
    (policy,), diag = _parse([{"name": "p", "objectType": "Customer", "rows": "{{ object.ltv != null }}"}])
    assert diag.errors == []
    assert policy.rows.raw == "{{ object.ltv != null }}"
    assert policy.rows.refs()[0].path == ("object", "ltv")

    _, diag = _parse([{"name": "p", "objectType": "Customer", "rows": "object.ltv !="}])
    assert any("not a valid expression" in e.message for e in diag.errors)

    _, diag = _parse([{"name": "p", "objectType": "Customer", "rows": ["object.ltv"]}])
    assert any("'rows' must be an expression" in e.message for e in diag.errors)


def test_row_predicates_compose_as_a_conjunction(ontology):
    """Policies subtract and never add, so two policies filtering one type can only mean *both*.

    An `||` would let a second policy widen what the first permitted, which is the one thing no
    policy may do — and the conjunction is what makes composition order-free, exactly as masks
    union. Asserted over rows rather than over the expression: adding a policy never adds one."""
    row = {"ltv": 1.0, "tier": "silver"}  # passes live-only, fails gold-only
    one = _bound(ontology, LIVE_ONLY).predicate_for("Customer")
    two = _bound(ontology, LIVE_ONLY, GOLD_ONLY).predicate_for("Customer")
    reversed_ = _bound(ontology, GOLD_ONLY, LIVE_ONLY).predicate_for("Customer")

    assert admits(one, row) and not admits(two, row)
    for candidate in ({"ltv": 1.0, "tier": "gold"}, {"ltv": None, "tier": "gold"}, row):
        assert admits(two, candidate) == admits(reversed_, candidate)
        assert not (admits(two, candidate) and not admits(one, candidate))
    assert _bound(ontology, LIVE_ONLY).filtered_by("Customer") == ("live-only",)
    assert _bound(ontology).predicate_for("Customer") is None


def test_a_predicate_is_attached_to_every_table_a_plan_reads(ontology):
    """The structural half of "both ends of a traverse". The predicate rides on `ir.TableRef`, and
    `Resolver._table` is the one place an object type becomes one, so a governed type cannot enter
    a plan without it — `get`, `search`, `list` and both ends of a traverse, by construction."""
    r = _resolver(ontology, policies=(LIVE_ONLY,))
    for call in (
        lambda: r.get("Customer", "c1"),
        lambda: r.list("Customer"),
        lambda: r.search("Customer", {"tier": "gold"}),
        lambda: r.traverse("Order", "o1", "placedBy"),
    ):
        call()
        governed = [t for t in tables_of(r.engine.plans[-1].source) if t.predicate is not None]
        assert [t.table for t in governed] == ["crm.customers"]


def test_a_link_is_not_a_way_around_a_row_predicate(ontology):
    """*You cannot search a customer but you can traverse to one* is the hole a predicate applied
    only to the landing type would leave. Here the landing type is `Order`, which nothing filters,
    and the anchor is a `Customer`, which is filtered — so the anchor end carries the predicate and
    a caller cannot read the orders of a customer this deployment does not show them."""
    r = _resolver(ontology, policies=(LIVE_ONLY,))
    r.traverse("Customer", "c1", "orders")
    source = r.engine.plans[-1].source
    assert source.to_table.table == "sales.orders" and source.to_table.predicate is None
    assert source.from_table.table == "crm.customers" and source.from_table.predicate is not None


def test_a_row_predicate_adds_no_sentence_to_any_tool_description(ontology):
    """The asymmetry, at the surface. A mask announces itself because the property names are already
    in the spec and in the JSON Schema, so saying "withheld" tells a caller nothing new. The rows
    *are* the data, so "you may not see this one" is an existence oracle over it — and a filtered
    row is simply absent, with `found: false` saying exactly what it says for a row that never
    existed."""
    _, tools = _tools(ontology, rows=[], policies=(LIVE_ONLY,))
    for tool in tools.values():
        assert "governance" not in tool.description
        assert "live-only" not in tool.description
        assert "row" not in tool.description.lower()
    assert tools["get_customer"].handler({"key": "c2"}) == {
        "objectType": "Customer",
        "key": "c2",
        "found": False,
        "object": None,
        "masked": [],
    }


def test_the_banner_names_a_filtered_type_and_nothing_a_caller_reads_does(ontology):
    """The one place the two halves of a policy are treated differently on purpose: an operator
    starting the process holds the `loom.yaml` this describes, so naming a filtered type tells them
    nothing they cannot already read. To anybody else it is a statement about data they were not
    shown."""
    from loom.cli import _governance_mode

    resolver = _resolver(ontology, policies=(LIVE_ONLY, HIDE_LTV))
    (line, _) = _governance_mode(resolver)
    assert "withhold Customer: ltv" in line
    assert "filter the rows of Customer (by live-only)" in line
    assert _governance_mode(_resolver(ontology)) == []


# ---- rows: the write plane -------------------------------------------------------


def _runtime(ontology, *policies, rows=None):
    catalog = FakeRowCatalog(rows=rows or CUSTOMERS)
    return catalog, build_runtime(ontology, LoomConfig(policies=policies), {"rest_main": catalog})


def test_an_action_on_a_row_the_policy_excludes_looks_exactly_like_a_missing_one(ontology):
    """An agent that cannot see a row must not be able to act on it — and must not be able to tell
    that it was refused rather than absent. `c2` has a null `ltv` and is withheld; the failure is
    the one a concurrent delete produces, in the same words, because a refusal a caller could tell
    apart would be the existence oracle the read path refuses to be."""
    catalog, runtime = _runtime(ontology, LIVE_ONLY)
    excluded = runtime.run("upgradeTier", {"customer": "c2", "newTier": "gold"})
    ungoverned = build_runtime(ontology, LoomConfig(), {"rest_main": catalog}).run(
        "upgradeTier", {"customer": "cX", "newTier": "gold"}
    )

    assert excluded.status == REFUSED
    (failure,) = excluded.failures
    assert failure.code == OBJECT_NOT_FOUND
    assert failure.message == ungoverned.failures[0].message.replace("cX", "c2")
    assert excluded.before is None
    assert catalog.writes == []
    # And the row the policy admits is untouched by any of this.
    assert runtime.run("upgradeTier", {"customer": "c1", "newTier": "silver"}).ok


def test_a_dry_run_cannot_preview_a_row_the_policy_excludes(ontology):
    """The hole a read-path-only predicate would leave, which is the same one a read-path-only mask
    would have left: `dryRun` reports `before` without changing anything."""
    _, runtime = _runtime(ontology, LIVE_ONLY)
    result = runtime.preview("upgradeTier", {"customer": "c2", "newTier": "gold"})
    assert result.status == REFUSED and result.before is None


def test_a_modify_may_move_a_row_out_of_the_predicate(ontology):
    """The gate is on `before`, never on the result — and this is the pair that forces it. A soft
    delete is exactly a modify that moves a row out of `rows: "object.deletedAt == null"`, so
    refusing it would break the most ordinary policy's most ordinary companion action."""
    catalog, runtime = _runtime(ontology, GOLD_ONLY)
    result = runtime.run("upgradeTier", {"customer": "c1", "newTier": "silver"})
    assert result.ok and result.after["tier"] == "silver"
    # The row is now invisible to the deployment that just wrote it, which is what a soft delete is.
    assert not runtime.run("upgradeTier", {"customer": "c1", "newTier": "gold"}).ok
    assert catalog.row("crm.customers", "id", "c1")["tier"] == "silver"


def test_a_create_still_reports_a_row_the_policy_excludes(ontology):
    """One deliberate existence oracle, named rather than discovered.

    The primary-key check before a `create` has to be **physical**: two creates on one key that both
    read past an excluded row would both append, manufacturing the duplicate key `_read` refuses
    forever after and Loom can never repair. So on this one path a row predicate hides rows and not
    keys — and what leaks is confined to *something exists under the key you supplied*: no property
    of it, and a key the caller chose."""
    hide_all_orders = Policy(name="no-orders", object_type="Order", rows=parse_expr("object.customerId != 'c9'"))
    catalog, runtime = _runtime(ontology, hide_all_orders)
    catalog.rows["sales.orders"] = [
        {"id": "o1", "customer_id": "c9", "total_amount": Decimal("10.00"), "created_at": None}
    ]

    result = runtime.run("createOrder", {"orderId": "o1", "customerId": "c1", "total": "5.00"})

    assert result.status == REFUSED
    (failure,) = result.failures
    assert failure.code == OBJECT_EXISTS
    assert catalog.writes == []
    assert len(catalog.rows["sales.orders"]) == 1


def test_a_run_refused_by_a_row_predicate_is_still_recorded(ontology):
    """It named a row, so it is an attempted edit rather than a malformed call — the gate `_record`
    already used. An audit trail that dropped these could not answer *who tried to act on a row this
    deployment does not show them*, which is the question a row predicate most invites."""
    catalog, runtime = _runtime(ontology, LIVE_ONLY)
    runtime.run("upgradeTier", {"customer": "c2", "newTier": "gold"}, actor="ci")
    (record,) = EditLog(catalog=catalog).history()
    assert record["status"] == REFUSED and record["object_key"] == "c2"
    assert OBJECT_NOT_FOUND in record["failures"]


def test_a_direct_call_and_an_agent_call_withhold_the_same_rows(ontology):
    """M5's claim for the row half, on the write plane, where the two callers are `loom run` and
    `run_<action>` — both built by `build_runtime`, which binds."""
    config = LoomConfig(mcp=McpConfig(writes=True, actor="ci"), policies=(LIVE_ONLY,))
    catalogs = {"rest_main": FakeRowCatalog(rows=CUSTOMERS)}
    direct = build_runtime(ontology, config, catalogs).run("upgradeTier", {"customer": "c2", "newTier": "gold"})
    server, _ = build_server(ontology, config, catalogs)
    text, is_error = server.call("run_upgrade_tier", {"parameters": {"customer": "c2", "newTier": "gold"}})

    assert direct.failures[0].code == OBJECT_NOT_FOUND
    # `isError` stays false: M4 fixed it to mean *did this become a run*, and a run refused by a
    # policy is a run. The refusal travels in the result, exactly as a refusal by a validation rule
    # does — which is also what stops the transport from telling the two apart.
    assert OBJECT_NOT_FOUND in text and not is_error


# ---- a policy that names the caller ----------------------------------------------
#
# M6's second slice. Everything above holds a deployment-scoped policy down; this section is the
# one thing that varies per caller, and the three questions it had to answer: what a `when:` may
# say, where the refusal for a surface that cannot attest lives, and what a *missing* claim means
# as opposed to a false one.


def _principal(subject: str = "alice", **claims) -> Principal:
    return Principal(
        subject=subject,
        issuer="https://issuer.test",
        client_id="client",
        claims={"sub": subject, "iss": "https://issuer.test", **claims},
    )


CLAIMS = {"dept": ClaimType("string"), "groups": ClaimType("string", array=True)}

GOLD_DESK = Policy(
    name="gold-desk",
    object_type="Customer",
    rows=parse_expr("object.tier == 'gold'"),
    when=parse_expr("principal.groups contains 'gold-desk'"),
)
OWN_ROWS = Policy(
    name="own-rows",
    object_type="Customer",
    rows=parse_expr("object.customerId == principal.sub"),
)


def test_a_program_with_no_conditional_policy_returns_the_same_set_for_every_caller(ontology):
    """The assertion that makes the seam cheap, and it is the one M6's decision 1 asked for.

    A deployment that predates this milestone is *provably* unchanged rather than argued to be
    equivalent: same object, so the resolver holding it is the same resolver, so there is nothing
    per-call about a program that names nobody."""
    program = bind_policies(ontology, [HIDE_LTV], CLAIMS)
    assert not program.conditional
    assert program.select(None) is program.select(_principal()) is program.select(None)


def test_the_masks_are_the_same_object_for_every_caller(ontology):
    """The claim that a conditional mask is refused, made where it would break if it stopped being
    true. `select` may change what rows a caller sees and may never change what the deployment
    *announces* — because the announcement is in the tool description, the filter schema and the
    `masked` field, all of which are built once."""
    program = bind_policies(ontology, [HIDE_LTV, GOLD_DESK], CLAIMS)
    assert program.conditional
    inside = program.select(_principal(groups=["gold-desk"]))
    outside = program.select(_principal(groups=["support"]))
    assert inside.masks is outside.masks is program.announcements().masks
    assert inside.masked("Customer") == outside.masked("Customer") == ("ltv",)


def test_a_surface_that_cannot_attest_refuses_a_policy_that_names_a_caller(ontology):
    """**Decision 2, and it lives in one function rather than in a check three call sites derive.**

    The tempting alternative — treat an unattested caller as principal-less and apply only the
    unconditional policies — is disqualified by this module's own invariant. Policies subtract and
    never add, so skipping the guarded ones gives that caller *less* subtraction, and `loom query`
    becomes the way to read what the served surface withholds.

    It names no surface, which is what makes it right for the case a surface check would get wrong:
    `loom query` against an attesting config still attests nobody, and `McpConfig.attests` is true
    there."""
    program = bind_policies(ontology, [GOLD_DESK], CLAIMS)
    with pytest.raises(PolicyError) as e:
        program.select(None)
    assert "gold-desk" in str(e.value) and "nobody is attested here" in str(e.value)
    # And the same for a predicate that names the caller without a guard: what makes a policy
    # conditional is that it cannot be decided without one, not which key it was written in.
    with pytest.raises(PolicyError):
        bind_policies(ontology, [OWN_ROWS], CLAIMS).select(None)


def test_loom_query_refuses_a_conditional_config_rather_than_reading_unfiltered(ontology):
    """The same refusal reached the way `loom query` reaches it — through `build_resolver`, which is
    `bind_reads(...).for_(None)`.

    `build_resolver`'s invariant survives with one sentence narrowed: it refuses exactly the
    *pairings* `loom serve` refuses, and what differs is not a check but an ability. Nothing here
    took a `surface=` argument."""
    config = LoomConfig(
        mcp=McpConfig(auth=McpAuth(issuer="i", audience="a", jwks_uri="https://k", claims=CLAIMS)),
        policies=(GOLD_DESK,),
    )
    with pytest.raises(PolicyError) as e:
        build_resolver(ontology, config, {"rest_main": FakeRowCatalog(rows=CUSTOMERS)})
    assert "cannot decide which policies apply" in str(e.value)


def test_a_guard_decides_whether_a_policy_applies_at_all(ontology):
    """`when:` composes as an **implication**: a policy whose guard is false withholds nothing.

    That is what stops it being sugar for a longer `rows:` — the same text moved inside the
    predicate would withhold *everything* when the guard is false, which is the opposite
    disposition, and the reason the two are different grammars rather than one."""
    program = bind_policies(ontology, [GOLD_DESK], CLAIMS)
    assert program.select(_principal(groups=["gold-desk"])).filtered_by("Customer") == ("gold-desk",)
    assert program.select(_principal(groups=["support"])).filtered_by("Customer") == ()


def test_a_missing_claim_applies_the_policy_rather_than_skipping_it(ontology):
    """**A missing claim is not a false one**, and the direction it fails in is the withholding one.

    The rule this shares with decision 2's refusal, read the other way: *decidable at pairing time
    with somebody to tell → refuse; decidable only per call, with only the caller to tell → withhold
    silently*. Telling this caller that a policy did or did not apply to them is the existence
    oracle §6.1 refuses, and there is nobody else in the exchange to tell.

    The dangerous alternative is treating absence as `null`: `principal.groups != null` would then
    be **false** for a caller carrying no groups, the guard would not apply, and a missing claim
    would have *widened* what that caller sees."""
    program = bind_policies(ontology, [GOLD_DESK], CLAIMS)
    assert program.select(_principal()).filtered_by("Customer") == ("gold-desk",)
    # A claim whose value contradicts its declared type is absent for the same reason: anything a
    # policy cannot decide about must be missing rather than wrong.
    assert program.select(_principal(groups="gold-desk")).filtered_by("Customer") == ("gold-desk",)


def test_the_caller_is_folded_into_the_predicate_and_stops_there(ontology):
    """Decision 1, paid for rather than asserted: what reaches the resolver is a predicate naming
    nobody, because a principal is constant for the duration of a call."""
    program = bind_policies(ontology, [OWN_ROWS], CLAIMS)
    ((name, expr),) = program.select(_principal("c2")).filters["Customer"]
    assert name == "own-rows"
    assert admits(expr, {"customerId": "c2"}) and not admits(expr, {"customerId": "c1"})
    # The raw text is what the author wrote, not a rendering with somebody's subject in it: a
    # message about this policy should name what is in the file.
    assert expr.raw == "object.customerId == principal.sub"


def test_two_callers_of_one_tool_set_are_filtered_differently(ontology, monkeypatch):
    """The whole slice, through a real dispatch, without a transport.

    One tool set, one process, one `Resolver` built at startup — and two callers whose reads differ,
    because what varies is a decided `PolicySet` selected above the resolver and never an identity
    threaded into it. The tool itself is identical for both: same name, same schema, same
    description, which is §7's claim and the reason a *mask* may not be conditioned.

    The transport is what `test_mcp_auth.py` adds — and it adds the rows, against a real engine.
    What is proved here is that the handler asks *per call* rather than closing over an answer from
    build time, and that the tool it asks through is the same tool for everybody."""
    program = bind_policies(ontology, [OWN_ROWS], CLAIMS)
    engine = RecordingEngine()
    announcing = Resolver(ontology=ontology, engine=engine, policies=program.announcements())
    tools = {t.name: t for t in build_tools(announcing, program=program)}

    predicates = {}
    for subject in ("c1", "c2"):
        monkeypatch.setattr(registry, "current_principal", lambda s=subject: _principal(s))
        tools["list_customer"].handler({})
        predicates[subject] = engine.plans[-1].source.table.predicate
    # Two callers, two different compiled filters, out of one tool whose schema and description
    # never moved: the surface is a function of the spec, and only the rows are a function of the
    # caller.
    assert predicates["c1"] != predicates["c2"]
    assert predicates["c1"].right.value == "c1" and predicates["c2"].right.value == "c2"
    assert "Withheld" not in tools["list_customer"].description


def test_a_read_with_policies_nobody_selected_is_refused(ontology):
    """The announcement set builds tool schemas and a banner and cannot read a row.

    Its predicates may still name a principal, so a read performed with it would be a read nobody
    selected — and the failure would be *silent*, one conditional policy short. Every read goes
    through `_table`, which is what makes the check total rather than a habit."""
    resolver = Resolver(
        ontology=ontology,
        engine=RecordingEngine(),
        policies=bind_policies(ontology, [GOLD_DESK], CLAIMS).announcements(),
    )
    with pytest.raises(ResolverError) as e:
        resolver.list("Customer")
    assert "never decided for a caller" in str(e.value)


def test_the_write_plane_is_governed_per_caller_too(ontology):
    """M5's claim — both planes withhold the same rows — under a policy that varies.

    A conditional policy enforced on the read plane alone would be one `dryRun` away from reading
    out of `before` exactly what it withheld from a read, so `_Run._admitted` gets the same decided
    set from the same `select`. `c1` may act on itself and `c2` may not, out of one runtime."""
    program = bind_policies(ontology, [OWN_ROWS], CLAIMS)
    catalog = FakeRowCatalog(rows=CUSTOMERS)
    writes = bind_writes(ontology, LoomConfig(policies=(OWN_ROWS,)), {"rest_main": catalog})

    mine = writes.for_(_principal("c1")).run("upgradeTier", {"customer": "c1", "newTier": "silver"})
    theirs = writes.for_(_principal("c2")).run("upgradeTier", {"customer": "c1", "newTier": "silver"})
    assert mine.status == APPLIED
    # The same words a concurrent delete produces: a caller who could tell "withheld" from "gone"
    # would have the existence oracle the read path refuses to be.
    assert theirs.failures[0].code == OBJECT_NOT_FOUND
    assert program.select(_principal("c2")).filtered_by("Customer") == ("own-rows",)


def test_a_run_with_policies_nobody_selected_is_refused(ontology):
    """`Resolver._table`'s assertion, on the plane that writes.

    The announcing runtime exists to describe a `run_` tool — its description announces the target's
    mask — and it must not be able to run one: its predicates may still name a principal, so a run
    performed with it would be a run nobody selected, refused by one conditional policy short."""
    writes = bind_writes(ontology, LoomConfig(policies=(OWN_ROWS,)), {"rest_main": FakeRowCatalog(rows=CUSTOMERS)})
    with pytest.raises(ActionError) as e:
        writes.announcing().run("upgradeTier", {"customer": "c1", "newTier": "gold"})
    assert "never decided for a caller" in str(e.value)


def test_a_stdio_server_refuses_a_config_whose_policies_name_a_caller(ontology):
    """A spawned server carries no bearer token, so it can never attest anybody — and finding that
    out per call would be a server that started and then failed every read.

    It is not "direct commands vs MCP": the predicate is *can this surface attest*, which is why
    `loom serve` over stdio lands on the same side as `loom query`."""
    config = LoomConfig(policies=(OWN_ROWS,))
    with pytest.raises(PolicyError) as e:
        build_server(ontology, config, {"rest_main": FakeRowCatalog(rows=CUSTOMERS)})
    assert "nobody is attested here" in str(e.value)


def test_the_banner_says_which_policies_vary_by_caller(ontology):
    """The line an operator reads at startup. Masks are deployment-wide by construction and say so;
    a policy whose rows vary per caller is named, because the rest of the sentence says it does not
    — and because `loom query` against that same file will refuse rather than read."""
    from loom.cli import _governance_mode

    program = bind_policies(ontology, [HIDE_LTV, GOLD_DESK], CLAIMS)
    resolver = Resolver(ontology=ontology, engine=RecordingEngine(), policies=program.announcements())
    banner = " ".join(_governance_mode(resolver))
    assert "withhold Customer: ltv" in banner and "filter the rows of Customer (by gold-desk)" in banner
    assert "masks are deployment-wide" in banner and "gold-desk filter rows per attested caller" in banner

    plain = Resolver(ontology=ontology, engine=RecordingEngine(), policies=_bound(ontology, HIDE_LTV))
    assert "deployment-wide — every caller" in " ".join(_governance_mode(plain))
