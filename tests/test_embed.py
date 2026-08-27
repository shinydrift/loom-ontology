"""The embed runtime — against a fake catalog and a fake model.

`test_ingest.py`'s bargain on a third plane: the ports mean the whole reconcile is testable with no
Iceberg stack and no model runtime, and what is asserted here is the **policy** — what counts as
stale, what a model swap does, what happens to text that outlived its row — because that is the part
a real warehouse would only tell us about by ranking somebody's rows against a vector of the wrong
generation.

The fake implements the read port and `VectorWriter`, and deliberately **not** `CatalogWriter`, not
`RowWriter` and not `BulkWriter`. Those three absences are assertions: the runtime is handed this and
works, which means it never reached for a schema verb (embedding never migrates the tables it reads),
never reached for a single-row verb, and never reached for the bulk verbs that take a table name —
which is the whole argument for `VectorWriter` being a port rather than a `BulkWriter` pointed at
`_loom_meta`.

`test_embed_iceberg.py` proves the same sequence against real pyiceberg, including the one thing no
fake can: that a `list<float>` column is DDL Loom can actually create.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from loom import build
from loom.catalog.base import (
    CatalogError,
    Column,
    ConcurrencyError,
    TableSchema,
    bulk_writer_for,
    row_writer_for,
    vector_table,
    vector_writer_for,
    writer_for,
)
from loom.embed import (
    APPLIED,
    CONFLICT,
    EMBED_FAILED,
    FAILED,
    MODEL_CHANGED,
    PREVIEWED,
    REFUSED,
    WRITE_FAILED,
    EmbeddingError,
    EmbedError,
    EmbedRuntime,
    embeddable,
    embedded_as_of,
    source_hash,
    vector_columns,
)
from loom.embed.store import VectorStore

VALID = Path(__file__).parent / "fixtures" / "valid"

CUSTOMERS = [
    {"id": "c1", "full_name": "Ada Lovelace", "tier": "gold"},
    {"id": "c2", "full_name": "Grace Hopper", "tier": "silver"},
]


class FakeVectorCatalog:
    """An in-memory catalog implementing the read port and `VectorWriter`, and nothing else."""

    def __init__(self, rows=None, snapshot=1, fail_on="", ensure_fails=False):
        self.name = "rest_main"
        self.rows: dict[str, list[dict]] = {
            "crm.customers": [dict(r) for r in (rows if rows is not None else CUSTOMERS)],
        }
        self.snapshots = {t: snapshot for t in self.rows}
        self.log: list[tuple] = []
        self.fail_on = fail_on
        self.ensure_fails = ensure_fails
        self.vector_columns: tuple | None = None

    # --- read port
    def table_exists(self, table: str) -> bool:
        return table in self.rows

    def describe(self, table: str) -> TableSchema:
        sample = self.rows[table][0] if self.rows.get(table) else {}
        return TableSchema(table=table, columns={c: Column(c, "string", False) for c in sample})

    def scan(self, table, columns=None, predicates=(), limit=None):
        self.log.append(("scan", table, columns))
        return _FakeArrow(self.rows.get(table, []))

    def current_snapshot_id(self, table: str) -> int | None:
        return self.snapshots.get(table)

    # --- vector port
    def ensure_vectors(self, object_type, columns):
        if self.ensure_fails:
            raise CatalogError("boom: the sidecar cannot be created")
        self.vector_columns = tuple(columns)
        self.rows.setdefault(vector_table(object_type), [])
        self.snapshots.setdefault(vector_table(object_type), None)

    def merge_vectors(self, object_type, columns, rows, *, expect_snapshot_id):
        table = vector_table(object_type)
        self.vector_columns = tuple(columns)
        self.rows.setdefault(table, [])
        self._guard(table, expect_snapshot_id)
        keys = {r["key"] for r in rows}
        kept = [r for r in self.rows[table] if r.get("key") not in keys]
        self.rows[table] = [*kept, *(dict(r) for r in rows)]
        self._bump(table)
        self.log.append(("merge", table, len(rows)))

    def delete_vectors(self, object_type, keys):
        """No `expect_snapshot_id` in the signature at all, which is the port's claim made
        structural: a fake that accepted one could not show that a prune asserts nothing."""
        table = vector_table(object_type)
        if table not in self.rows:
            return
        if self.fail_on == table:
            raise CatalogError(f"boom: {table}")
        dropped = set(keys)
        self.rows[table] = [r for r in self.rows[table] if r.get("key") not in dropped]
        self._bump(table)
        self.log.append(("delete", table, len(keys)))

    def _guard(self, table, expect_snapshot_id):
        if self.fail_on == table:
            raise CatalogError(f"boom: {table}")
        current = self.snapshots.get(table)
        if expect_snapshot_id != current:
            raise ConcurrencyError(
                f"'{table}' moved: expected {expect_snapshot_id}, found {current}",
                table=table, expected=expect_snapshot_id, found=current,
            )

    def _bump(self, table):
        self.snapshots[table] = (self.snapshots.get(table) or 0) + 1

    @property
    def vectors(self):
        return list(self.rows.get(vector_table("Customer"), []))

    @property
    def writes(self):
        return [e for e in self.log if e[0] in ("merge", "delete")]


class _FakeArrow:
    def __init__(self, rows):
        self._rows = rows

    def to_pylist(self):
        return [dict(r) for r in self._rows]


class FakeProvider:
    """A deterministic model: the vector is the text's length and its first three code points.

    Deterministic on purpose, so a test can assert *which* text produced a stored vector rather than
    only that one is there. `calls` records every batch, which is how the batching and the
    "unchanged rows are not re-embedded" claims are checked at all — both are statements about what
    was *not* sent to a model."""

    def __init__(self, dims=4, fails=False, short=False):
        self.model = "fake-v1"
        self._dims = dims
        self.calls: list[list[str]] = []
        self.fails = fails
        self.short = short

    @property
    def dims(self) -> int:
        return self._dims

    def embed(self, texts):
        if self.fails:
            raise EmbeddingError("boom: the model is unreachable")
        self.calls.append(list(texts))
        out = [
            tuple(float(len(t)) for _ in range(1)) + tuple(float(ord(c)) for c in t[:3].ljust(3))
            for t in texts
        ]
        return out[:-1] if self.short else out


def ontology_fixture():
    """The `valid` fixture, with `semantic: name` added.

    Injected here rather than declared in the fixture directory, which is the choice M10's first
    slice already made and for its reason: `fixtures/valid` is shared by two dozen tests and by the
    governance suite that masks `Customer.name`, so a `semantic:` in the file would be a declaration
    those tests have to route around forever to test something else."""
    ont, _ = build(VALID)
    customer = replace(ont.object_types["Customer"], semantic="name")
    return replace(ont, object_types={**ont.object_types, "Customer": customer})


def runtime(ontology, catalog, provider=None):
    return EmbedRuntime(
        ontology=ontology,
        catalogs={"rest_main": catalog},
        provider=provider or FakeProvider(),
        targets=("Customer",),
    )


# ---- the happy path ------------------------------------------------------------


def test_first_reconcile_embeds_every_row_with_text():
    catalog = FakeVectorCatalog()
    provider = FakeProvider()
    result = runtime(ontology_fixture(), catalog, provider).reconcile()

    assert result.status == APPLIED
    assert result.rows_embedded == 2
    assert provider.calls == [["Ada Lovelace", "Grace Hopper"]]
    assert {r["key"] for r in catalog.vectors} == {"c1", "c2"}
    assert all(r["model"] == "fake-v1" and r["dims"] == 4 for r in catalog.vectors)
    assert all(r["property"] == "name" for r in catalog.vectors)


def test_a_second_reconcile_embeds_nothing():
    """The headline claim: staleness is a hash comparison, so an unchanged row costs no model call."""
    catalog = FakeVectorCatalog()
    provider = FakeProvider()
    runtime(ontology_fixture(), catalog, provider).reconcile()
    result = runtime(ontology_fixture(), catalog, provider).reconcile()

    assert result.rows_embedded == 0
    assert result.types[0].rows_current == 2
    assert len(provider.calls) == 1  # the first run's, and nothing since
    assert len(catalog.vectors) == 2


def test_changed_text_is_re_embedded_and_unchanged_text_is_not():
    catalog = FakeVectorCatalog()
    provider = FakeProvider()
    runtime(ontology_fixture(), catalog, provider).reconcile()

    catalog.rows["crm.customers"][0]["full_name"] = "Ada Byron"
    result = runtime(ontology_fixture(), catalog, provider).reconcile()

    assert result.rows_embedded == 1
    assert provider.calls[-1] == ["Ada Byron"]
    stored = {r["key"]: r for r in catalog.vectors}
    assert stored["c1"]["source_hash"] == source_hash("Ada Byron", "fake-v1", 4, "name")


def test_renaming_the_semantic_property_makes_every_row_pending():
    """The one staleness the reconcile could not see, and the one that silently empties `match_`.

    Renaming a property's apiName moves no column and changes no text, so `loom plan` says *No
    changes* and every `source_hash` still matched — the reconcile reported `rowsCurrent: 2,
    rowsEmbedded: 0`. But `ir.VectorRef`'s comparability guard also compares the stored `property`,
    so it withheld both rows and the ranked plane returned nothing at all, permanently. The hash now
    covers the fourth column the guard compares, so a rename invalidates by construction — the same
    way a model swap does, and for the same reason."""
    catalog = FakeVectorCatalog()
    provider = FakeProvider()
    ont = ontology_fixture()
    runtime(ont, catalog, provider).reconcile()
    assert all(r["property"] == "name" for r in catalog.vectors)

    # Same column, same text, same model — only the name the spec calls the property by.
    customer = ont.object_types["Customer"]
    renamed = replace(customer.properties["name"], name="fullName")
    props = {("fullName" if k == "name" else k): (renamed if k == "name" else v)
             for k, v in customer.properties.items()}
    moved = replace(customer, properties=props, semantic="fullName")
    after = replace(ont, object_types={**ont.object_types, "Customer": moved})

    result = runtime(after, catalog, provider).reconcile()
    assert result.rows_embedded == 2
    assert result.types[0].rows_current == 0
    assert all(r["property"] == "fullName" for r in catalog.vectors)
    assert len(catalog.vectors) == 2  # rewritten in place, not left beside the old ones


def test_the_stored_vector_is_the_one_the_model_returned_for_that_row():
    """Positional order is the port's contract, and this is where it is checked end to end."""
    catalog = FakeVectorCatalog()
    runtime(ontology_fixture(), catalog).reconcile()

    stored = {r["key"]: r["vector"] for r in catalog.vectors}
    assert stored["c1"] == [12.0, float(ord("A")), float(ord("d")), float(ord("a"))]
    assert stored["c2"] == [12.0, float(ord("G")), float(ord("r")), float(ord("a"))]


