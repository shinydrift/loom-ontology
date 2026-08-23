"""`semantic:` — the grammar, the refusals, and the surface they turned into.

The first half is M10's first slice, which shipped no vector and no tool: the ability to *say* a
property is worth searching by meaning, the deployment key that says where a vector would come from,
and every refusal the pairing of those two owes. The second half is slice 3's `match_<object>` — the
tool set, the schema and the envelope, plus the two refusals that live above the resolver.

What is *not* here is a similarity. Nothing in this module computes one: the plan is
`test_resolver.py`'s, the SQL is `test_query_compile.py`'s, and whether a ranking actually ranks is
`test_match_iceberg.py`'s, because it is the one claim a stub cannot make.
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


# ---- the tool ----------------------------------------------------------------------
#
# Slice 3. What is asserted here is the *surface* and the two refusals above the resolver — which
# tools get built, what their schema says, what comes back — against a stub provider and a fake
# catalog, because none of that needs a model or a lake. `test_query_compile.py` owns the SQL,
# `test_resolver.py` owns the plan, and `test_match_iceberg.py` owns the one claim only a real
# engine over a real warehouse can make: that a ranking actually ranks.


class StubProvider:
    """Two floats per text, so a similarity is arithmetic anybody can check by hand."""

    model = "stub-v1"
    dims = 2

    def __init__(self, vector=(1.0, 0.0)):
        self.vector = vector
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [self.vector for _ in texts]


class StubCatalog:
    """Answers the one question the ranked read plane asks a catalog directly."""

    def __init__(self, tables=("_loom_meta.vectors__Ticket",)):
        self.name = "c"
        self.tables = set(tables)
        self.asked: list[str] = []

    def table_exists(self, table):
        self.asked.append(table)
        return table in self.tables


class StubEngine:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.plans = []

    def capabilities(self):
        from loom.query.engine import Capabilities

        return Capabilities(name="stub", vector_search=True)

    def compile(self, plan):
        from loom.query.engine import CompiledQuery

        self.plans.append(plan)
        return CompiledQuery(sql="<stub>")

    def execute(self, compiled):
        return self.rows


def _matcher(provider=None, catalog=None, ontology=None, types=("Ticket",)):
    from loom.embed.match import Matcher
    from loom.embed.store import VectorStore

    return Matcher(
        provider=provider or StubProvider(),
        stores={
            name: VectorStore(catalog=catalog or StubCatalog(), object_type=name, key_type="string")
            for name in types
        },
    )


def _tools(tmp_path, rows=(), matcher=..., extra="  semantic: body"):
    from loom.mcp.registry import build_tools
    from loom.resolver import Resolver

    ontology, _ = _build(tmp_path, extra)
    resolver = Resolver(ontology=ontology, engine=StubEngine(rows))
    if matcher is ...:
        matcher = _matcher()
    return {t.name: t for t in build_tools(resolver, matcher=matcher)}, resolver


RANKED = [
    {"ticketId": "t1", "body": "chargeback filed", "opened": None, "severity": "high",
     "_loom_score": 0.91, "_loom_embedded_at": None},
    {"ticketId": "t2", "body": "shipping late", "opened": None, "severity": "low",
     "_loom_score": 0.12, "_loom_embedded_at": None},
]


def test_a_declared_semantic_property_and_a_provider_make_a_tool(tmp_path):
    tools, _ = _tools(tmp_path)
    assert "match_ticket" in tools


def test_no_provider_withholds_the_tool_and_nothing_else(tmp_path):
    """`mcp.writes: false`'s posture. A deployment that configures no model is not withholding a
    ranking it could serve — it has none — so every other tool is untouched."""
    tools, _ = _tools(tmp_path, matcher=None)
    assert "match_ticket" not in tools
    assert {"get_ticket", "search_ticket", "list_ticket"} <= set(tools)


def test_a_type_that_declares_nothing_gets_no_tool(tmp_path):
    """Even with a provider configured: the spec declares intent and the deployment declares
    mechanism, so both halves have to be there."""
    tools, _ = _tools(tmp_path, extra="", matcher=_matcher(types=()))
    assert not [t for t in tools if t.startswith("match_")]


def test_the_tool_takes_text_and_the_same_filters_search_takes(tmp_path):
    tools, _ = _tools(tmp_path)
    schema = tools["match_ticket"].input_schema
    assert schema["required"] == ["text"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"text", "filter", "limit", "offset"}
    # `severity` is what this fixture declares searchable, and the ranked read may narrow on exactly
    # the same set — one question with one answer.
    assert set(schema["properties"]["filter"]["properties"]) == set(
        tools["search_ticket"].input_schema["properties"]["filter"]["properties"]
    )


def test_the_text_argument_is_the_callers_words_not_the_propertys_value(tmp_path):
    tools, _ = _tools(tmp_path)
    described = tools["match_ticket"].input_schema["properties"]["text"]["description"]
    assert "your own words" in described and "Ticket.body" in described


def test_a_ranked_result_puts_the_score_beside_the_object(tmp_path):
    tools, _ = _tools(tmp_path, rows=RANKED)
    out = tools["match_ticket"].handler({"text": "customer wanted their money back"})
    assert out["objectType"] == "Ticket" and out["property"] == "body"
    assert out["model"] == "stub-v1"
    assert [m["score"] for m in out["matches"]] == [0.91, 0.12]
    assert out["matches"][0]["object"]["ticketId"] == "t1"
    assert "score" not in out["matches"][0]["object"]


def test_the_envelope_pages_like_every_other_read(tmp_path):
    tools, _ = _tools(tmp_path, rows=RANKED)
    out = tools["match_ticket"].handler({"text": "dispute", "limit": 2})
    assert out["count"] == 2 and out["limit"] == 2 and out["offset"] == 0
    assert out["hasMore"] is True
    assert tools["match_ticket"].handler({"text": "dispute", "limit": 5})["hasMore"] is False


def test_the_caller_s_words_are_what_gets_embedded(tmp_path):
    provider = StubProvider()
    tools, _ = _tools(tmp_path, rows=RANKED, matcher=_matcher(provider=provider))
    tools["match_ticket"].handler({"text": "  sent the money back  "})
    # Stripped, and embedded once — a batch verb with a batch of one, never a call per row.
    assert provider.calls == [["sent the money back"]]


def test_a_filter_reaches_the_plan(tmp_path):
    tools, resolver = _tools(tmp_path, rows=RANKED)
    tools["match_ticket"].handler({"text": "dispute", "filter": {"severity": "high"}})
    assert resolver.engine.plans[-1].source.filters


def test_blank_text_is_refused_rather_than_answered(tmp_path):
    """`{"in": []}`'s argument. An empty ranking a caller cannot tell from a real one is worse than
    a sentence saying what to do, and `embeddable` decides this exactly as it decides a row has no
    text."""
    from loom.resolver import ResolverError

    tools, _ = _tools(tmp_path)
    for text in ("", "   "):
        with pytest.raises(ResolverError, match="empty or blank"):
            tools["match_ticket"].handler({"text": text})


def test_an_unembedded_type_is_refused_and_names_the_command(tmp_path):
    """A sidecar that does not exist yet is an ordinary state of an ordinary deployment, so it gets
    a sentence rather than a catalog error — and rather than an empty page, which a caller could not
    tell from *nothing was similar*."""
    from loom.resolver import ResolverError

    tools, _ = _tools(tmp_path, matcher=_matcher(catalog=StubCatalog(tables=())))
    with pytest.raises(ResolverError, match="loom embed --type Ticket"):
        tools["match_ticket"].handler({"text": "dispute"})


def test_the_lake_is_only_asked_after_the_argument_is_checked(tmp_path):
    """`cmd_query`'s ordering: a blank query should not need a reachable metastore to be refused."""
    from loom.resolver import ResolverError

    catalog = StubCatalog()
    tools, _ = _tools(tmp_path, matcher=_matcher(catalog=catalog))
    with pytest.raises(ResolverError):
        tools["match_ticket"].handler({"text": ""})
    assert catalog.asked == []


