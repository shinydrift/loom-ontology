"""`semantic:` — the grammar, the refusals, and the two files it is split across.

M10's first slice ships no vector and no tool: what it ships is the ability to *say* a property is
worth searching by meaning, the deployment key that says where a vector would come from, and every
refusal the pairing of those two owes. So this module asserts a spec that declares it, a spec that
declares it wrongly, a config that configures it, and the one policy that cannot stand beside it —
and nothing about similarity, because nothing here computes one.
"""

from __future__ import annotations

import textwrap

import pytest

from loom import build
from loom.config import DEFAULT_EMBEDDING_PROVIDER, load_config
from loom.errors import Diagnostics, SpecErrors
from loom.expr import parse as parse_expr
from loom.governance import Policy, PolicyError, bind_policies

OBJECT = """
objectType:
  apiName: Ticket
  primaryKey: ticketId
  backing: {{ catalog: c, table: support.tickets }}
  properties:
    - {{ name: ticketId, type: string, column: id, unique: true }}
    - {{ name: body,     type: string, column: body }}
    - {{ name: opened,   type: timestamp, column: opened_at }}
    - {{ name: severity, type: enum, values: [low, high], column: severity }}
  searchable: [severity]
{extra}
"""


def _build(tmp_path, extra: str = ""):
    (tmp_path / "o.yaml").write_text(textwrap.dedent(OBJECT.format(extra=extra)))
    return build(tmp_path)


def _refuses(tmp_path, extra: str, substring: str):
    with pytest.raises(SpecErrors) as ei:
        _build(tmp_path, extra)
    messages = "\n".join(e.render() for e in ei.value.errors)
    assert substring in messages, f"expected {substring!r} in:\n{messages}"


# ---- the declaration -------------------------------------------------------------


def test_a_string_property_can_be_declared_semantic(tmp_path):
    ontology, diag = _build(tmp_path, "  semantic: body")
    assert ontology.object_types["Ticket"].semantic == "body"
    assert diag.warnings == []


def test_semantic_is_absent_by_default(tmp_path):
    """Every spec written before this milestone declares nothing, and stays a spec that means what
    it meant. `None` rather than `()` because this key names one property, not a list of them."""
    ontology, _ = _build(tmp_path)
    assert ontology.object_types["Ticket"].semantic is None
    assert ontology.object_types["Ticket"].semantic_property is None


def test_semantic_property_resolves_to_the_declared_property(tmp_path):
    ontology, _ = _build(tmp_path, "  semantic: body")
    prop = ontology.object_types["Ticket"].semantic_property
    assert prop is not None and prop.column == "body"


def test_it_does_not_have_to_be_searchable(tmp_path):
    """The two declarations are independent, and this fixture says so by declaring `severity`
    searchable and `body` semantic. `searchable` is what a filter may narrow by; `semantic` is what
    a ranking may order by. A spec may ask for either without the other."""
    ontology, _ = _build(tmp_path, "  semantic: body")
    obj = ontology.object_types["Ticket"]
    assert obj.searchable == ("severity",) and obj.semantic == "body"


# ---- what it refuses -------------------------------------------------------------


def test_a_property_no_type_declares_is_refused(tmp_path):
    _refuses(tmp_path, "  semantic: bodyy", "semantic property 'bodyy' is not a declared property")


def test_an_undeclared_property_gets_a_did_you_mean(tmp_path):
    """The same courtesy every other name in this grammar gets — a `semantic:` typo is otherwise
    invisible in the output it produces, because what it produces is one missing tool."""
    _refuses(tmp_path, "  semantic: bodyy", "body")


@pytest.mark.parametrize(
    "prop,kind", [("opened", "timestamp"), ("severity", "enum"), ("ticketId", "string")]
)
def test_only_a_string_may_be_embedded(tmp_path, prop, kind):
    """Narrower than `searchable`, which M7 widened to every scalar, and deliberately so.

    A ranking needs prose to rank. An ordered type already has an order and `gte` says exactly what
    a similarity score would approximate; a closed set is enumerable and `eq`/`in` answer it
    exactly. `ticketId` is in the list as the case that *is* a string and passes — the parametrise
    asserts the rule is about the type and not about the name."""
    extra = f"  semantic: {prop}"
    if kind == "string":
        ontology, _ = _build(tmp_path, extra)
        assert ontology.object_types["Ticket"].semantic == prop
    else:
        _refuses(tmp_path, extra, f"semantic property '{prop}' is {kind}")


def test_a_list_is_refused_as_a_shape(tmp_path):
    """One property, so the key is a name. The refusal says which shape it wanted rather than
    accepting a one-element list as an alternative spelling — M8's lesson about two spellings, paid
    before there are two."""
    _refuses(tmp_path, "  semantic: [body]", "one property, so this key is a name and not a list")