# ---- staleness, and what is not staleness --------------------------------------


def test_a_row_with_no_text_is_counted_apart_and_never_becomes_pending():
    """A null semantic property is the absence of text, not a vector that is behind.

    The distinction has to hold across runs, because a reconcile that counted it as pending would
    embed nothing, report work outstanding, and never converge."""
    catalog = FakeVectorCatalog(
        rows=[{"id": "c1", "full_name": None}, {"id": "c2", "full_name": "   "}]
    )
    provider = FakeProvider()
    result = runtime(ontology_fixture(), catalog, provider).reconcile()

    assert result.rows_embedded == 0
    assert result.types[0].rows_without_text == 2
    assert result.types[0].rows_read == 2
    assert provider.calls == []


def test_text_that_is_blanked_prunes_its_vector_like_a_deleted_row():
    """Blanking the text is an erasure of the embeddable content, and the sidecar follows it.

    The reason the orphan set keys on *rows with text* rather than on *rows*: a customer whose name
    is cleared leaves exactly the recoverable copy a deleted customer does."""
    catalog = FakeVectorCatalog()
    runtime(ontology_fixture(), catalog).reconcile()
    catalog.rows["crm.customers"][0]["full_name"] = None

    result = runtime(ontology_fixture(), catalog).reconcile()

    assert result.rows_pruned == 1
    assert {r["key"] for r in catalog.vectors} == {"c2"}


