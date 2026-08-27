"""The vector sidecar against a real Iceberg catalog — the parts a fake cannot prove.

`test_embed.py` proves the *policy* against a fake. This proves the four things only pyiceberg can
answer:

- that `list<float>` is DDL Loom can actually create, with a nested field id that does not collide
  with a top-level one — the one genuinely new thing in the catalog layer this milestone, and the
  only column Loom has ever created that the type system has no name for;
- that a vector survives the round trip as a vector, rather than as a string or a null, which is
  what `_batch` building against the table's own schema is supposed to buy;
- that `merge_vectors` really is an equality-delete plus an append in one commit, so a re-embedded
  row is replaced rather than duplicated;
- that `delete_vectors` really removes the row, which is the claim the erasure path rests on and
  the one that would be worst to be wrong about.

It runs the shipped example, seeded but **never applied** — `test_ingest_iceberg.py`'s starting
point, for its reason: a lake Loom is a guest in is exactly where a sidecar in Loom's own namespace
has to prove it keeps to itself.
"""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from loom import build
from loom.catalog import open_catalogs
from loom.catalog.base import bulk_writer_for, vector_table, vector_writer_for
from loom.embed import APPLIED, EmbedRuntime, source_hash
from loom.embed.store import VectorStore

pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
pytest.importorskip("pyarrow", reason="needs the [iceberg] extra")

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "retail"


