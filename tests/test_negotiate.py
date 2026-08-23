"""Capability negotiation — what an ontology demands, and what happens when an engine can't.

Two halves. `requirements()` is pure, so most of this needs no engine, no catalog and no storage:
what a spec demands is a fact about the spec. The rest asserts the refusal — that it is a refusal
rather than a narrowing, that it names what to go and look at, and that it happens where a spec and
an engine are wired together rather than only where they are served.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from loom import build
from loom.config import LoomConfig
from loom.negotiate import (
    NEGOTIATED,
    NOT_NEGOTIATED,
    CapabilityError,
    Requirement,
    capability_fields,
    check_capabilities,
    requirements,
    unmet,
)
from loom.query.engine import Capabilities

VALID = Path(__file__).parent / "fixtures" / "valid"


@pytest.fixture
def ontology():
    ont, _ = build(VALID)
    return ont


@pytest.fixture
def semantic_ontology(ontology):
    """The fixture, with `Customer.name` declared semantic.

    Built here rather than in `fixtures/valid` on purpose. That directory is read by sixteen test
    modules, and a `semantic:` there would change what those assert for reasons unrelated to what
    they are about — the governance suite masks `Customer.name`, which this milestone made a
    refusal. What every negotiated flag needs is an ontology that demands it, not the *same*
    ontology, so the coverage assertions get one built for them and the shared fixture keeps
    demanding exactly the three it demanded before."""
    customer = dataclasses.replace(ontology.object_types["Customer"], semantic="name")
    return dataclasses.replace(
        ontology, object_types={**ontology.object_types, "Customer": customer}
    )


def _by_capability(reqs) -> dict[str, Requirement]:
    return {r.capability: r for r in reqs}


def _fully_capable(name: str) -> Capabilities:
    """Every negotiated flag on. Not `Capabilities(name)` — see
    `test_a_default_capabilities_is_not_a_fully_capable_one` for why the default is not this."""
    return Capabilities(name=name, **{flag: True for flag in NEGOTIATED})


# ---- what a spec demands ---------------------------------------------------------


def test_the_fixture_demands_three_of_the_four_negotiated_capabilities(ontology):
    """Customer/Order + placedBy: a link, a searchable string, and the page arguments.

    Three rather than four because nothing here declares `semantic:` — which is the point of the
    fourth: a link or a searchable string is ordinary and demands its capability by existing, and
    an embedding is asked for or it is not."""
    assert [r.capability for r in requirements(ontology)] == ["joins", "offset", "case_insensitive_like"]


def test_a_semantic_property_is_what_demands_vector_search(semantic_ontology):
    req = _by_capability(requirements(semantic_ontology))["vector_search"]
    assert req.demanded_by == ("Customer.name",)
    assert "array" in req.because


def test_nothing_but_a_semantic_declaration_demands_vector_search(ontology):
    """A searchable string demands `case_insensitive_like` and not this one.

    The two are easy to conflate — both are about text — and they are demanded by different
    declarations: `searchable` asks for substring matching, `semantic` asks for distance between
    vectors, and a spec may ask for either without the other."""
    assert "vector_search" not in _by_capability(requirements(ontology))


def test_a_link_is_what_demands_joins_and_the_requirement_names_it(ontology):
    req = _by_capability(requirements(ontology))["joins"]
    assert req.demanded_by == ("linkType 'placedBy' (Order -> Customer)",)
    assert "join" in req.because


def test_a_through_link_names_its_join_table_and_says_it_is_two_joins(ontology):
    """The reason adapts to the ontology in front of you.

    A many-to-many hop goes through a third table, so it is two joins rather than one — worth
    saying, and worth *not* saying for a spec that has no such link, since a reason describing a
    shape the spec doesn't have is one more thing to rule out while reading a refusal."""
    from loom.model import ThroughTable

    linked = dataclasses.replace(
        ontology,
        link_types={
            name: dataclasses.replace(
                link,
                through=ThroughTable(
                    catalog="rest_main", table="sales.order_customers", from_column="o", to_column="c"
                ),
            )
            for name, link in ontology.link_types.items()
        },
    )
    req = _by_capability(requirements(linked))["joins"]
    assert req.demanded_by == ("linkType 'placedBy' (Order -> Customer, through rest_main.sales.order_customers)",)
    assert "twice" in req.because
    # And the plain case says nothing about join tables.
    assert "twice" not in _by_capability(requirements(ontology))["joins"].because