def test_a_deleted_row_has_its_vector_pruned():
    catalog = FakeVectorCatalog()
    runtime(ontology_fixture(), catalog).reconcile()
    catalog.rows["crm.customers"] = [r for r in catalog.rows["crm.customers"] if r["id"] != "c2"]

    result = runtime(ontology_fixture(), catalog).reconcile()

    assert result.rows_pruned == 1
    assert {r["key"] for r in catalog.vectors} == {"c1"}


def test_the_prune_commits_before_the_merge():
    """Ordering, asserted rather than trusted: an orphan is text with a deadline behind it, so a run
    that fails after the merge must still have removed it."""
    catalog = FakeVectorCatalog()
    runtime(ontology_fixture(), catalog).reconcile()
    catalog.rows["crm.customers"] = [
        {"id": "c1", "full_name": "Ada Byron"},  # changed → pending
    ]  # c2 gone → orphan

    catalog.log.clear()
    runtime(ontology_fixture(), catalog).reconcile()

    assert [e[0] for e in catalog.writes] == ["delete", "merge"]


def test_a_row_with_no_key_is_counted_and_skipped():
    catalog = FakeVectorCatalog(
        rows=[{"id": None, "full_name": "Nobody"}, {"id": "c1", "full_name": "Ada Lovelace"}]
    )
    result = runtime(ontology_fixture(), catalog).reconcile()

    assert result.types[0].rows_unkeyed == 1
    assert result.rows_embedded == 1


