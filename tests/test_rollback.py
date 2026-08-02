"""`loom rollback` — against the fake catalog, where the policy is what's on trial.

The same bargain as test_apply.py. What's asserted here is what rollback *decides*: that a rename
comes back as a rename rather than an add-and-strand, that a rolled-back add is left live and said
out loud, that reversing a promotion is refused whole — and that a refused rollback leaves the
working tree as untouched as it leaves the lake. `test_rollback_iceberg.py` runs the same sequence
against real pyiceberg, where the field ids and the rows can actually be checked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.catalog.base import Column
from loom.errors import Diagnostics
from loom.migrate import (
    APPLIED,
    REFUSED,
    UP_TO_DATE,
    MetaStore,
    RollbackError,
    apply_plan,
    desired_tables,
    diff_ontology,
    file_changes,
    latest_version,
    left_behind,
    materialize,
    render_rollback,
    resolve_target,
    restore_files,
    snapshot_spec,
)
from loom.migrate.meta import META_TABLE, STATUS_PARTIAL
from loom.ontology import build
from test_apply import FakeWritableCatalog

WIDGET = """
objectType:
  apiName: Widget
  primaryKey: id
  title: id
  backing: {{ catalog: rest_main, table: demo.widgets }}
  properties:
    - {{ name: id, type: string, column: id, unique: true }}
{extra}
"""


def _spec(root: Path, extra: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "widget.yaml").write_text(WIDGET.format(extra=extra))
    return root


def _apply(root: Path, catalogs, **kwargs):
    """One turn of the ordinary loop — edit the spec, plan it, apply it — so a test can build up a
    history the way a project would rather than by hand-writing `_loom_meta` rows."""
    diag = Diagnostics()
    ontology, _ = build(root)
    plan = diff_ontology(ontology, catalogs, diag)
    assert diag.errors == [], [e.render() for e in diag.errors]
    return apply_plan(plan, catalogs, snapshot_spec(root), **kwargs)


def _rollback(root: Path, catalogs, version=None, *, execute=True):
    """What `cmd_rollback` does, minus the printing and the prompt: resolve, re-plan against a
    materialized copy of the recorded spec, execute, then write the files."""
    history = {name: MetaStore(c).history() for name, c in catalogs.items()}
    target = resolve_target(history, version)
    current = resolve_target(history, latest_version(history))

    diag = Diagnostics()
    tmp = root.parent / "_materialized"
    restored, _ = build(materialize(target.snapshot, tmp / "restored"))
    recorded, _ = build(materialize(current.snapshot, tmp / "recorded"))
    plan = diff_ontology(restored, catalogs, diag, renames=target.renames)
    left = left_behind(plan, desired_tables(recorded, diag), desired_tables(restored, diag), catalogs)
    changes = file_changes(root, target.snapshot)

    result = apply_plan(plan, catalogs, target.snapshot, rollback_of=target.version) if execute else None
    if execute and result.status != REFUSED:
        restore_files(root, target.snapshot, changes)
    return target, plan, left, changes, result, diag


LTV = "    - { name: ltv, type: double, column: ltv_usd, nullable: true }"
LTV_RENAMED = "    - { name: ltv, type: double, column: lifetime_value, nullable: true, renamedFrom: ltv_usd }"
REGION = "    - { name: region, type: string, column: region, nullable: true }"

GADGET = """
objectType:
  apiName: Gadget
  primaryKey: id
  title: id
  backing: {{ catalog: {catalog}, table: demo.gadgets }}
  properties:
    - {{ name: id, type: string, column: id, unique: true }}