def test_an_ontology_with_no_links_demands_no_joins(ontology):
    linkless = dataclasses.replace(ontology, link_types={})
    assert "joins" not in _by_capability(requirements(linkless))
    # And an engine that cannot join serves it.
    check_capabilities(linkless, Capabilities(name="joinless", joins=False))


def test_a_searchable_string_demands_like_and_a_searchable_enum_does_not(ontology):
    """`Customer.searchable` is `[name, tier]` — a string and an enum.

    The resolver emits a `Contains` for the string and an `Eq` for the enum (a closed set, where
    substring matching would only add ambiguity), so only one of the two shows up here."""
    req = _by_capability(requirements(ontology))["case_insensitive_like"]
    assert req.demanded_by == ("Customer.name", "Order.orderId")
    assert "Customer.tier" not in req.demanded_by


def test_a_searchable_non_string_demands_nothing_new(ontology):
    """Typed filters let `searchable` name any type, and range comparisons are a floor rather than
    a capability — so declaring a date searchable adds no requirement, and there is no flag for it
    to add. `case_insensitive_like` is still demanded by exactly the properties `contains` reaches."""
    from loom.filters import CONTAINS, operators

    dated = dataclasses.replace(
        ontology,
        object_types={
            name: dataclasses.replace(obj, searchable=(*obj.searchable, "placedAt"))
            if name == "Order"
            else obj
            for name, obj in ontology.object_types.items()
        },
    )
    before = _by_capability(requirements(ontology))
    after = _by_capability(requirements(dated))
    assert set(before) == set(after)
    assert after["case_insensitive_like"].demanded_by == before["case_insensitive_like"].demanded_by
    placed_at = dated.object_types["Order"].properties["placedAt"]
    assert CONTAINS not in operators(placed_at, searchable=True)


def test_an_ontology_with_nothing_searchable_demands_no_like(ontology):
    plain = dataclasses.replace(
        ontology,
        object_types={
            name: dataclasses.replace(obj, searchable=()) for name, obj in ontology.object_types.items()
        },
    )
    assert "case_insensitive_like" not in _by_capability(requirements(plain))


def test_offset_is_demanded_by_the_surface_rather_than_by_anything_in_the_spec(ontology):
    """The one requirement that is not a spec feature.

    M4's roadmap box read "validate spec features vs. capabilities", and this is the case that
    corrects it: strip every link and every searchable property and the page arguments are still on
    every read tool, because they are Loom's own vocabulary. An engine without OFFSET serves page 1
    of any ontology and fails page 2 of all of them."""
    stripped = dataclasses.replace(
        ontology,
        link_types={},
        object_types={
            name: dataclasses.replace(obj, searchable=()) for name, obj in ontology.object_types.items()
        },
    )
    assert [r.capability for r in requirements(stripped)] == ["offset"]


# ---- the refusal -----------------------------------------------------------------


def test_a_capable_engine_is_simply_wired(ontology):
    check_capabilities(ontology, Capabilities(name="capable"))
    assert unmet(ontology, Capabilities(name="capable")) == ()


@pytest.mark.parametrize("flag", sorted(NEGOTIATED))
def test_every_negotiated_flag_can_refuse_on_its_own(semantic_ontology, flag):
    caps = dataclasses.replace(_fully_capable("partial"), **{flag: False})
    with pytest.raises(CapabilityError) as excinfo:
        check_capabilities(semantic_ontology, caps)
    assert flag in str(excinfo.value)
    assert "partial" in str(excinfo.value)


def test_the_refusal_lists_every_unmet_requirement_at_once(semantic_ontology):
    """Not the first one. An operator swapping an adapter should learn what it has to support in
    one reading — the same reason `Diagnostics` collects spec errors instead of raising on the
    first."""
    caps = Capabilities(name="minimal", joins=False, offset=False, case_insensitive_like=False)
    with pytest.raises(CapabilityError) as excinfo:
        check_capabilities(semantic_ontology, caps)
    message = str(excinfo.value)
    for flag in NEGOTIATED:
        assert flag in message