# ---- the model swap ------------------------------------------------------------


def test_a_different_model_is_refused_and_names_the_flag():
    catalog = FakeVectorCatalog()
    runtime(ontology_fixture(), catalog, FakeProvider()).reconcile()

    other = FakeProvider()
    other.model = "fake-v2"
    result = runtime(ontology_fixture(), catalog, other).reconcile()

    assert result.status == REFUSED
    assert result.failures[0].code == MODEL_CHANGED
    assert "--remodel" in result.failures[0].message
    assert "fake-v1" in result.failures[0].message


def test_a_refused_model_swap_writes_nothing():
    catalog = FakeVectorCatalog()
    runtime(ontology_fixture(), catalog, FakeProvider()).reconcile()
    before = [dict(r) for r in catalog.vectors]

    other = FakeProvider()
    other.model = "fake-v2"
    catalog.log.clear()
    runtime(ontology_fixture(), catalog, other).reconcile()

    assert catalog.writes == []
    assert catalog.vectors == before


def test_remodel_re_embeds_everything():
    catalog = FakeVectorCatalog()
    runtime(ontology_fixture(), catalog, FakeProvider()).reconcile()

    other = FakeProvider()
    other.model = "fake-v2"
    result = runtime(ontology_fixture(), catalog, other).reconcile(remodel=True)

    assert result.status == APPLIED
    assert result.rows_embedded == 2
    assert {r["model"] for r in catalog.vectors} == {"fake-v2"}


def test_a_whole_table_of_edits_under_the_same_model_is_not_a_model_swap():
    """The reason `model` is a column rather than an inference from *every hash mismatching*.

    Two rows, both legitimately edited between reconciles — indistinguishable from a model swap to
    anything reading only the hashes, and trivially distinguishable to something reading the fact."""
    catalog = FakeVectorCatalog()
    runtime(ontology_fixture(), catalog).reconcile()
    for row in catalog.rows["crm.customers"]:
        row["full_name"] = row["full_name"].upper()

    result = runtime(ontology_fixture(), catalog).reconcile()

    assert result.status == APPLIED
    assert result.rows_embedded == 2


# ---- dry run -------------------------------------------------------------------


def test_a_dry_run_reports_the_work_and_writes_nothing():
    catalog = FakeVectorCatalog()
    provider = FakeProvider()
    result = runtime(ontology_fixture(), catalog, provider).reconcile(dry_run=True)

    assert result.status == PREVIEWED
    assert result.rows_embedded == 2
    assert catalog.writes == []
    assert provider.calls == []  # `dims` is a property here; a real provider probes once
    assert catalog.vectors == []


# ---- failure ------------------------------------------------------------------


