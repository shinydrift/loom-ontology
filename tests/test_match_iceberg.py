"""`match_<object>` end to end — the one claim a stub cannot make.

Every other module in this milestone asserts a *decision*: `test_semantic.py` the surface and the
refusals, `test_resolver.py` the plan, `test_query_compile.py` the SQL. None of them proves the thing
the whole plane exists for, which is that a ranking **ranks** — that a `list<float>` written by
`loom embed` survives Iceberg, arrives in DuckDB as something `array_cosine_similarity` can measure,
and comes back ordered by distance rather than by whatever the scan happened to yield.

So this runs the real stack with nothing stubbed but the model: pyiceberg -> Arrow -> DuckDB ->
resolver -> tool dispatch, against a warehouse this module creates. A bespoke fixture rather than the
shipped example, and for once that is the right way round — the example's only string columns are
people's names, and a test whose subject is *meaning* wants prose. The provider is a hand-written
lexicon over three axes, so every expected ordering here is arithmetic a reader can check.

Six things are asserted that only this stack can answer:

- the join and the ordering: nearest first, and the tie-break is what makes page 2 follow page 1;
- that a filter narrows **before** the ranking, rather than re-ranking what a filter kept;
- that a governance predicate removes a row from the ranking entirely — it does not rank it low;
- that the comparability guard means a sidecar holding two widths **answers** rather than raising,
  which is the failure mode DuckDB has when the guard is absent;
- that a row with no vector is simply absent, which is the cost this milestone accepted out loud;
- that `loom query --match` and the generated tool return the same rows in the same order.
"""

from __future__ import annotations

import textwrap
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
pytest.importorskip("pyarrow", reason="needs the [duckdb] extra")
pytest.importorskip("duckdb", reason="needs the [duckdb] extra")

import pyarrow as pa  # noqa: E402

from loom import build  # noqa: E402
from loom.catalog import open_catalogs  # noqa: E402
from loom.catalog.base import vector_table, vector_writer_for  # noqa: E402
from loom.config import find_config, load_config  # noqa: E402
from loom.embed import APPLIED, EmbedRuntime  # noqa: E402
from loom.embed.match import Matcher, bind_matching  # noqa: E402
from loom.embed.store import VectorRow, VectorStore, now, source_hash  # noqa: E402
from loom.errors import Diagnostics  # noqa: E402
from loom.mcp.server import build_server  # noqa: E402
from loom.resolver import build_resolver  # noqa: E402

SPEC = """
objectType:
  apiName: Ticket
  displayName: Support ticket
  primaryKey: ticketId
  backing: { catalog: local, table: support.tickets }
  properties:
    - { name: ticketId, type: string, column: id, unique: true }
    - { name: body,     type: string, column: body, nullable: true }
    - { name: severity, type: enum, values: [low, high], column: severity }
    - { name: queue,    type: string, column: queue }
  searchable: [severity, queue]
  semantic: body
"""

CONFIG = """
version: 0
catalogs:
  local:
    type: iceberg-sql
    uri: "sqlite:///{root}/catalog.db"
    warehouse: "{root}/warehouse"
engine: {{ type: duckdb }}
mcp:
  name: tickets
  embedding: {{ provider: local, model: stub-v1 }}
{extra}
"""

SCHEMA = pa.schema(
    [
        pa.field("id", pa.string(), nullable=False),
        pa.field("body", pa.string(), nullable=True),
        pa.field("severity", pa.string(), nullable=False),
        pa.field("queue", pa.string(), nullable=False),
    ]
)

# Prose an exact filter cannot find and a person immediately can. `t1`/`t4` are the payment
# dispute; `t2`/`t5` are delivery; `t3` is neither; `t6` has no text at all.
TICKETS = pa.table(
    {
        "id": ["t1", "t2", "t3", "t4", "t5", "t6"],
        "body": [
            "customer wanted their money back and we sent it",
            "the parcel never turned up",
            "asked how to change the account email",
            "chargeback raised with the bank over a duplicate charge",
            "courier left the box with a neighbour",
            None,
        ],
        "severity": ["high", "low", "low", "high", "low", "low"],
        "queue": ["billing", "logistics", "accounts", "billing", "logistics", "billing"],
    },
    schema=SCHEMA,
)

# Three axes, three word lists. A vector is how many of each list's words the text contains, so
# every ranking in this module is countable by hand.
AXES = (
    ("money", "refund", "chargeback", "charge", "bank", "paid", "payment"),
    ("parcel", "courier", "box", "delivery", "shipped", "neighbour", "turned"),
    ("account", "email", "password", "login"),
)


class LexiconProvider:
    """A model in the sense that matters here: same text in, same vector out, and near texts near.

    Not a hash. A hashing stub would prove the plumbing and nothing about the ordering, and the
    ordering is the only thing this module exists to check."""

    model = "stub-v1"

    def __init__(self, dims: int = 3):
        self._dims = dims

    @property
    def dims(self) -> int:
        return self._dims

    def embed(self, texts):
        return [self._one(t) for t in texts]

    def _one(self, text: str):
        words = set(text.lower().replace(",", " ").split())
        raw = [float(len(words & set(axis))) for axis in AXES[: self._dims]]
        # Normalised, so cosine similarity is about *which* axis rather than how wordy the row is.
        scale = sum(x * x for x in raw) ** 0.5 or 1.0
        return tuple(x / scale for x in raw)