def test_the_ranked_plane_holds_nothing_that_can_write_a_vector(tmp_path):
    """Slice 2 split `VectorStore` in two so this could be a fact about the object rather than a
    convention: a serving process can rank a sidecar and cannot maintain one."""
    matcher = _matcher()
    assert all(store.writer is None for store in matcher.stores.values())


def test_bind_matching_answers_none_for_a_deployment_with_no_provider(tmp_path):
    from loom.embed.match import bind_matching

    ontology, _ = _build(tmp_path, "  semantic: body")
    config, _ = _config(tmp_path, "  writes: false")
    assert bind_matching(ontology, config, {}) is None


def test_bind_matching_answers_none_for_a_spec_that_declares_nothing(tmp_path):
    from loom.embed.match import bind_matching

    ontology, _ = _build(tmp_path, "")
    config, _ = _config(tmp_path, "  embedding: { provider: local, model: bge-small }")
    assert bind_matching(ontology, config, {}) is None


def test_bind_matching_loads_no_model(tmp_path):
    """A server whose embedding model is a 150MB download still starts in the time it always did —
    `provider_for` stays offline by construction and the first `match_` pays."""
    from loom.embed.match import bind_matching
    from loom.embed.provider import LocalProvider

    ontology, _ = _build(tmp_path, "  semantic: body")
    config, _ = _config(tmp_path, "  embedding: { provider: local, model: bge-small }")
    matcher = bind_matching(ontology, config, {"c": StubCatalog()})
    assert isinstance(matcher.provider, LocalProvider)
    assert matcher.provider._impl is None and matcher.provider._dims is None