def test_a_model_failure_is_reported_rather_than_raised():
    catalog = FakeVectorCatalog()
    result = runtime(ontology_fixture(), catalog, FakeProvider(fails=True)).reconcile()

    assert result.status == FAILED
    assert result.failures[0].code == EMBED_FAILED
    assert catalog.vectors == []


def test_a_short_batch_from_a_provider_is_refused_rather_than_matched_up():
    """The silent failure this exists to prevent: every row getting its neighbour's meaning."""
    catalog = FakeVectorCatalog()
    result = runtime(ontology_fixture(), catalog, FakeProvider(short=True)).reconcile()

    assert result.status == FAILED
    assert catalog.vectors == []


def test_a_catalog_failure_is_reported_rather_than_raised():
    catalog = FakeVectorCatalog(fail_on=vector_table("Customer"))
    result = runtime(ontology_fixture(), catalog).reconcile()

    assert result.status == FAILED
    assert result.failures[0].code == WRITE_FAILED


def test_a_sidecar_that_moved_is_a_conflict_rather_than_a_broken_catalog():
    """A second reconcile running is a different answer from an unreachable metastore, and
    `ConcurrencyError` subclasses `CatalogError`, so the order of the two handlers is the check."""
    catalog = FakeVectorCatalog()
    runtime(ontology_fixture(), catalog).reconcile()
    catalog.rows["crm.customers"][0]["full_name"] = "Ada Byron"
    catalog.snapshots[vector_table("Customer")] = 99  # somebody else committed

    original = catalog.current_snapshot_id
    catalog.current_snapshot_id = lambda t: 1 if t.startswith("_loom_meta") else original(t)
    result = runtime(ontology_fixture(), catalog).reconcile()

    assert result.status == REFUSED
    assert result.failures[0].code == CONFLICT


# ---- what the runtime may not reach --------------------------------------------


def test_the_fake_implements_the_vector_port_and_none_of_the_write_ports():
    """The absences are the assertion. A reconcile runs against this, so it never reached for a
    schema verb, a single-row verb, or the bulk verbs that take a table name."""
    catalog = FakeVectorCatalog()
    assert vector_writer_for(catalog) is catalog
    for asking in (writer_for, row_writer_for, bulk_writer_for):
        with pytest.raises(CatalogError):
            asking(catalog)


def test_a_narrowed_run_visits_one_type():
    catalog = FakeVectorCatalog()
    result = runtime(ontology_fixture(), catalog).reconcile("Customer")
    assert [t.object_type for t in result.types] == ["Customer"]


def test_an_undeclared_type_is_a_malformed_command():
    catalog = FakeVectorCatalog()
    with pytest.raises(EmbedError, match="not declared"):
        runtime(ontology_fixture(), catalog).reconcile("Nope")


def test_a_type_without_semantic_is_a_malformed_command():
    catalog = FakeVectorCatalog()
    with pytest.raises(EmbedError, match="no 'semantic:'"):
        runtime(ontology_fixture(), catalog).reconcile("Order")


# ---- the store's own vocabulary -------------------------------------------------


def test_source_hash_covers_the_model_the_width_and_the_property():
    """The four things `ir.VectorRef`'s comparability guard compares — all four, or the guard can
    refuse a row this hash calls current. The property was the one left out: a `semantic:` rename
    keeps the text, so without it every hash matched, `reconcile` reported nothing to do, and
    `match_` ranked nothing at all."""
    assert source_hash("x", "a", 4, "p") != source_hash("x", "b", 4, "p")
    assert source_hash("x", "a", 4, "p") != source_hash("x", "a", 8, "p")
    assert source_hash("x", "a", 4, "p") != source_hash("x", "a", 4, "q")
    assert source_hash("x", "a", 4, "p") == source_hash("x", "a", 4, "p")


def test_source_hash_cannot_be_impersonated_across_its_parts():
    """Length-prefixed rather than joined, so no text can spell a different (text, model) pair."""
    assert source_hash("ab", "c", 1, "p") != source_hash("a", "bc", 1, "p")


def test_embeddable_treats_blank_and_null_alike_and_refuses_a_non_string():
    assert embeddable("  hi  ") == "hi"
    assert embeddable("") is None
    assert embeddable(None) is None
    assert embeddable(42) is None