@pytest.fixture
def project(tmp_path):
    """A real Iceberg warehouse with one table of prose in it, and a spec that declares it."""
    return _project(tmp_path)


def _project(
    tmp_path: Path,
    extra: str = "",
    rows: pa.Table = TICKETS,
    specs: tuple = (),
    tables: tuple = (),
):
    """`specs` and `tables` are slice 4's: a `via` needs a second object type to hop to, and every
    test above this one is about a plane that has none."""
    from pyiceberg.catalog.sql import SqlCatalog

    root = tmp_path / "proj"
    (root / "ontology").mkdir(parents=True)
    (root / "warehouse").mkdir(parents=True)
    (root / "ontology" / "ticket.yaml").write_text(textwrap.dedent(SPEC))
    for name, text in specs:
        (root / "ontology" / name).write_text(textwrap.dedent(text))
    # Beside the ontology directory rather than in it, as the shipped example has it: `build()`
    # reads every yaml file in the directory and a `loom.yaml` among them is not a spec.
    (root / "loom.yaml").write_text(
        textwrap.dedent(CONFIG.format(root=root.as_posix(), extra=extra))
    )

    diag = Diagnostics()
    config = load_config(find_config(root / "ontology"), diag)
    ontology, _ = build(root / "ontology")
    diag.raise_if_errors()

    catalog = SqlCatalog("local", uri=config.catalogs["local"].uri, warehouse=config.catalogs["local"].warehouse)
    catalog.create_namespace("support")
    catalog.create_table("support.tickets", schema=SCHEMA).append(rows)
    for name, schema, data in tables:
        catalog.create_table(name, schema=schema).append(data)
    return ontology, config, root


def _embedded(project, provider=None, catalogs=None):
    """Reconcile the sidecar for real, then hand back what a caller needs to rank against it."""
    ontology, config, root = project
    cats = catalogs if catalogs is not None else open_catalogs(config)
    provider = provider or LexiconProvider()
    result = EmbedRuntime(
        ontology=ontology, catalogs=cats, provider=provider, targets=("Ticket",)
    ).reconcile()
    assert result.status == APPLIED, result.failures
    return cats, provider


def _matcher(provider, catalogs) -> Matcher:
    return Matcher(
        provider=provider,
        stores={"Ticket": VectorStore(catalog=catalogs["local"], object_type="Ticket", key_type="string")},
    )


def _ranked(project, text, filters=None, limit=None, offset=0, catalogs=None, provider=None):
    ontology, config, _ = project
    cats, provider = _embedded(project, provider=provider, catalogs=catalogs)
    resolver = build_resolver(ontology, config, cats)
    return _matcher(provider, cats).match(
        resolver, "Ticket", text, filters or {}, limit=limit, offset=offset
    )


# ---- the ranking ----------------------------------------------------------------


def test_a_ranking_ranks(project):
    """The whole milestone in one assertion: the caller's words are not the data's words, and the
    two tickets about a refund come back ahead of the three that are not."""
    result = _ranked(project, "the customer wanted a refund from the bank")
    assert [m.object["ticketId"] for m in result.matches][:2] == ["t1", "t4"]
    assert result.matches[0].score > result.matches[2].score
    # `contains` finds none of them: the answer is in the text and the question's words are not.
    assert "refund" not in TICKETS.column("body")[0].as_py()


def test_a_different_question_reorders_the_same_rows(project):
    """A ranking is a function of the query and not a stored order — which a fixed ordering by
    primary key would also satisfy on one call and never on two."""
    result = _ranked(project, "the parcel was left with the neighbour")
    assert [m.object["ticketId"] for m in result.matches][:2] == ["t2", "t5"]


def test_a_row_with_no_text_is_simply_absent(project):
    """Blank text is the absence of text rather than staleness, so `t6` has no vector and cannot be
    ranked. The milestone accepted this out loud: `match_` can silently omit a row that exists, and
    `loom embed` is where the count of them lives."""
    result = _ranked(project, "money")
    assert "t6" not in [m.object["ticketId"] for m in result.matches]
    assert len(result.matches) == 5


def test_a_row_edited_after_it_was_embedded_comes_back_marked_stale(project):
    """The failure mode M10 did not name, and the one an omission cannot become.

    A row that was never embedded is *absent* — the milestone accepted that out loud. A row whose
    text changed after it was embedded is not absent: it is ranked by a sentence that is gone and
    returned carrying the sentence that replaced it, so the caller is holding the evidence the score
    is wrong and nothing in the answer says so. `t1` here is rewritten from a refund into a delivery
    complaint, and still answers the refund question off its dead vector."""
    ontology, config, root = project
    cats, provider = _embedded(project)
    from pyiceberg.catalog.sql import SqlCatalog

    catalog = SqlCatalog("local", uri=config.catalogs["local"].uri, warehouse=config.catalogs["local"].warehouse)
    table = catalog.load_table("support.tickets")
    rows = table.scan().to_arrow().to_pylist()
    for row in rows:
        if row["id"] == "t1":
            row["body"] = "the courier left the parcel by the bins"
    table.overwrite(pa.Table.from_pylist(rows, schema=SCHEMA))

    resolver = build_resolver(ontology, config, open_catalogs(config))
    result = _matcher(provider, cats).match(resolver, "Ticket", "money back from the bank")

    top = result.matches[0]
    assert top.object["ticketId"] == "t1"  # still first, off a vector for text it no longer holds
    assert top.object["body"] == "the courier left the parcel by the bins"
    assert top.stale is True
    assert result.stale_matches == 1
    # And every row nobody touched is unmarked, so the flag is about the row and not about the run.
    assert [m.stale for m in result.matches[1:]] == [False] * (len(result.matches) - 1)