def test_an_empty_string_is_refused(tmp_path):
    _refuses(tmp_path, "  semantic: '  '", "must be the name of one string property")


# ---- the deployment half ---------------------------------------------------------


CONFIG = """
version: 0
catalogs:
  c: {{ type: iceberg-sql, uri: "sqlite:///x.db", warehouse: /tmp/w }}
engine: {{ type: duckdb }}
mcp:
{mcp}
"""


def _config(tmp_path, mcp: str):
    p = tmp_path / "loom.yaml"
    p.write_text(textwrap.dedent(CONFIG.format(mcp=mcp)))
    diag = Diagnostics()
    return load_config(p, diag), diag


def test_a_deployment_declares_provider_and_model(tmp_path):
    config, diag = _config(tmp_path, "  embedding: { provider: local, model: bge-small }")
    assert config.mcp.embedding is not None
    assert config.mcp.embedding.provider == "local"
    assert config.mcp.embedding.model == "bge-small"
    assert diag.errors == []


def test_the_provider_defaults_to_local(tmp_path):
    """No bytes of somebody's lake leave the machine unless a deployment says so — the same posture
    as the loopback default bind."""
    config, _ = _config(tmp_path, "  embedding: { model: bge-small }")
    assert config.mcp.embedding.provider == DEFAULT_EMBEDDING_PROVIDER == "local"


def test_the_model_has_no_default(tmp_path):
    """It is folded into every stored vector's hash, so a default Loom could change in a later
    release would silently invalidate every vector in every warehouse that took it."""
    _, diag = _config(tmp_path, "  embedding: { provider: local }")
    assert any("embedding.model" in e.render() for e in diag.errors)


def test_an_unknown_provider_is_refused_with_a_suggestion(tmp_path):
    _, diag = _config(tmp_path, "  embedding: { provider: opanai, model: m }")
    rendered = "\n".join(e.render() for e in diag.errors)
    assert "unknown embedding provider 'opanai'" in rendered and "openai" in rendered


def test_dims_is_not_a_key(tmp_path):
    """Declaring the width beside the model name is a chance to declare it wrong, and the failure
    is silent: vectors of the declared width get written and ranked against each other. The
    provider is asked instead."""
    _, diag = _config(tmp_path, "  embedding: { provider: local, model: m, dims: 384 }")
    assert any("dims" in e.render() for e in diag.errors)


def test_absent_embedding_is_not_an_error(tmp_path):
    """Every deployment before this milestone, and still the default. Absent withholds a tool; it
    does not refuse to start — that distinction is `check_capabilities`' and not this key's."""
    config, diag = _config(tmp_path, "  transport: stdio")
    assert config.mcp.embedding is None
    assert diag.errors == []


# ---- the pairing a policy cannot make ---------------------------------------------


def test_a_mask_over_the_semantic_property_is_refused(tmp_path):
    """The fifth thing a mask cannot withhold.

    Sharper than the filter refusal this module already makes: filtering on a withheld value lets a
    caller binary-search it a bit at a time, and a ranking hands back how *near* each row came, so
    the same probe returns a gradient. A combination refusal — the spec is fine, the policy is fine,
    and the deployment of the two together cannot stand."""
    ontology, _ = _build(tmp_path, "  semantic: body")
    with pytest.raises(PolicyError) as ei:
        bind_policies(ontology, [Policy(name="hide-body", object_type="Ticket", mask=("body",))])
    message = str(ei.value)
    assert "semantic property" in message and "gradient" in message


def test_masking_a_different_property_is_untouched(tmp_path):
    """The refusal is about the pairing and not about the object type — a deployment may still
    withhold anything else on a type that declares a semantic property."""
    ontology, _ = _build(tmp_path, "  semantic: body")
    bound = bind_policies(
        ontology, [Policy(name="hide-severity", object_type="Ticket", mask=("severity",))]
    )
    assert bound.select(None).masked("Ticket") == ("severity",)


def test_a_row_predicate_over_the_semantic_property_is_not_refused(tmp_path):
    """A predicate uses the value and shows nobody, which is the distinction `bind_policies`
    already draws for the other four. Filtering the candidate set by the same property a ranking
    orders it by is Loom narrowing rather than the caller reading."""
    ontology, _ = _build(tmp_path, "  semantic: body")
    bound = bind_policies(
        ontology,
        [Policy(name="only-open", object_type="Ticket", rows=parse_expr("object.body != 'closed'"))],
    )
    assert bound.select(None).masked("Ticket") == ()