def test_embedded_as_of_is_the_oldest_stamp():
    """*Every vector here is at least this current* — the newest would let one fresh row describe a
    table last reconciled in March."""
    old = datetime(2026, 3, 1, tzinfo=UTC)
    new = old + timedelta(days=90)
    assert embedded_as_of({"a": {"embedded_at": new}, "b": {"embedded_at": old}}) == old
    assert embedded_as_of({}) is None


def test_the_key_column_takes_the_object_s_own_primary_key_type():
    """Why the sidecar is per type: a `long` key stays a `long` and needs no cast on the join."""
    assert vector_columns("long")[0].iceberg_type == "long"
    assert vector_columns("string")[0].iceberg_type == "string"
    assert [c.name for c in vector_columns("string")][:2] == ["key", "property"]


def test_the_store_reads_hashes_without_reading_vectors():
    """The projection is the point: deciding what needs embedding never loads a vector."""
    catalog = FakeVectorCatalog()
    runtime(ontology_fixture(), catalog).reconcile()

    catalog.log.clear()
    store = VectorStore(catalog=catalog, object_type="Customer", key_type="string")
    store.existing()
    scanned = [e for e in catalog.log if e[0] == "scan"]
    assert "vector" not in scanned[0][2]


# ---- the provider port ----------------------------------------------------------


def test_provider_for_routes_on_the_configured_provider():
    from loom.config import EmbeddingConfig
    from loom.embed import LocalProvider, OpenAIProvider, provider_for

    assert isinstance(provider_for(EmbeddingConfig(provider="local", model="m")), LocalProvider)
    assert isinstance(provider_for(EmbeddingConfig(provider="openai", model="m")), OpenAIProvider)


def test_constructing_a_provider_loads_no_model():
    """`build_embedder` pairs a spec with a deployment without a network, which is `_parse_auth`'s
    two-phase split one layer further along: the file is checked here, the machine is checked on
    first use."""
    from loom.config import EmbeddingConfig
    from loom.embed import provider_for

    provider = provider_for(EmbeddingConfig(provider="local", model="not-a-real-model"))
    assert provider.model == "not-a-real-model"  # no import, no download, no error


def test_the_local_provider_names_the_extra_when_fastembed_is_absent(monkeypatch):
    import sys

    from loom.embed import DEFAULT_LOCAL_MODEL, LocalProvider

    monkeypatch.setitem(sys.modules, "fastembed", None)
    with pytest.raises(EmbeddingError, match=r"loom-ontology\[embed\]"):
        LocalProvider(model=DEFAULT_LOCAL_MODEL).embed(["hi"])


def test_the_openai_provider_refuses_without_a_key_rather_than_reading_one_from_the_config(
    monkeypatch,
):
    """A `loom.yaml` is reviewed in a repository, so a key in one is a key in a diff. `mcp.auth`
    makes the same call from the other direction — it names an issuer and never a secret."""
    from loom.embed import OpenAIProvider

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(EmbeddingError, match="OPENAI_API_KEY"):
        OpenAIProvider(model="text-embedding-3-small").embed(["hi"])


def test_the_openai_provider_reorders_by_index(monkeypatch):
    """The API documents that embeddings may come back out of order, and the port's contract is
    positional. Without this sort every row quietly gets its neighbour's meaning."""
    httpx2 = pytest.importorskip("httpx2", reason="needs the [embed] extra")

    from loom.embed import OpenAIProvider

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    payload = {"data": [
        {"index": 1, "embedding": [2.0]},
        {"index": 0, "embedding": [1.0]},
    ]}
    monkeypatch.setattr(
        httpx2, "post", lambda *a, **k: httpx2.Response(200, json=payload, request=httpx2.Request("POST", "http://x"))
    )
    assert OpenAIProvider(model="m").embed(["first", "second"]) == ((1.0,), (2.0,))