def test_a_current_sidecar_marks_nothing_stale(project):
    """The control: staleness is a comparison against the text as it is *now*, so a reconcile that
    is level with its rows marks none of them — including `t3`, whose text no query here is near."""
    result = _ranked(project, "money back from the bank")
    assert result.stale_matches == 0
    assert not any(m.stale for m in result.matches)


def test_a_row_whose_text_was_blanked_is_stale_rather_than_fresh(project):
    """`embeddable` calls null and blank *the absence of text*, so a vector still standing for a
    blanked row describes something that was removed. It is the same shape as an edit and not the
    same shape as a row that was never embedded, which is why it is marked rather than dropped:
    the row is in the answer, wearing a score earned by prose nobody can read any more."""
    ontology, config, root = project
    cats, provider = _embedded(project)
    from pyiceberg.catalog.sql import SqlCatalog

    catalog = SqlCatalog("local", uri=config.catalogs["local"].uri, warehouse=config.catalogs["local"].warehouse)
    table = catalog.load_table("support.tickets")
    rows = table.scan().to_arrow().to_pylist()
    for row in rows:
        if row["id"] == "t1":
            row["body"] = "   "
    table.overwrite(pa.Table.from_pylist(rows, schema=SCHEMA))

    resolver = build_resolver(ontology, config, open_catalogs(config))
    result = _matcher(provider, cats).match(resolver, "Ticket", "money back from the bank")
    stale = {m.object["ticketId"] for m in result.matches if m.stale}
    assert stale == {"t1"}


def test_the_generated_tool_says_which_matches_are_stale(project):
    """The envelope's half of it. `staleMatches` is the operator's count and `stale` on a match is
    the caller's — the same split `embeddedAsOf` already draws between `loom embed`'s sidecar-wide
    number and this envelope's per-page one. A current sidecar carries neither key on a match, so
    the ordinary answer keeps the shape it has always had."""
    ontology, config, root = project
    cats, provider = _embedded(project)
    from pyiceberg.catalog.sql import SqlCatalog

    catalog = SqlCatalog("local", uri=config.catalogs["local"].uri, warehouse=config.catalogs["local"].warehouse)
    table = catalog.load_table("support.tickets")
    rows = table.scan().to_arrow().to_pylist()
    for row in rows:
        if row["id"] == "t1":
            row["body"] = "the courier left the parcel by the bins"
    table.overwrite(pa.Table.from_pylist(rows, schema=SCHEMA))

    tool = _rebuilt_tool(ontology, config, open_catalogs(config), provider)
    envelope, is_error = _call(tool, {"text": "money back from the bank"})

    assert is_error is False
    assert envelope["staleMatches"] == 1
    assert envelope["matches"][0]["stale"] is True
    assert "stale" not in envelope["matches"][1]
    assert "`stale: true`" in tool.description


def test_every_object_comes_back_as_its_declared_properties(project):
    result = _ranked(project, "money back")
    assert set(result.matches[0].object) == {"ticketId", "body", "severity", "queue"}
    assert result.property == "body" and result.model == "stub-v1"


def test_the_stamp_is_the_oldest_among_the_rows_returned(project):
    """The correction to slice 2's prediction, made checkable: one page's stamp is a fact about that
    page. A sidecar-wide reading could not distinguish these two calls, and it is the one the caller
    is *not* holding — `loom embed` reports that one, because it is the operator's question."""
    ontology, config, _ = project
    cats, provider = _embedded(project)

    # `t5` is the delivery ticket nobody asks about here, backdated to a reconcile months ago.
    ancient = datetime(2026, 1, 1, tzinfo=UTC)
    store = VectorStore(
        catalog=cats["local"],
        object_type="Ticket",
        key_type="string",
        writer=vector_writer_for(cats["local"]),
    )
    stored = {r["key"]: r for r in _sidecar_rows(project)}
    store.merge(
        [
            VectorRow(
                key="t5",
                property="body",
                model=provider.model,
                dims=provider.dims,
                vector=stored["t5"]["vector"],
                source_hash=stored["t5"]["source_hash"],
                embedded_at=ancient,
            )
        ],
        expect_snapshot_id=store.snapshot_id(),
    )

    resolver = build_resolver(ontology, config, cats)
    matcher = _matcher(provider, cats)
    money = matcher.match(resolver, "Ticket", "money back from the bank", limit=2)
    delivery = matcher.match(resolver, "Ticket", "the parcel was left with the neighbour")

    assert "t5" not in [m.object["ticketId"] for m in money.matches]
    assert money.embedded_as_of != ancient
    assert "t5" in [m.object["ticketId"] for m in delivery.matches]
    assert delivery.embedded_as_of == ancient