@pytest.fixture
def seeded(tmp_path):
    """A seeded copy of the example: real Iceberg tables with rows in them, no `loom apply`."""
    target = tmp_path / "retail"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns(".warehouse"))
    spec = importlib.util.spec_from_file_location("embed_seed", target / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.seed(target)

    from loom.config import find_config, load_config
    from loom.errors import Diagnostics

    diag = Diagnostics()
    config = load_config(find_config(target / "ontology"), diag)
    ontology, _ = build(target / "ontology")
    diag.raise_if_errors()

    # `semantic: name` injected rather than declared in the example, for `test_embed.py`'s reason —
    # the example is a published artifact that four other suites read, and a property declared for
    # one test's benefit is one every other test has to route around.
    customer = replace(ontology.object_types["Customer"], semantic="name")
    ontology = replace(ontology, object_types={**ontology.object_types, "Customer": customer})
    return target, ontology, config


class StubProvider:
    """Three floats per text, derived from it. Real vectors are 384 wide; three is enough to prove
    the column holds a list of floats, and small enough that a failure is readable."""

    model = "stub-v1"
    dims = 3

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [(float(len(t)), float(ord(t[0])), float(ord(t[-1]))) for t in texts]


def runtime(seeded, provider=None):
    _, ontology, config = seeded
    return EmbedRuntime(
        ontology=ontology,
        catalogs=open_catalogs(config),
        provider=provider or StubProvider(),
        targets=("Customer",),
    )


def sidecar(seeded):
    _, ontology, config = seeded
    catalog = open_catalogs(config)["local"]
    return VectorStore(
        catalog=catalog,
        object_type="Customer",
        key_type="string",
        writer=vector_writer_for(catalog),
    )


def stored(seeded):
    _, _, config = seeded
    catalog = open_catalogs(config)["local"]
    rows = catalog.scan(vector_table("Customer")).to_pylist()
    return {r["key"]: r for r in rows}


def test_the_sidecar_is_created_with_a_real_list_column(seeded):
    """The new DDL, end to end: a column whose type `ALL_KINDS` cannot spell."""
    result = runtime(seeded).reconcile()
    assert result.status == APPLIED

    _, _, config = seeded
    catalog = open_catalogs(config)["local"]
    schema = catalog.describe(vector_table("Customer"))
    assert schema.columns["vector"].iceberg_type == "list<float>"
    assert schema.columns["key"].iceberg_type == "string"
    assert schema.columns["key"].required


def test_the_nested_field_id_does_not_collide_with_a_top_level_one(seeded):
    """Iceberg numbers nested fields out of the same space, so a colliding element id is a corrupt
    schema rather than an error — which is why the ids are allocated past the last column."""
    runtime(seeded).reconcile()
    _, _, config = seeded
    table = open_catalogs(config)["local"]._load(vector_table("Customer"))

    ids = [f.field_id for f in table.schema().fields]
    element = table.schema().find_field("vector").field_type.element_id
    assert len(set(ids)) == len(ids)
    assert element not in ids


def test_a_vector_survives_the_round_trip_as_a_vector(seeded):
    runtime(seeded).reconcile()
    rows = stored(seeded)

    assert rows, "the seed has customers, so the sidecar should not be empty"
    for row in rows.values():
        assert isinstance(row["vector"], list)
        assert len(row["vector"]) == 3
        assert all(isinstance(v, float) for v in row["vector"])
        assert row["model"] == "stub-v1"
        assert row["dims"] == 3
        assert row["property"] == "name"
        assert row["embedded_at"] is not None


def test_the_stored_hash_is_the_one_the_runtime_would_recompute(seeded):
    """What makes the second run a no-op is that this equality holds through Iceberg's round trip."""
    runtime(seeded).reconcile()
    _, ontology, config = seeded
    catalog = open_catalogs(config)["local"]
    obj = ontology.object_types["Customer"]
    names = {
        r["id"]: r["full_name"]
        for r in catalog.scan(obj.backing_table, columns=("id", "full_name")).to_pylist()
    }

    for key, row in stored(seeded).items():
        assert row["source_hash"] == source_hash(names[key].strip(), "stub-v1", 3, "name")


def test_re_embedding_replaces_a_row_rather_than_duplicating_it(seeded):
    """`merge_vectors` is an equality-delete plus an append in one commit — the claim made structural.

    An upsert that appended without deleting would leave two rows for one key, both plausible, and a
    ranked query would then return the row twice at two different scores. It is the failure the
    `key` column and `_keys_filter` exist to prevent, and it is invisible until somebody ranks."""
    runtime(seeded).reconcile()
    before = stored(seeded)
    assert len(before) >= 2

    _, ontology, config = seeded
    catalog = open_catalogs(config)["local"]
    obj = ontology.object_types["Customer"]
    rows = catalog.scan(obj.backing_table).to_pylist()
    changed = rows[0]["id"]
    rows[0]["full_name"] = "Someone Else Entirely"
    bulk_writer_for(catalog).replace_table(
        obj.backing_table,
        rows,
        expect_snapshot_id=catalog.current_snapshot_id(obj.backing_table),
        commit_properties={},
    )

    result = runtime(seeded).reconcile()

    assert result.rows_embedded == 1
    after = stored(seeded)
    assert len(after) == len(before)  # replaced, not appended beside
    assert after[changed]["vector"] != before[changed]["vector"]
    assert after[changed]["source_hash"] == source_hash(
        "Someone Else Entirely", "stub-v1", 3, "name"
    )


def test_remodel_alone_re_embeds_nothing_because_the_model_is_in_the_hash(seeded):
    """`--remodel` permits a re-embed; it does not cause one.

    Worth pinning down, because the flag reads like a rebuild switch and is not one. The model is
    folded into `source_hash`, so a genuine model change invalidates every row *by construction* and
    the flag only stops the refusal in front of it. Passed against the same model, the hashes still
    match and the honest amount of work is none."""
    runtime(seeded).reconcile()
    provider = StubProvider()

    result = runtime(seeded, provider).reconcile(remodel=True)

    assert result.rows_embedded == 0
    assert provider.calls == []


def test_a_second_reconcile_calls_no_model_and_writes_no_snapshot(seeded):
    """Staleness is a hash comparison, proved where the hashes have actually been through Parquet."""
    runtime(seeded).reconcile()
    _, _, config = seeded
    catalog = open_catalogs(config)["local"]
    snapshot = catalog.current_snapshot_id(vector_table("Customer"))

    provider = StubProvider()
    result = runtime(seeded, provider).reconcile()

    assert result.rows_embedded == 0
    assert provider.calls == []
    assert catalog.current_snapshot_id(vector_table("Customer")) == snapshot


def test_delete_vectors_really_removes_the_row(seeded):
    """The erasure claim. Everything else in this milestone is recoverable if it is wrong; this is
    the one that would leave derived personal text in a warehouse after somebody asked for it to go."""
    runtime(seeded).reconcile()
    store = sidecar(seeded)
    keys = list(stored(seeded))
    assert len(keys) >= 2

    store.prune([keys[0]])

    after = stored(seeded)
    assert keys[0] not in after
    assert keys[1] in after


def test_an_orphaned_vector_is_pruned_by_the_next_reconcile(seeded):
    """The path the erasure slice inherits: a row that stops being embeddable loses its vector."""
    runtime(seeded).reconcile()
    _, ontology, config = seeded
    catalog = open_catalogs(config)["local"]
    obj = ontology.object_types["Customer"]

    rows = catalog.scan(obj.backing_table).to_pylist()
    gone = rows[0]["id"]
    bulk_writer_for(catalog).replace_table(
        obj.backing_table,
        rows[1:],
        expect_snapshot_id=catalog.current_snapshot_id(obj.backing_table),
        commit_properties={},
    )

    result = runtime(seeded).reconcile()

    assert result.rows_pruned == 1
    assert gone not in stored(seeded)


def test_the_sidecar_is_the_only_table_the_reconcile_created(seeded):
    """`_loom_meta` stays Loom's, and the ontology's tables stay untouched — the migrate layer's
    posture one level down, checked rather than asserted in a docstring."""
    _, ontology, config = seeded
    catalog = open_catalogs(config)["local"]
    obj = ontology.object_types["Customer"]
    before = catalog.current_snapshot_id(obj.backing_table)

    runtime(seeded).reconcile()

    assert catalog.current_snapshot_id(obj.backing_table) == before
    assert catalog.table_exists(vector_table("Customer"))
    assert not catalog.table_exists(vector_table("Order"))


def test_a_delete_action_prunes_the_row_s_vector_in_the_same_breath(seeded):
    """The erasure obligation M10 owes, end to end against real Iceberg.

    `loom embed`'s orphan prune is the *general* vector erasure path and its lag is the interval
    between runs. That lag is fine for a row that was deleted for ordinary reasons and is not fine
    for one deleted *because somebody asked to be forgotten*, so a `delete` action does not wait for
    it."""
    from loom.action import build_runtime

    runtime(seeded).reconcile()
    before = stored(seeded)
    gone = sorted(before)[0]

    _, ontology, config = seeded
    result = build_runtime(ontology, config, open_catalogs(config)).run(
        "forgetCustomer", {"customer": gone}
    )

    assert result.status == APPLIED
    after = stored(seeded)
    assert gone not in after
    assert len(after) == len(before) - 1


def test_an_unreachable_catalog_makes_a_prune_fail_rather_than_quietly_succeed(seeded, monkeypatch):
    """The read port's `table_exists` swallows every exception to answer False, which is right for a
    probe and catastrophic for an erasure: an unreachable metastore would read as *no sidecar*, the
    prune would return quietly, and the delete action would report `applied` over a vector that is
    still there. This verb asks the implementation directly, so it cannot."""
    from loom.catalog.base import CatalogError, vector_writer_for

    runtime(seeded).reconcile()
    _, _, config = seeded
    catalog = open_catalogs(config)["local"]

    def unreachable(_table):
        raise RuntimeError("metastore is down")

    monkeypatch.setattr(catalog._impl, "table_exists", unreachable)
    with pytest.raises(CatalogError, match="cannot be shown to be gone"):
        vector_writer_for(catalog).delete_vectors("Customer", ["c1"])