def test_the_openai_provider_refuses_a_short_batch(monkeypatch):
    httpx2 = pytest.importorskip("httpx2", reason="needs the [embed] extra")

    from loom.embed import OpenAIProvider

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    payload = {"data": [{"index": 0, "embedding": [1.0]}]}
    monkeypatch.setattr(
        httpx2, "post", lambda *a, **k: httpx2.Response(200, json=payload, request=httpx2.Request("POST", "http://x"))
    )
    with pytest.raises(EmbeddingError, match="positional"):
        OpenAIProvider(model="m").embed(["first", "second"])


def test_an_empty_batch_reaches_no_model():
    from loom.embed import LocalProvider, OpenAIProvider

    assert LocalProvider(model="whatever").embed([]) == ()
    assert OpenAIProvider(model="whatever").embed([]) == ()


# ---- the erasure obligation ------------------------------------------------------


class FakeActionCatalog(FakeVectorCatalog):
    """The vector fake, plus the two ports the action runtime asks for.

    Combined here rather than in `test_action.py` because what is under test is the *pairing* — an
    action that deletes a row and a sidecar that holds a copy of that row's text — and that pairing
    is this module's subject."""

    def __init__(self, prune_fails=False, **kwargs):
        super().__init__(**kwargs)
        self.prune_fails = prune_fails
        self.edits: list[dict] = []

    def describe(self, table):
        sample = self.rows[table][0] if self.rows.get(table) else {}
        return TableSchema(table=table, columns={c: Column(c, "string", False) for c in sample})

    # --- row-write port
    def delete_row(self, table, key_column, key_value, *, expect_snapshot_id, commit_properties):
        self._guard(table, expect_snapshot_id)
        self.rows[table] = [r for r in self.rows[table] if r.get(key_column) != key_value]
        self._bump(table)
        self.log.append(("delete_row", table, key_value))

    def insert_row(self, table, row, *, expect_snapshot_id, commit_properties):
        raise AssertionError("this suite only deletes")

    def replace_row(self, table, key_column, key_value, row, *, expect_snapshot_id,
                    commit_properties):
        raise AssertionError("this suite only deletes")

    # --- edit-log port
    def ensure_log(self, columns):
        pass

    def append_edit(self, columns, row):
        self.edits.append(dict(row))

    # --- vector port, with a failure switch
    def delete_vectors(self, object_type, keys):
        if self.prune_fails:
            raise CatalogError("boom: the sidecar cannot be written")
        super().delete_vectors(object_type, keys)


def _action_runtime(catalog):
    from loom.action import ActionRuntime

    return ActionRuntime(ontology=ontology_fixture(), catalogs={"rest_main": catalog})


def test_a_delete_action_prunes_the_vector_before_it_deletes_the_row():
    """Ordering, and it is the whole of what "fails if it cannot" means: refusing *after* the row is
    gone would leave nothing to refuse."""
    catalog = FakeActionCatalog()
    runtime(ontology_fixture(), catalog).reconcile()

    catalog.log.clear()
    result = _action_runtime(catalog).run("forgetCustomer", {"customer": "c1"})

    assert result.status == APPLIED
    assert [e[0] for e in catalog.log if e[0] in ("delete", "delete_row")] == ["delete", "delete_row"]
    assert {r["key"] for r in catalog.vectors} == {"c2"}


def test_a_delete_action_that_cannot_prune_refuses_and_leaves_the_row():
    """The asymmetry stated: a failed embed leaves a row briefly missing from search, and a failed
    vector delete leaves personal data outliving the request that erased it. Only the second refuses.
    """
    catalog = FakeActionCatalog()
    runtime(ontology_fixture(), catalog).reconcile()
    catalog.prune_fails = True

    result = _action_runtime(catalog).run("forgetCustomer", {"customer": "c1"})

    assert not result.ok
    assert [f.code for f in result.failures] == ["write_failed"]
    # The row is still there, which is the point: the erasure did not half-happen.
    assert "c1" in {r["id"] for r in catalog.rows["crm.customers"]}
    assert {r["key"] for r in catalog.vectors} == {"c1", "c2"}