# ---- paging ---------------------------------------------------------------------


def test_pages_of_a_ranking_are_disjoint_and_in_order(project):
    """The tie-break is what buys this: without a total order, page 2 would be an unrelated draw
    from the same set rather than the continuation of page 1."""
    ontology, config, _ = project
    cats, provider = _embedded(project)
    resolver = build_resolver(ontology, config, cats)
    matcher = _matcher(provider, cats)

    whole = matcher.match(resolver, "Ticket", "money back from the bank")
    first = matcher.match(resolver, "Ticket", "money back from the bank", limit=2)
    second = matcher.match(resolver, "Ticket", "money back from the bank", limit=2, offset=2)

    ordered = [m.object["ticketId"] for m in whole.matches]
    assert [m.object["ticketId"] for m in first.matches] == ordered[:2]
    assert [m.object["ticketId"] for m in second.matches] == ordered[2:4]


def test_ties_break_on_the_primary_key(project):
    """`t3` and `t6`… — the three rows with no overlap with the query all score identically, so
    something other than the score has to decide, and it has to decide the same way twice."""
    once = _ranked(project, "chargeback")
    twice = _ranked(project, "chargeback")
    assert [m.object["ticketId"] for m in once.matches] == [
        m.object["ticketId"] for m in twice.matches
    ]
    zeroes = [m.object["ticketId"] for m in once.matches if m.score == 0.0]
    assert zeroes == sorted(zeroes)


# ---- filtering, which is part of retrieval ---------------------------------------


def test_a_filter_narrows_before_the_ranking(project):
    """Not a filter over the top k — a filtered call ranks fewer rows. `t5` is the second-best
    delivery ticket and only appears once the filter admits it."""
    everything = _ranked(project, "the parcel never arrived")
    logistics = _ranked(project, "the parcel never arrived", {"queue": "logistics"})
    assert {m.object["ticketId"] for m in logistics.matches} == {"t2", "t5"}
    assert len(everything.matches) > len(logistics.matches)


def test_a_filter_can_leave_nothing_to_rank(project):
    result = _ranked(project, "money", {"queue": "nowhere"})
    assert result.matches == ()
    assert result.embedded_as_of is None


def test_the_top_of_a_filtered_ranking_is_not_the_top_of_the_whole_one(project):
    """The two orders a similarity clause inside `filter:` would have had to choose between,
    distinguishable here: rank-then-filter would return nothing at all."""
    result = _ranked(project, "the customer wanted a refund", {"severity": "low"})
    assert result.matches, "rank-then-filter would have thrown away every low-severity row"
    assert result.matches[0].object["severity"] == "low"


# ---- governance ------------------------------------------------------------------


GOVERNED = """
governance:
  policies:
    - name: billing-only
      objectType: Ticket
      rows: "object.queue == 'billing'"
"""


def test_a_governed_row_is_not_ranked_low_it_does_not_exist(tmp_path):
    """The predicate rides on `ir.TableRef` at the point a type becomes a table, so a ranked read is
    governed by the same line `get`, `search` and `traverse` are — with nothing written for it."""
    project = _project(tmp_path, extra=GOVERNED)
    result = _ranked(project, "the parcel never turned up")
    assert {m.object["ticketId"] for m in result.matches} == {"t1", "t4"}
    # And the row that *would* have won is the one withheld: it is absent, not demoted.
    assert "t2" not in [m.object["ticketId"] for m in result.matches]


# ---- the comparability guard ------------------------------------------------------


def _sidecar_rows(project):
    _, config, _ = project
    return open_catalogs(config)["local"].scan(vector_table("Ticket")).to_pylist()


def test_a_sidecar_holding_two_widths_answers_rather_than_raising(project):
    """The guard's sharpest justification, and the reason it is in the `WHERE` and not only in the
    scan: `array_cosine_similarity` over two widths is an *error* in DuckDB. A warehouse caught
    part-way through a `--remodel` therefore has to be survivable, and what survives is the
    generation this deployment configures."""
    ontology, config, _ = project
    cats, provider = _embedded(project)

    # One row re-embedded by a wider model, exactly as an interrupted remodel would leave it.
    wide = LexiconProvider(dims=2)
    store = VectorStore(
        catalog=cats["local"],
        object_type="Ticket",
        key_type="string",
        writer=vector_writer_for(cats["local"]),
    )
    text = "customer wanted their money back and we sent it"
    store.merge(
        [
            VectorRow(
                key="t1",
                property="body",
                model="other-model",
                dims=2,
                vector=wide.embed([text])[0],
                source_hash=source_hash(text, "other-model", 2, "body"),
                embedded_at=now(),
            )
        ],
        expect_snapshot_id=store.snapshot_id(),
    )
    assert {r["dims"] for r in _sidecar_rows(project)} == {2, 3}

    resolver = build_resolver(ontology, config, cats)
    result = _matcher(provider, cats).match(resolver, "Ticket", "money back from the bank")
    # It answered. And the row of the other generation is not in it — a vector from another model is
    # not a worse match, the arithmetic over it denotes nothing.
    assert "t1" not in [m.object["ticketId"] for m in result.matches]
    assert result.matches[0].object["ticketId"] == "t4"