"""


def _fresh(tmp_path, extra: str):
    """A spec applied against an empty lake — version 1, and a history to roll back into."""
    root = _spec(tmp_path / "ontology", extra)
    catalog = FakeWritableCatalog(tables={})
    catalogs = {catalog.name: catalog}
    assert _apply(root, catalogs).status == APPLIED
    return root, catalog, catalogs


@pytest.fixture
def project(tmp_path):
    return _fresh(tmp_path, LTV)


# --- what it restores ------------------------------------------------------------------------


def test_a_rename_comes_back_as_a_rename_not_an_add(project):
    """The case that doesn't come free. The restored spec says `column: ltv_usd` and nothing more —
    `renamedFrom` points forward, so it cannot name the column it has to be renamed back from. Left
    to a plain re-plan this adds `ltv_usd` beside a full `lifetime_value` and strands it, which is
    the exact failure `renamedFrom` exists to prevent."""
    root, catalog, catalogs = project
    _spec(root, LTV_RENAMED)
    assert _apply(root, catalogs).status == APPLIED
    assert "lifetime_value" in catalog.tables["demo.widgets"]

    target, plan, _, _, result, _ = _rollback(root, catalogs, version=1)

    assert target.renames == {("rest_main", "demo.widgets"): {"ltv_usd": "lifetime_value"}}
    assert result.status == APPLIED, result.error
    assert [c.kind for t in plan.changes for c in t.columns] == ["rename"]
    assert [entry for entry in catalog.log if entry[0] == "alter"][-1] == (
        "alter",
        "demo.widgets",
        (("rename", "ltv_usd"),),
    )
    after = catalog.tables["demo.widgets"]
    assert "lifetime_value" not in after
    # The same column under its old label — a rename never was anything else.
    assert after["ltv_usd"].field_id == 2


def test_the_spec_is_restored_to_disk_verbatim(project):
    root, _, catalogs = project
    before = (root / "widget.yaml").read_text()
    _spec(root, f"{LTV}\n{REGION}")
    (root / "extra.yaml").write_text(GADGET.format(catalog="rest_main"))
    assert _apply(root, catalogs).status == APPLIED

    _, _, _, changes, result, _ = _rollback(root, catalogs, version=1)

    assert result.status == APPLIED, result.error
    assert (root / "widget.yaml").read_text() == before, "byte-identical, not re-serialized"
    # A file that did not exist at version 1 is not left behind: the old spec *plus* whatever came
    # after is not the spec that was recorded, so leaving it would not be a rollback.
    assert (changes.written, changes.deleted) == (("widget.yaml",), ("extra.yaml",))
    assert not (root / "extra.yaml").exists()


def test_the_working_tree_is_untouched_until_the_ddl_has_run(project):
    """Planning happens against a copy in a temp directory, so nothing the user has open moves
    while the plan is still something they could decline."""
    root, _, catalogs = project
    _spec(root, f"{LTV}\n{REGION}")
    assert _apply(root, catalogs).status == APPLIED
    edited = (root / "widget.yaml").read_text()

    target, _, _, changes, _, _ = _rollback(root, catalogs, version=1, execute=False)

    assert changes.written == ("widget.yaml",)
    assert (root / "widget.yaml").read_text() == edited, "planned, not yet restored"
    assert json.loads(json.dumps(dict(target.snapshot.files)))["widget.yaml"] != edited


# --- what it leaves behind -------------------------------------------------------------------


def test_a_rolled_back_add_is_left_live_and_named(project):
    """Reversing an add means dropping, and Loom never drops. So the column stays, the restored
    spec no longer maps it, and it is unmanaged from here on — which is the honest report, but only
    if it is said rather than discovered."""
    root, catalog, catalogs = project
    _spec(root, f"{LTV}\n{REGION}")
    assert _apply(root, catalogs).status == APPLIED

    target, plan, left, changes, _, _ = _rollback(root, catalogs, version=1, execute=False)

    assert [(e.table, e.columns) for e in left] == [("demo.widgets", ("region",))]
    assert [(u.table, u.columns) for u in plan.unmanaged] == [("demo.widgets", ("region",))]
    assert "region — added after version 1" in render_rollback(target, plan, left, changes)

    assert _rollback(root, catalogs, version=1)[4].status == APPLIED
    assert "region" in catalog.tables["demo.widgets"], "never dropped"


def test_a_column_nobody_ever_mapped_is_reported_separately(project):
    """Someone else's data and a column this rollback stranded are both unmanaged from here on, but
    they are not the same problem and the report does not merge them."""
    root, catalog, catalogs = project
    catalog.tables["demo.widgets"]["audit_note"] = Column("audit_note", "string", required=False, field_id=9)
    _spec(root, f"{LTV}\n{REGION}")
    assert _apply(root, catalogs).status == APPLIED

    target, plan, left, changes, _, _ = _rollback(root, catalogs, version=1, execute=False)

    assert sorted(c for e in left for c in e.columns) == ["region"]
    out = render_rollback(target, plan, left, changes)
    assert "region — added after version 1" in out
    assert "audit_note — never mapped by this ontology" in out


def test_a_table_created_since_is_left_in_place(project):
    """The drop question one level up, with the same answer."""
    root, catalog, catalogs = project
    (root / "gadget.yaml").write_text(
        "objectType:\n  apiName: Gadget\n  primaryKey: id\n  title: id\n"
        "  backing: { catalog: rest_main, table: demo.gadgets }\n"
        "  properties:\n    - { name: id, type: string, column: id, unique: true }\n"
    )
    assert _apply(root, catalogs).status == APPLIED
    assert "demo.gadgets" in catalog.tables

    target, plan, left, changes, result, _ = _rollback(root, catalogs, version=1)

    assert result.status == APPLIED, result.error
    assert catalog.table_exists("demo.gadgets"), "a rollback never drops a table either"
    assert [(e.table, e.whole_table) for e in left] == [("demo.gadgets", True)]
    assert "demo.gadgets — the whole table, created after version 1" in render_rollback(
        target, plan, left, changes
    )


# --- what it refuses -------------------------------------------------------------------------


def test_rolling_back_a_promotion_is_refused_whole(tmp_path):
    """A promotion reverses to a narrowing, which is breaking, so it goes through the refusal that
    already exists. Not a hole in rollback: once the column is a `double`, the spec that says `int`
    no longer describes this lake, and the way out of that is forward."""
    root, catalog, catalogs = _fresh(tmp_path, "    - { name: ltv, type: int, column: ltv_usd, nullable: true }")
    _spec(root, LTV)  # int -> double
    assert _apply(root, catalogs).status == APPLIED
    assert catalog.tables["demo.widgets"]["ltv_usd"].iceberg_type == "double"

    on_disk = (root / "widget.yaml").read_text()
    writes = len(catalog.writes)
    _, _, _, _, result, _ = _rollback(root, catalogs, version=1)

    assert result.status == REFUSED
    assert "double does not promote to int" in result.error
    assert len(catalog.writes) == writes, "a refused rollback writes nothing, not even history"
    assert (root / "widget.yaml").read_text() == on_disk, "and touches no file either"


def test_rolling_back_a_loosening_is_refused_whole(tmp_path):
    """The other irreversible one: `relax` reverses to a tighten, and existing rows may already
    hold the nulls the restored constraint would not admit."""
    root, catalog, catalogs = _fresh(tmp_path, "    - { name: ltv, type: double, column: ltv_usd }")
    _spec(root, LTV)  # required -> optional
    assert _apply(root, catalogs).status == APPLIED
    assert catalog.tables["demo.widgets"]["ltv_usd"].required is False

    _, _, _, _, result, _ = _rollback(root, catalogs)

    assert result.status == REFUSED
    assert "optional -> required" in result.error


def test_a_reverse_rename_onto_a_column_the_restored_spec_maps_is_an_error(tmp_path):
    """The one shape the synthesised overlay can get into that a written `renamedFrom` cannot, so
    it is checked where the overlay is applied rather than left to the planner.

    It takes an out-of-band drop to reach — which the live-catalog-as-baseline rule explicitly
    tolerates: `region` disappears outside Loom, the next spec renames `ltv_usd` onto the name that
    freed, and rolling back would have to both rename `region` away and have it. One column, two
    incompatible instructions."""
    root, catalog, catalogs = _fresh(tmp_path, f"{LTV}\n{REGION}")
    del catalog.tables["demo.widgets"]["region"]
    _spec(root, "    - { name: ltv, type: double, column: region, nullable: true, renamedFrom: ltv_usd }")
    assert _apply(root, catalogs).status == APPLIED

    target, _, _, _, _, diag = _rollback(root, catalogs, version=1, execute=False)

    assert target.renames == {("rest_main", "demo.widgets"): {"ltv_usd": "region"}}
    assert [e.render() for e in diag.errors] == [
        "rolling back 'demo.widgets' would rename 'region' back to 'ltv_usd', but the restored "
        "spec maps both columns\n"
        "    hint: Loom never drops a column, so a rename cannot take one another property is "
        "live on — roll back to a version on the other side of that rename"
    ]


def test_rolling_back_onto_a_column_someone_recreated_is_refused(project):
    """The *both live* shape, reached through the overlay rather than through the spec. Merging two
    columns means dropping one, so the answer is the same one `apply` gives."""
    root, catalog, catalogs = project
    _spec(root, LTV_RENAMED)
    assert _apply(root, catalogs).status == APPLIED
    catalog.tables["demo.widgets"]["ltv_usd"] = Column("ltv_usd", "double", required=False, field_id=7)

    _, _, _, _, result, _ = _rollback(root, catalogs, version=1)

    assert result.status == REFUSED
    assert "cannot merge them" in result.error


# --- version selection ------------------------------------------------------------------------


def test_a_version_no_catalog_holds_says_which_ones_exist(project):
    root, _, catalogs = project
    history = {name: MetaStore(c).history() for name, c in catalogs.items()}

    with pytest.raises(RollbackError) as e:
        resolve_target(history, 9)

    assert "no catalog has a version 9" in str(e.value)
    assert "recorded versions are 1" in str(e.value)


def test_the_first_apply_has_nothing_behind_it(project):
    root, _, catalogs = project
    history = {name: MetaStore(c).history() for name, c in catalogs.items()}

    with pytest.raises(RollbackError) as e:
        resolve_target(history, None)

    assert "nothing behind it" in str(e.value)


def test_the_default_target_is_one_step_back(project):
    root, _, catalogs = project
    _spec(root, f"{LTV}\n{REGION}")
    _apply(root, catalogs)
    _spec(root, LTV)
    _apply(root, catalogs)

    target, _, _, _, _, _ = _rollback(root, catalogs, execute=False)

    assert target.version == 2


def test_a_partial_apply_is_restorable_and_says_so(project):
    """A row recorded as `partial` is still the text that was attempted, which is the thing being
    restored — but a reader deserves to know the lake was mid-flight when it was written."""
    root, catalog, catalogs = project
    _spec(root, f"{LTV}\n{REGION}")
    catalog.fail_on = "demo.widgets"
    assert _apply(root, catalogs).status != APPLIED
    catalog.fail_on = None
    assert MetaStore(catalog).latest().status == STATUS_PARTIAL

    target, plan, left, changes, _, _ = _rollback(root, catalogs, version=2, execute=False)

    assert target.status == STATUS_PARTIAL
    assert "recorded as 'partial'" in render_rollback(target, plan, left, changes)


def test_a_catalog_with_no_history_that_far_back_is_named_not_refused(tmp_path):
    """`version` selects a *spec*, not a per-catalog target — which is what makes the multi-catalog
    case tractable. A catalog the spec only started binding at version 2 has no row at version 1,
    so it is named rather than refused or silently skipped, and the restored spec then says about
    it whatever it says: here, nothing, so its table is left whole and it records nothing."""
    root, main, _ = _fresh(tmp_path, LTV)
    second = FakeWritableCatalog(name="rest_eu", tables={})
    catalogs = {main.name: main, second.name: second}
    (root / "gadget.yaml").write_text(GADGET.format(catalog="rest_eu"))
    assert _apply(root, catalogs).status == APPLIED
    assert [r.version for r in MetaStore(second).history()] == [2]

    target, plan, left, changes, result, _ = _rollback(root, catalogs, version=1)

    assert target.held_by == ("rest_main",)
    assert target.absent_from == ("rest_eu",)
    out = render_rollback(target, plan, left, changes)
    assert "'rest_eu' has no `_loom_meta` history at or before version 1" in out
    assert result.status == APPLIED, result.error
    # Never dropped, and the report says so rather than leaving it to be noticed.
    assert second.table_exists("demo.gadgets")
    assert "rest_eu.demo.gadgets — the whole table, created after version 1" in out
    assert [r.version for r in MetaStore(second).history()] == [2], "the restored spec doesn't bind it"


def test_catalogs_that_disagree_about_a_version_refuse_rather_than_pick_one(project):
    root, catalog, catalogs = project
    other = FakeWritableCatalog(name="rest_eu", tables={})
    other.rows[META_TABLE] = [dict(r.row(), content_hash="tampered") for r in MetaStore(catalog).history()]
    other.tables[META_TABLE] = {}

    with pytest.raises(RollbackError) as e:
        resolve_target({"rest_main": MetaStore(catalog).history(), "rest_eu": MetaStore(other).history()}, 1)

    assert "disagree about what version 1 was" in str(e.value)
    assert "written outside Loom" in str(e.value)


# --- chained renames --------------------------------------------------------------------------


def test_renames_compose_across_versions(project):
    """A single spec cannot express a chain, but a lake accumulates one: `ltv_usd -> lifetime_value`
    at version 2 and `lifetime_value -> ltv_total` at version 3 mean the column the version-1 spec
    calls `ltv_usd` is called `ltv_total` today. Rolling back to 1 is one rename, not two."""
    root, catalog, catalogs = project
    _spec(root, LTV_RENAMED)
    assert _apply(root, catalogs).status == APPLIED
    _spec(root, "    - { name: ltv, type: double, column: ltv_total, nullable: true, renamedFrom: lifetime_value }")
    assert _apply(root, catalogs).status == APPLIED

    target, plan, _, _, result, _ = _rollback(root, catalogs, version=1)

    assert target.renames == {("rest_main", "demo.widgets"): {"ltv_usd": "ltv_total"}}
    assert result.status == APPLIED, result.error
    assert [(c.kind, c.column, c.renamed_from) for t in plan.changes for c in t.columns] == [
        ("rename", "ltv_usd", "ltv_total")
    ]
    assert set(catalog.tables["demo.widgets"]) == {"id", "ltv_usd"}


def test_rolling_a_rollback_forward_again_needs_no_rename(project):
    """The chain composes back to where it started, so the column is not renamed from itself — an
    edit `_alteration` would read as the unmergeable shape and refuse."""
    root, catalog, catalogs = project
    _spec(root, LTV_RENAMED)
    assert _apply(root, catalogs).status == APPLIED
    assert _rollback(root, catalogs, version=1)[4].status == APPLIED

    target, plan, _, _, result, _ = _rollback(root, catalogs, version=2)

    assert target.renames == {("rest_main", "demo.widgets"): {"lifetime_value": "ltv_usd"}}
    assert result.status == APPLIED, result.error
    assert set(catalog.tables["demo.widgets"]) == {"id", "lifetime_value"}


# --- what it records --------------------------------------------------------------------------


def test_a_rollback_is_a_new_row_naming_the_version_it_restored(project):
    root, catalog, catalogs = project
    _spec(root, f"{LTV}\n{REGION}")
    assert _apply(root, catalogs).status == APPLIED

    target, _, _, _, result, _ = _rollback(root, catalogs, version=1)

    history = MetaStore(catalog).history()
    assert [r.version for r in history] == [1, 2, 3], "append-only — nothing was unwound"
    row = history[-1]
    assert row.content_hash == target.snapshot.content_hash == history[0].content_hash
    assert row.spec == history[0].spec, "the restored spec, verbatim"
    assert row.summary_data()["rollback_of"] == 1
    assert row.summary_data()["tables"] == []  # nothing to change: version 1 only added a column
    assert result.versions == {"rest_main": 3}


def test_the_rollback_row_says_applied_so_the_next_run_believes_the_truth(project):
    """`status` is what the *next* apply's "is this spec already applied here?" check reads. After a
    rollback the lake genuinely is at the restored spec, so anything but `applied` would make that
    check write a redundant row for a spec that is already live."""
    root, catalog, catalogs = project
    _spec(root, f"{LTV}\n{REGION}")
    _apply(root, catalogs)
    _rollback(root, catalogs, version=1)
    writes = len(catalog.writes)

    again = _apply(root, catalogs)

    assert MetaStore(catalog).latest().status == "applied"
    assert again.status == UP_TO_DATE
    assert len(catalog.writes) == writes, "the restored spec is already live — nothing to record"


def test_rolling_back_to_the_current_version_restores_only_the_files(project):
    """Not refused: the recorded spec and the one on disk are different questions, and "put back the
    YAML I edited but never applied" is a real thing to want."""
    root, catalog, catalogs = project
    recorded = (root / "widget.yaml").read_text()
    _spec(root, f"{LTV}\n{REGION}")  # edited, never applied

    _, plan, _, changes, result, _ = _rollback(root, catalogs, version=1)

    assert plan.is_empty
    assert result.status == UP_TO_DATE
    assert changes.written == ("widget.yaml",)
    assert (root / "widget.yaml").read_text() == recorded