def test_a_delete_action_on_a_type_without_semantic_reaches_for_no_vector_port():
    """Most deployments have no sidecar at all, and a delete must not need one to work."""
    from dataclasses import replace as _replace

    from loom.action import ActionRuntime

    plain = build(VALID)[0]  # no `semantic:` anywhere
    catalog = FakeActionCatalog()
    catalog.delete_vectors = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("the vector port was asked for")
    )

    result = ActionRuntime(ontology=plain, catalogs={"rest_main": catalog}).run(
        "forgetCustomer", {"customer": "c1"}
    )

    assert result.status == APPLIED
    assert _replace(plain.object_types["Customer"]).semantic is None


def test_the_reconcile_reports_the_freshness_it_found_rather_than_the_one_it_leaves():
    """*Every vector here is at least this current*, and it is the **before** figure deliberately —
    after a run everything is current by construction, which would always say `now` and so say
    nothing."""
    catalog = FakeVectorCatalog()
    first = runtime(ontology_fixture(), catalog).reconcile()
    assert first.types[0].embedded_as_of is None  # nothing was there to be as-of

    second = runtime(ontology_fixture(), catalog).reconcile()
    assert second.types[0].embedded_as_of is not None
    assert second.types[0].embedded_as_of == min(r["embedded_at"] for r in catalog.vectors)


# ---- what the review found -------------------------------------------------------


def test_a_duplicated_primary_key_gets_no_vector_and_is_counted():
    """Either row's text would be a wrong answer, so neither is stored.

    A last-one-wins map would make the stored vector a function of file layout, flip on the next
    compaction, and rank a row by its twin's text. `loom ingest` in `append` mode can produce this,
    so it is reachable rather than hypothetical."""
    catalog = FakeVectorCatalog(rows=[
        {"id": "c1", "full_name": "Ada Lovelace"},
        {"id": "c1", "full_name": "Ada Byron"},
        {"id": "c2", "full_name": "Grace Hopper"},
    ])
    result = runtime(ontology_fixture(), catalog).reconcile()

    assert result.types[0].rows_ambiguous == 1
    assert result.rows_embedded == 1
    assert {r["key"] for r in catalog.vectors} == {"c2"}


def test_a_failure_part_way_through_reports_the_work_that_committed():
    """Zeroes on a run that pruned would suppress the CLI's erasure note — the one line an operator
    most needs to have seen."""
    catalog = FakeVectorCatalog()
    runtime(ontology_fixture(), catalog).reconcile()
    catalog.rows["crm.customers"] = [{"id": "c3", "full_name": "Alan Turing"}]  # c1, c2 orphaned

    result = runtime(ontology_fixture(), catalog, FakeProvider(fails=True)).reconcile()

    assert result.status == FAILED
    assert result.rows_pruned == 2  # the prune landed before the model was reached
    assert result.rows_embedded == 0


def test_a_model_swap_is_refused_before_any_type_is_written():
    """A refusal is whole-run because the operator made one decision, so *nothing has been written*
    has to be true even when the swapped type is not the first one visited."""
    catalog = FakeVectorCatalog()
    ont = ontology_fixture()
    two = EmbedRuntime(
        ontology=ont, catalogs={"rest_main": catalog}, provider=FakeProvider(),
        targets=("Customer",),
    )
    two.reconcile()

    other = FakeProvider()
    other.model = "fake-v2"
    catalog.log.clear()
    result = EmbedRuntime(
        ontology=ont, catalogs={"rest_main": catalog}, provider=other, targets=("Customer",),
    ).reconcile()

    assert result.status == REFUSED
    assert "Nothing has been written" in result.failures[0].message
    assert catalog.writes == []


def test_the_openai_provider_turns_a_wrong_shaped_response_into_a_failure():
    """A right-length, wrong-shape response is a failed embed rather than a traceback."""
    httpx2 = pytest.importorskip("httpx2", reason="needs the [embed] extra")

    from loom.embed import OpenAIProvider

    payload = {"data": [{"index": 0, "no_embedding_here": True}]}
    with pytest.MonkeyPatch.context() as m:
        m.setenv("OPENAI_API_KEY", "sk-test")
        m.setattr(httpx2, "post", lambda *a, **k: httpx2.Response(
            200, json=payload, request=httpx2.Request("POST", "http://x")))
        with pytest.raises(EmbeddingError, match="not embeddings"):
            OpenAIProvider(model="m").embed(["one"])