def test_re_pointing_semantic_does_not_rank_the_old_property_s_vectors(tmp_path):
    """The other half of the guard, and the window it closes is narrow and invisible. Moving
    `semantic:` to a different column changes every `source_hash`, so the next reconcile fixes it —
    but until then the sidecar holds vectors of the *old* text, and ranking them under an envelope
    naming the new property would be a wrong answer with a fresh timestamp on it."""
    project = _project(tmp_path)
    ontology, config, _ = project
    cats, provider = _embedded(project)

    moved = replace(ontology.object_types["Ticket"], semantic="queue")
    reaimed = replace(ontology, object_types={"Ticket": moved})
    resolver = build_resolver(reaimed, config, cats)
    result = _matcher(provider, cats).match(resolver, "Ticket", "money back from the bank")

    assert result.property == "queue"
    assert result.matches == ()
    # And a reconcile against the new property makes it answer again, with the new vectors.
    EmbedRuntime(ontology=reaimed, catalogs=cats, provider=provider, targets=("Ticket",)).reconcile()
    assert _matcher(provider, cats).match(resolver, "Ticket", "billing").matches


# ---- the surface -----------------------------------------------------------------


def test_the_generated_tool_returns_what_the_resolver_ranked(project):
    """`build_server` end to end, over the same warehouse: the tool exists because the spec declares
    `semantic:` and the config names a provider, and its envelope carries the ranking."""
    ontology, config, _ = project
    cats, provider = _embedded(project)
    server, _ = build_server(ontology, config, cats)
    assert "match_ticket" in server.tools

    # The real config names `provider: local`, which would reach for fastembed on the first call.
    # This module is about the plane below the model, so the tool is reassembled through the same
    # `build_tools` with the lexicon the sidecar was actually filled by.
    tool = _rebuilt_tool(ontology, config, cats, provider)
    out, is_error = _call(tool, {"text": "the customer wanted a refund"})
    assert is_error is False
    assert [m["object"]["ticketId"] for m in out["matches"]][:2] == ["t1", "t4"]
    assert out["count"] == 5 and out["hasMore"] is False
    assert out["objectType"] == "Ticket" and out["property"] == "body"
    assert out["model"] == "stub-v1" and out["embeddedAsOf"] is not None
    assert out["masked"] == []
    assert out["matches"][0]["score"] > out["matches"][2]["score"]
    # The namespace rule, at the far end of the whole stack: Loom's word beside the object, never
    # inside it, so a spec that declared a property called `score` would still round-trip.
    assert set(out["matches"][0]["object"]) == {"ticketId", "body", "severity", "queue"}


def test_loom_query_match_returns_what_the_tool_returns(project, monkeypatch, capsys):
    """`loom query` mirrors the generated tools deliberately — if the dev command can do something
    the tools cannot, the ontology has a back door. Driven through `main()`, so the argument parsing
    and the refusals are the ones a person at a terminal meets."""
    import json as _json

    from loom.cli import main

    ontology, config, root = project
    cats, provider = _embedded(project)
    monkeypatch.setattr("loom.embed.match.provider_for", lambda _config: provider)

    assert main(["query", "Ticket", str(root / "ontology"), "--match", "the customer wanted a refund"]) == 0
    out = capsys.readouterr()
    rows = _json.loads(out.out)
    tool_out, _ = _call(
        _rebuilt_tool(ontology, config, cats, provider), {"text": "the customer wanted a refund"}
    )
    assert [r["object"]["ticketId"] for r in rows] == [
        m["object"]["ticketId"] for m in tool_out["matches"]
    ]
    # The two facts a bare list of rows cannot carry, said on stderr where the mask already is.
    assert "ranked by Ticket.body against 'stub-v1'" in out.err


def test_loom_query_refuses_match_beside_key(project, capsys):
    """A ranked read and a keyed one are different verbs. A similarity over the one row you already
    named is a number about nothing, so it is refused rather than resolved by precedence."""
    _, _, root = project
    assert main_argv(["query", "Ticket", str(root / "ontology"), "--match", "x", "--key", "t1"]) == 1
    assert "cannot be combined with --key" in capsys.readouterr().err


def main_argv(argv):
    from loom.cli import main

    return main(argv)


def test_an_unembedded_deployment_refuses_with_a_sentence(tmp_path):
    """Before any reconcile has run there is no sidecar, which is an ordinary state of an ordinary
    deployment — so it gets a sentence naming the command rather than a catalog error."""
    from loom.resolver import ResolverError

    ontology, config, _ = _project(tmp_path)
    cats = open_catalogs(config)
    matcher = bind_matching(ontology, config, cats)
    resolver = build_resolver(ontology, config, cats)
    with pytest.raises(ResolverError, match="loom embed --type Ticket"):
        matcher.match(resolver, "Ticket", "anything")


# ---- helpers ---------------------------------------------------------------------


def _rebuilt_tool(ontology, config, catalogs, provider):
    """The generated tool, with this module's provider in place of the configured one.

    `build_server` would build a `LocalProvider` and reach for fastembed on the first call. What is
    under test here is everything *below* the model, so the tool is assembled through the same
    `build_tools` the server uses and handed the provider the sidecar was actually filled by."""
    from loom.mcp.registry import build_tools

    resolver = build_resolver(ontology, config, catalogs)
    tools = {
        t.name: t
        for t in build_tools(resolver, matcher=_matcher(provider, catalogs))
    }
    return tools["match_ticket"]