def test_a_default_capabilities_is_not_a_fully_capable_one(semantic_ontology):
    """`Capabilities(name=...)` is three yeses and one no, and that asymmetry is deliberate.

    `joins` / `offset` / `case_insensitive_like` default true because they are floors almost every
    dialect meets; `vector_search` defaults false because array arithmetic is not implied by being
    able to filter. Asserted here so the day a fourth adapter appears and says nothing, what it is
    described as is a decision somebody reads rather than a default nobody noticed."""
    with pytest.raises(CapabilityError):
        check_capabilities(semantic_ontology, Capabilities(name="silent"))


def test_the_refusal_names_the_declaration_to_go_and_change(ontology):
    """"this engine does not support joins" is not actionable on its own; the linkType that made it
    a requirement is."""
    with pytest.raises(CapabilityError) as excinfo:
        check_capabilities(ontology, Capabilities(name="joinless", joins=False))
    message = str(excinfo.value)
    assert "placedBy" in message
    assert "engine:" in message  # the other way out, named


def test_native_merge_can_never_refuse_an_ontology(ontology):
    """The routing hint, asserted as a hint.

    Every shipped adapter reports `native_merge: false` — writes go through the catalog's
    `RowWriter` — and that is not a reason to refuse anything. If this ever fails, a capability has
    been negotiated that no spec can demand."""
    check_capabilities(ontology, Capabilities(name="no-merge", native_merge=False))


def test_every_capability_is_either_negotiated_or_deliberately_not():
    """The exhaustiveness assertion.

    Adding a flag to `Capabilities` fails here until somebody decides which kind of fact it is: a
    requirement a spec can demand, or a hint no spec can. Without this, the quiet answer is "third
    kind: unread", which is how `loom.managed` happened."""
    assert NEGOTIATED | NOT_NEGOTIATED == capability_fields()
    assert not (NEGOTIATED & NOT_NEGOTIATED)


def test_no_requirement_can_name_a_capability_outside_the_negotiated_set(ontology):
    """Structural, not just true of the shipped specs: a `Requirement` refuses to exist naming a
    flag outside the set. The coverage test above reads the two sets and cannot see this — a
    requirement on `native_merge` would keep them covering the dataclass exactly while refusing
    ontologies for a capability nothing can demand."""
    assert {r.capability for r in requirements(ontology)} <= NEGOTIATED
    with pytest.raises(ValueError, match="not a negotiated capability"):
        Requirement(capability="native_merge", demanded_by=("nothing",), because="nothing")


def test_the_shipped_duckdb_adapter_serves_the_shipped_example():
    """The one test here that touches a real adapter: negotiation has to be satisfiable, not just
    strict.

    No extra is needed and none is skipped for. `capabilities()` is a pure method and the adapter
    module imports the `duckdb` package lazily, inside `_connection()` — which is the same property
    that lets `compile()` be asserted without storage."""
    from loom.query.engines.duckdb import DuckDBEngine

    example, _ = build(Path(__file__).resolve().parents[1] / "examples" / "retail" / "ontology")
    check_capabilities(example, DuckDBEngine(catalogs={}).capabilities())


# ---- where it happens ------------------------------------------------------------


def test_build_resolver_is_where_the_pairing_is_checked(ontology, monkeypatch):
    """Not `cmd_serve`. `build_resolver` is the one function that pairs a spec with an engine, so
    `loom query` refuses exactly what `loom serve` refuses — a dev command that could read out of
    an engine the served surface will not stand on is the back door `loom query` exists not to be."""
    from loom.resolver import build_resolver

    class Joinless:
        def capabilities(self):
            return Capabilities(name="joinless", joins=False)

        def compile(self, plan):  # pragma: no cover - never reached
            raise AssertionError("negotiation should have refused before anything compiled")

        def execute(self, compiled):  # pragma: no cover - never reached
            raise AssertionError("negotiation should have refused before anything executed")

    monkeypatch.setattr("loom.query.engines.open_engine", lambda cfg, cats: Joinless())

    with pytest.raises(CapabilityError, match="placedBy"):
        build_resolver(ontology, LoomConfig(), catalogs={})


def test_a_resolver_can_still_be_constructed_from_any_engine(ontology):
    """The check is on the wiring, not an invariant of the pair. That is what lets a test drive the
    resolver with a fake, and an adapter be exercised before anybody has decided what it serves."""
    from loom.resolver import Resolver

    class Joinless:
        def capabilities(self):
            return Capabilities(name="joinless", joins=False)

        def compile(self, plan):
            return None

        def execute(self, compiled):
            return []

    assert Resolver(ontology=ontology, engine=Joinless()).engine.capabilities().joins is False