def _call(tool, arguments):
    import json as _json

    from loom.mcp.server import LoomMCPServer

    server = LoomMCPServer(tools={tool.name: tool})
    text, is_error = server.call(tool.name, arguments)
    return (_json.loads(text) if not is_error else text), is_error


# ---- via -------------------------------------------------------------------------
#
# Slice 4, and the claims here are the ones only a real engine over a real warehouse can make: that
# a semi-join actually narrows, that a to-many hop does not quietly return the same object several
# times, and that a governance predicate on the *far* end withholds rows — which is the case that
# fails outright if a hop's predicate is left in the top-level `WHERE`, because the alias is out of
# scope there and the parameters no longer line up with the clauses.

QUEUE_SPEC = """
objectType:
  apiName: Queue
  displayName: Support queue
  primaryKey: queueId
  backing: { catalog: local, table: support.queues }
  properties:
    - { name: queueId, type: string, column: id, unique: true }
    - { name: owner,   type: string, column: owner }
    - { name: region,  type: string, column: region }
    - { name: charter, type: string, column: charter }
  searchable: [owner, region]
  semantic: charter
"""

LINK_SPEC = """
linkType:
  apiName: handledBy
  cardinality: many_to_one
  from: { objectType: Ticket, property: queue }
  to:   { objectType: Queue,  property: queueId }
  reverseName: tickets
"""

QUEUE_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string(), nullable=False),
        pa.field("owner", pa.string(), nullable=False),
        pa.field("region", pa.string(), nullable=False),
        pa.field("charter", pa.string(), nullable=False),
    ]
)

# `accounts` is deliberately missing: `t3` is a ticket whose queue names no row, which is the only
# way an empty `{}` hop can be told apart from a no-op.
QUEUES = pa.table(
    {
        "id": ["billing", "logistics"],
        "owner": ["ada", "grace"],
        "region": ["emea", "amer"],
        "charter": [
            "we handle money paid and refunds charged back",
            "we handle the parcel, the courier and the box",
        ],
    },
    schema=QUEUE_SCHEMA,
)


@pytest.fixture
def linked(tmp_path):
    """The same tickets, plus the queues they are handled by and the link between them."""
    return _linked_project(tmp_path)


def _linked_project(tmp_path: Path, extra: str = ""):
    return _project(
        tmp_path,
        extra=extra,
        specs=(("queue.yaml", QUEUE_SPEC), ("link.yaml", LINK_SPEC)),
        tables=(("support.queues", QUEUE_SCHEMA, QUEUES),),
    )


def _both_embedded(project, types=("Ticket", "Queue")):
    ontology, config, _ = project
    cats = open_catalogs(config)
    provider = LexiconProvider()
    result = EmbedRuntime(
        ontology=ontology, catalogs=cats, provider=provider, targets=types
    ).reconcile()
    assert result.status == APPLIED, result.failures
    return cats, provider


def _via_matcher(provider, catalogs, types=("Ticket", "Queue")) -> Matcher:
    return Matcher(
        provider=provider,
        stores={
            name: VectorStore(catalog=catalogs["local"], object_type=name, key_type="string")
            for name in types
        },
    )


def _hopped(project, object_type, text, via=None, filters=None, catalogs=None):
    ontology, config, _ = project
    types = tuple(
        name
        for name, obj in ontology.object_types.items()
        if obj.semantic_property is not None
    )
    cats, provider = (
        _both_embedded(project, types) if catalogs is None else (catalogs, LexiconProvider())
    )
    resolver = build_resolver(ontology, config, cats)
    return _via_matcher(provider, cats, types).match(
        resolver, object_type, text, filters or {}, via or {}
    )


def test_a_hop_narrows_a_ranking_by_a_linked_object(linked):
    """The query anyone actually has, and the one slices 1–3 could not express: rank by meaning,
    *belonging to* something. The delivery tickets are the best match either way — what the hop
    removes is everything handled by anyone else."""
    everything = _hopped(linked, "Ticket", "the parcel never turned up")
    grace = _hopped(linked, "Ticket", "the parcel never turned up", via={"handledBy": {"owner": "grace"}})
    assert {m.object["ticketId"] for m in grace.matches} == {"t2", "t5"}
    assert len(everything.matches) > len(grace.matches)
    # And it is still a ranking: the nearest of the survivors comes first.
    assert grace.matches[0].object["ticketId"] == "t2"


def test_a_hop_narrows_before_the_ranking_like_every_other_filter(linked):
    """Not a filter over the top k. `t5` is the second-best delivery ticket and it survives the hop,
    which rank-then-filter over a page of two would have thrown away."""
    result = _hopped(linked, "Ticket", "the customer wanted a refund", via={"handledBy": {"owner": "grace"}})
    assert result.matches, "rank-then-filter would have kept nothing handled by grace"
    assert {m.object["ticketId"] for m in result.matches} == {"t2", "t5"}


def test_an_empty_hop_keeps_only_rows_that_have_a_far_object(linked):
    """`{}` is an existence test rather than a no-op — `t3` is in the `accounts` queue and no such
    Queue row exists, so it is the row this narrows away."""
    everything = _hopped(linked, "Ticket", "money")
    existing = _hopped(linked, "Ticket", "money", via={"handledBy": {}})
    assert "t3" in {m.object["ticketId"] for m in everything.matches}
    assert "t3" not in {m.object["ticketId"] for m in existing.matches}


def test_a_to_many_hop_returns_each_near_row_once(linked):
    """The reason it is `IN (SELECT …)` and not a JOIN. `billing` handles both high-severity
    tickets, so a joined hop would hand back the same Queue twice — same score, same object — and
    the page it filled would be smaller than it looked."""
    result = _hopped(linked, "Queue", "refunds and chargebacks", via={"tickets": {"severity": "high"}})
    keys = [m.object["queueId"] for m in result.matches]
    assert keys == ["billing"]
    assert len(keys) == len(set(keys))


def test_a_hop_and_a_filter_are_anded(linked):
    """Two narrowings of different kinds, composed the way everything in this grammar composes."""
    result = _hopped(
        linked,
        "Ticket",
        "the parcel never turned up",
        filters={"severity": "low"},
        via={"handledBy": {"owner": "grace"}},
    )
    assert {m.object["ticketId"] for m in result.matches} == {"t2", "t5"}
    assert all(m.object["severity"] == "low" for m in result.matches)


FAR_GOVERNED = """
governance:
  policies:
    - name: emea-only
      objectType: Queue
      rows: "object.region == 'emea'"
"""


def test_a_policy_on_the_far_type_withholds_rows_through_the_hop(tmp_path):
    """The finding, made checkable. The predicate comes from `Resolver._table` for free; where it is
    *placed* is not free — in the top-level `WHERE` the hop's alias is out of scope and the
    parameters stop lining up with the clauses, so this call would fail rather than narrow."""
    project = _linked_project(tmp_path, extra=FAR_GOVERNED)
    result = _hopped(project, "Ticket", "the parcel never turned up", via={"handledBy": {}})
    # `logistics` is amer, so every ticket it handles is unreachable through this hop.
    assert {m.object["ticketId"] for m in result.matches} == {"t1", "t4"}
    assert "t2" not in {m.object["ticketId"] for m in result.matches}


def test_the_far_policy_governs_the_hop_and_not_the_ranked_type(tmp_path):
    """A policy on `Queue` says nothing about which tickets may be ranked — only about which queues
    a hop may find. Without the hop, every ticket is still there."""
    project = _linked_project(tmp_path, extra=FAR_GOVERNED)
    result = _hopped(project, "Ticket", "the parcel never turned up")
    assert "t2" in {m.object["ticketId"] for m in result.matches}


def test_the_generated_tool_takes_via_and_returns_the_hopped_ranking(linked):
    """Through `build_tools`, so the argument the agent actually sends is the one under test."""
    ontology, config, _ = linked
    cats, provider = _both_embedded(linked)
    tool = _rebuilt_tool(ontology, config, cats, provider)
    out, is_error = _call(
        tool, {"text": "the parcel never turned up", "via": {"handledBy": {"owner": "grace"}}}
    )
    assert is_error is False
    assert [m["object"]["ticketId"] for m in out["matches"]] == ["t2", "t5"]
    assert out["count"] == 2 and out["hasMore"] is False
    assert set(tool.input_schema["properties"]["via"]["properties"]) == {"handledBy"}


def test_loom_query_via_returns_what_the_tool_returns(linked, monkeypatch, capsys):
    """The dev command mirrors the generated tool, `via` included — a cross-object filter the CLI
    could do and the surface could not would be the back door this command exists not to be."""
    import json as _json

    from loom.cli import main

    ontology, config, root = linked
    cats, provider = _both_embedded(linked)
    monkeypatch.setattr("loom.embed.match.provider_for", lambda _config: provider)

    argv = [
        "query", "Ticket", str(root / "ontology"),
        "--match", "the parcel never turned up",
        "--via", "handledBy.owner=grace",
    ]
    assert main(argv) == 0
    rows = _json.loads(capsys.readouterr().out)
    tool_out, _ = _call(
        _rebuilt_tool(ontology, config, cats, provider),
        {"text": "the parcel never turned up", "via": {"handledBy": {"owner": "grace"}}},
    )
    assert [r["object"]["ticketId"] for r in rows] == [
        m["object"]["ticketId"] for m in tool_out["matches"]
    ]


def test_loom_query_refuses_via_without_match(linked, capsys):
    """`via` is an argument of `match_<object>` and of nothing else, so it is refused rather than
    quietly applied to a search the surface offers no hop on."""
    _, _, root = linked
    assert main_argv(["query", "Ticket", str(root / "ontology"), "--via", "handledBy"]) == 1
    assert "--via requires --match" in capsys.readouterr().err


# ---- a hop with a table in the middle ---------------------------------------------
#
# These exist because a live probe reached a case the suite could not: `test_query_compile.py`
# asserts the SQL a many-to-many hop compiles to, and nothing had ever *run* it. The subquery
# changes which column it selects when a mapping table is present — the near row's value lives on
# the mapping's near side, not on the far table's join column — and that is exactly the kind of
# claim a string comparison cannot make.

TAG_SPEC = """
objectType:
  apiName: Tag
  primaryKey: tagId
  backing: { catalog: local, table: support.tags }
  properties:
    - { name: tagId, type: string, column: id, unique: true }
    - { name: label, type: string, column: label }
  searchable: [tagId, label]
  semantic: label
"""

TAGGED_SPEC = """
linkType:
  apiName: tags
  cardinality: many_to_many
  from: { objectType: Ticket, property: ticketId }
  to:   { objectType: Tag,    property: tagId }
  through: { catalog: local, table: support.ticket_tags, fromColumn: ticket_id, toColumn: tag_id }
  reverseName: tickets
"""

TAG_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string(), nullable=False),
        pa.field("label", pa.string(), nullable=False),
    ]
)
TAGS = pa.table(
    {"id": ["urgent", "vip"], "label": ["needs a person today", "top account"]}, schema=TAG_SCHEMA
)

MAP_SCHEMA = pa.schema(
    [
        pa.field("ticket_id", pa.string(), nullable=False),
        pa.field("tag_id", pa.string(), nullable=False),
    ]
)
# `t1` carries both tags, which is the row a JOIN would hand back twice.
TICKET_TAGS = pa.table(
    {"ticket_id": ["t1", "t1", "t4", "t2"], "tag_id": ["urgent", "vip", "vip", "urgent"]},
    schema=MAP_SCHEMA,
)


@pytest.fixture
def tagged(tmp_path):
    return _project(
        tmp_path,
        specs=(
            ("queue.yaml", QUEUE_SPEC),
            ("link.yaml", LINK_SPEC),
            ("tag.yaml", TAG_SPEC),
            ("tagged.yaml", TAGGED_SPEC),
        ),
        tables=(
            ("support.queues", QUEUE_SCHEMA, QUEUES),
            ("support.tags", TAG_SCHEMA, TAGS),
            ("support.ticket_tags", MAP_SCHEMA, TICKET_TAGS),
        ),
    )


def test_a_many_to_many_hop_reaches_through_its_mapping_table(tagged):
    """The mapping table joins inside the subquery and changes what it selects, which is the half
    of the lowering only a running engine can confirm."""
    result = _hopped(tagged, "Ticket", "money", via={"tags": {"label": "top account"}})
    assert {m.object["ticketId"] for m in result.matches} == {"t1", "t4"}


def test_a_many_to_many_hop_returns_each_near_row_once(tagged):
    """`t1` carries both tags, so a joined hop would return it twice — same object, same score, and
    a page smaller than the number on it. The semi-join cannot, whatever the projection holds."""
    result = _hopped(tagged, "Ticket", "money", via={"tags": {}})
    keys = [m.object["ticketId"] for m in result.matches]
    assert sorted(keys) == ["t1", "t2", "t4"]
    assert len(keys) == len(set(keys))


def test_two_hops_of_different_shapes_are_anded(tagged):
    """One hop through a mapping table and one straight across, in a single call: two subqueries,
    two aliases, and a near row has to satisfy both."""
    both = _hopped(
        tagged, "Ticket", "money", via={"tags": {"label": "top account"}, "handledBy": {"owner": "ada"}}
    )
    assert {m.object["ticketId"] for m in both.matches} == {"t1", "t4"}
    # `ada` handles billing and `grace` handles logistics, so the same tag with the other owner is
    # a conjunction nothing satisfies — which is what tells this apart from a hop being ignored.
    neither = _hopped(
        tagged, "Ticket", "money", via={"tags": {"label": "top account"}, "handledBy": {"owner": "grace"}}
    )
    assert neither.matches == ()


def test_a_reverse_many_to_many_hop_swaps_the_mapping_columns(tagged):
    """A hop taken from the link's `to` end, which is the one arm of the through-column swap nothing
    else in the suite reaches — and an inverted swap joins ticket ids to tag ids, which matches
    nothing and returns an empty ranking rather than an error.

    `urgent` tags `t1` (billing) and `t2` (logistics); `vip` tags `t1` and `t4`, both billing. So
    the queue a ticket sits in tells the two tags apart from the far side of the mapping."""
    logistics = _hopped(tagged, "Tag", "needs attention", via={"tickets": {"queue": "logistics"}})
    assert [m.object["tagId"] for m in logistics.matches] == ["urgent"]

    high = _hopped(tagged, "Tag", "needs attention", via={"tickets": {"severity": "high"}})
    # `t1` and `t4` are the high ones and between them carry both tags — once each, which is the
    # no-duplication claim read from the reverse end, where `vip` is reached through two rows.
    assert sorted(m.object["tagId"] for m in high.matches) == ["urgent", "vip"]

    # `t3` is in the accounts queue and carries no tag at all.
    assert _hopped(tagged, "Tag", "needs attention", via={"tickets": {"queue": "accounts"}}).matches == ()


def test_a_reverse_hop_across_a_plain_link_is_the_same_shape(tagged):
    """`tickets` off a Queue is `handledBy` read backwards, so the join columns swap and nothing
    else does — the two-column version of the swap above."""
    result = _hopped(tagged, "Queue", "money paid and refunds", via={"tickets": {"severity": "high"}})
    # Only `billing` handles a high-severity ticket, and it handles two of them.
    assert [m.object["queueId"] for m in result.matches] == ["billing"]
