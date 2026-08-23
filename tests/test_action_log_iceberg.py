"""The edit log against a real Iceberg catalog — M3's last definition of done.

`test_action_log.py` proves the *policy* against a fake. This proves the two things a fake cannot:
that a record really is written beside a real row, in a real table this run created; and that the
row write's own Iceberg commit really carries the identity of the edit, which is the claim the whole
write-then-log ordering rests on.

It runs the shipped example, seeded but **never applied** — the quickstart's own starting point, and
the case that would have been impossible if `apply` were the one to create the log.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.action import APPLIED, MAX_ATTEMPTS, REFUSED, ActionRuntime, EditLog
from loom.catalog.base import EDIT_LOG_TABLE
from test_action_iceberg import Interloper

pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "retail"


@pytest.fixture
def runtime(seeded):
    from loom.catalog import open_catalogs

    _, ontology, config = seeded
    return ActionRuntime(ontology=ontology, catalogs=open_catalogs(config))


def catalog_of(seeded):
    """A *fresh* handle every time, so no test passes on a cached read."""
    from loom.catalog import open_catalogs

    _, _, config = seeded
    return open_catalogs(config)["local"]


def edits(seeded):
    return list(EditLog(catalog=catalog_of(seeded)).history())


def only_edit(seeded):
    rows = edits(seeded)
    assert len(rows) == 1, f"expected exactly one record, got {len(rows)}"
    return rows[0]


def snapshot_summary(seeded, table):
    """The Iceberg snapshot summary of `table`'s current snapshot — the commit's own metadata, read
    back through pyiceberg rather than through anything Loom wrote."""
    _, _, config = seeded
    from loom.catalog import open_catalogs

    return open_catalogs(config)["local"]._impl.load_table(table).current_snapshot().summary


def stamped(seeded, table, edit_id):
    """Every snapshot in `table`'s history carrying `edit_id`.

    Usually more than one for a modify, and that is worth knowing rather than asserting away: a
    `modify` is an equality-delete plus an append, and pyiceberg records each as its own Iceberg
    snapshot even though both land in a single metadata commit. Both carry the stamp, and exactly one
    of them has the asserted snapshot as its parent."""
    _, _, config = seeded
    from loom.catalog import open_catalogs

    impl = open_catalogs(config)["local"]._impl.load_table(table)
    return [s for s in impl.snapshots() if s.summary.get("loom.edit_id") == edit_id]


# ---- a real record beside a real row -------------------------------------------


def test_a_real_record_is_written_beside_a_real_row(seeded, runtime):
    """The headline, and the case the quickstart exercises: a lake that has been seeded and never
    applied to. There is no `_loom_meta` at all until this run, and the run creates the half of it
    it needs.

    That is the whole argument for the first append owning the create. Had `apply` owned it, this
    lake could not log — and a lake Loom is a guest in is exactly the one where an audit trail
    matters."""
    catalog = catalog_of(seeded)
    assert not catalog.table_exists(EDIT_LOG_TABLE)

    result = runtime.run("upgradeTier", {"customer": "c3", "newTier": "gold"}, actor="ada")
    assert result.status == APPLIED, result.failures

    row = next(r for r in catalog_of(seeded).scan("crm.customers").to_pylist() if r["id"] == "c3")
    assert row["tier"] == "gold"

    edit = only_edit(seeded)
    assert edit["edit_id"] == result.edit_id
    assert (edit["actor"], edit["action"], edit["status"]) == ("ada", "upgradeTier", APPLIED)
    assert (edit["catalog"], edit["table_name"]) == ("local", "crm.customers")
    assert edit["object_key"] == "c3" and edit["attempts"] == 1
    assert json.loads(edit["before"])["tier"] == "bronze"
    assert json.loads(edit["after"])["tier"] == "gold"
    assert edit["recorded_at"] is not None and edit["loom_version"]


def test_the_commit_that_changed_the_row_carries_the_id_of_the_record(seeded, runtime):
    """The claim the ordering rests on, against a real metastore.

    The log is a second commit and Iceberg has no transaction spanning two tables, so a crash can
    land between them. What makes that survivable is that the *row* write stamped the edit's identity
    into its own snapshot summary — so a lost record is a stamped snapshot with no matching row,
    which a reader can find, rather than silence."""
    result = runtime.run("upgradeTier", {"customer": "c3", "newTier": "silver"}, actor="grace")

    summary = snapshot_summary(seeded, "crm.customers")
    assert summary.get("loom.edit_id") == result.edit_id
    assert summary.get("loom.action") == "upgradeTier"
    assert summary.get("loom.actor") == "grace"
    assert only_edit(seeded)["edit_id"] == summary["loom.edit_id"]


def test_the_stamped_snapshot_is_the_one_the_read_asserted(seeded, runtime):
    """`read_snapshot_id` pins the write exactly, without the port having to return anything.

    The write asserted that snapshot inside the commit, so nothing else can have committed against
    it: on this ref precisely one snapshot has it as a parent, and that one is stamped. Which is why
    the log does not need the id of the snapshot the write *produced* — recording the one it was
    asserted against identifies the same commit, and asking the port to hand a snapshot id back would
    have widened three verbs for something already known."""
    before = catalog_of(seeded).current_snapshot_id("crm.customers")

    result = runtime.run("upgradeTier", {"customer": "c3", "newTier": "gold"})

    assert result.read_snapshot_id == before
    children = [s for s in stamped(seeded, "crm.customers", result.edit_id)
                if s.parent_snapshot_id == before]
    assert len(children) == 1
    assert only_edit(seeded)["read_snapshot_id"] == before


def test_a_delete_records_the_row_it_erased_without_copying_it(seeded, runtime):
    """`forgetCustomer` against real rows. The record holds what the *ontology* saw and no more —
    `region` and `segments` are gone with the row and are nowhere in the log, which is the difference
    between an audit trail and a backup nobody asked for."""
    row = next(r for r in catalog_of(seeded).scan("crm.customers").to_pylist() if r["id"] == "c1")
    assert row["region"] and row["segments"]  # the columns no property maps, with real values in them

    result = runtime.run("forgetCustomer", {"customer": "c1"}, actor="dpo")
    assert result.status == APPLIED, result.failures
    assert not [r for r in catalog_of(seeded).scan("crm.customers").to_pylist() if r["id"] == "c1"]

    edit = only_edit(seeded)
    assert edit["operation"] == "delete" and edit["after"] == ""
    assert json.loads(edit["before"])["name"] == "Ada Lovelace"
    whole = json.dumps(edit, default=str)
    assert row["region"] not in whole
    assert "early-adopter" not in whole


# ---- refusals ------------------------------------------------------------------


def test_what_the_log_holds_after_a_run_that_refused(seeded, runtime):
    """A refusal changes nothing it was asked to change — and is recorded, which is the sentence this
    slice rewrote. The table does not move, and the attempt is recoverable in full: who, against which
    object, in what state, asking for what, and why it was declined."""
    before = catalog_of(seeded).current_snapshot_id("crm.customers")

    result = runtime.run("upgradeTier", {"customer": "c1", "newTier": "gold"}, actor="mallory")

    assert result.status == REFUSED  # `c1` is already gold, so the spec's own rule refuses it
    assert catalog_of(seeded).current_snapshot_id("crm.customers") == before
    assert not stamped(seeded, "crm.customers", result.edit_id)  # nothing committed to stamp

    edit = only_edit(seeded)
    assert edit["status"] == REFUSED and edit["actor"] == "mallory"
    assert edit["object_key"] == "c1" and edit["edit_id"] == result.edit_id
    assert json.loads(edit["parameters"]) == {"customer": "c1", "newTier": "gold"}
    assert json.loads(edit["before"])["tier"] == "gold"
    assert edit["after"] == ""
    assert [f["code"] for f in json.loads(edit["failures"])] == ["validation_failed"]


def test_a_call_that_could_not_be_bound_is_not_recorded(seeded, runtime):
    """Where the boundary actually falls, including the case that looks like it should be inside it.

    `bronze` is not one of `upgradeTier`'s declared values, so the run refuses during binding —
    before a key was ever resolved. It named a customer in a parameter, but the runtime never got as
    far as addressing a row, and a record with no key answers no audit question. There is a practical
    edge to it too: every append is an Iceberg commit, and an agent looping on a malformed call would
    otherwise write one per attempt into the table meant to hold edits."""
    result = runtime.run("upgradeTier", {"customer": "c3", "newTier": "bronze"})

    assert result.status == REFUSED
    assert [f.code for f in result.failures] == ["type_error"]
    assert result.edit_id == ""
    assert not catalog_of(seeded).table_exists(EDIT_LOG_TABLE)


def test_a_refused_run_creates_the_log_table_it_needs(seeded, runtime):
    """The log is created by whatever run needs it first, and a refusal is a run. A lake whose every
    action has so far been refused still has an audit trail, which is the point of recording them."""
    assert not catalog_of(seeded).table_exists(EDIT_LOG_TABLE)

    runtime.run("upgradeTier", {"customer": "c99", "newTier": "gold"})

    assert catalog_of(seeded).table_exists(EDIT_LOG_TABLE)
    assert only_edit(seeded)["status"] == REFUSED


def test_the_validation_rules_own_sentence_reaches_the_log(seeded, runtime):
    """A failed rule carries the author's message verbatim into the result, and the result's failures
    go into the record unchanged — so the log says why in the spec's own words."""
    runtime.run("upgradeTier", {"customer": "c1", "newTier": "gold"})  # c1 is already gold

    failure = json.loads(only_edit(seeded)["failures"])[0]
    assert failure["code"] == "validation_failed"
    assert failure["message"] == "New tier must differ from the current tier"


# ---- contention ----------------------------------------------------------------


def test_what_the_log_holds_after_a_run_that_conflicted(seeded):
    """A real competing writer, committing through a second independently opened catalog handle, on
    every attempt — so the run exhausts `MAX_ATTEMPTS` and refuses.

    One record, not three. The two attempts that lost wrote nothing, so they are not edits; they are
    one edit that took three tries, and `attempts` says so. What contention there was is on the row
    in full — the conflict detail the previous slice shaped for exactly this."""
    from loom.catalog import open_catalogs

    _, ontology, config = seeded
    catalog = Interloper(
        open_catalogs(config)["local"], config, strike_on=tuple(range(1, MAX_ATTEMPTS + 1))
    )
    runtime = ActionRuntime(ontology=ontology, catalogs={"local": catalog})

    result = runtime.run("upgradeTier", {"customer": "c3", "newTier": "gold"}, actor="ada")

    assert result.status == REFUSED and result.retryable
    edit = only_edit(seeded)
    assert edit["status"] == REFUSED
    assert edit["attempts"] == MAX_ATTEMPTS
    assert edit["read_snapshot_id"] == result.read_snapshot_id
    detail = json.loads(edit["failures"])[0]["detail"]
    assert detail["table"] == "crm.customers"
    assert detail["attempts"] == MAX_ATTEMPTS
    assert detail["expectedSnapshotId"] != detail["foundSnapshotId"]
    # The interloper only ever touched `c1`'s unmapped `region`, so from `c3`'s point of view the
    # table was merely busy — and the log records that difference rather than just "conflict".
    assert detail["changed"] == [] and detail["contended"] is False


def test_a_run_that_wins_on_a_retry_is_one_record_and_one_stamped_commit(seeded):
    """The competing writer strikes once. The run retries, applies, and leaves a single record — and
    exactly one commit in the table's history carries this edit's id, because the attempt that lost
    never committed anything to stamp."""
    from loom.catalog import open_catalogs

    _, ontology, config = seeded
    catalog = Interloper(open_catalogs(config)["local"], config, strike_on=(1,))
    runtime = ActionRuntime(ontology=ontology, catalogs={"local": catalog})

    result = runtime.run("upgradeTier", {"customer": "c3", "newTier": "gold"})

    assert result.status == APPLIED and result.attempts == 2
    edit = only_edit(seeded)
    assert edit["attempts"] == 2 and edit["status"] == APPLIED

    # Every stamped snapshot in the table's history belongs to this run: the attempt that lost
    # committed nothing, so there was nothing for it to stamp.
    marked = stamped(seeded, "crm.customers", result.edit_id)
    assert marked and all(s.summary.get("loom.actor") for s in marked)
    assert len([s for s in marked if s.parent_snapshot_id == result.read_snapshot_id]) == 1


# ---- the table itself ----------------------------------------------------------


def test_the_log_is_append_only_across_runs_and_ordered(seeded, runtime):
    """Three runs, three rows, oldest first — and the earlier rows are untouched by the later ones.
    Nothing in Loom rewrites a record."""
    runtime.run("upgradeTier", {"customer": "c3", "newTier": "silver"}, actor="one")
    runtime.run("upgradeTier", {"customer": "c3", "newTier": "gold"}, actor="two")
    runtime.run("forgetCustomer", {"customer": "c3"}, actor="three")

    rows = edits(seeded)
    assert [r["actor"] for r in rows] == ["one", "two", "three"]
    assert [r["operation"] for r in rows] == ["modify", "modify", "delete"]
    assert [r["status"] for r in rows] == [APPLIED, APPLIED, APPLIED]
    assert len({r["edit_id"] for r in rows}) == 3


def test_the_log_table_is_marked_managed_and_lives_beside_the_apply_history(seeded, runtime):
    """Same namespace as `_loom_meta.applied`, and stamped `loom.managed` the way every table Loom
    creates is: one place to look, and a table that says who made it to any Iceberg client."""
    runtime.run("upgradeTier", {"customer": "c3", "newTier": "gold"})

    _, _, config = seeded
    from loom.catalog import open_catalogs

    impl = open_catalogs(config)["local"]._impl.load_table(EDIT_LOG_TABLE)
    assert impl.properties["loom.managed"] == "true"
    assert EDIT_LOG_TABLE.startswith("_loom_meta.")


def test_apply_and_rollback_leave_the_log_alone(seeded, runtime):
    """Rollback reverses DDL and only DDL. The log is rows, so it is untouched — and `plan` never
    proposes anything for it, because it only ever visits the tables the spec declares.

    Both verbs are driven for real here rather than argued about: an apply of the example spec over a
    lake that already holds an edit log, then a rollback of it, with the log read back either side."""
    from loom.cli import main

    runtime.run("upgradeTier", {"customer": "c3", "newTier": "gold"}, actor="ada")
    recorded = only_edit(seeded)

    target, _, _ = seeded
    ontology = target / "ontology"
    assert main(["apply", str(ontology), "--yes"]) == 0
    assert edits(seeded) == [recorded]

    assert main(["rollback", str(ontology), "--to", "1", "--yes"]) == 0
    assert edits(seeded) == [recorded]

    # And the planner has nothing to say about it, before or after.
    assert main(["plan", str(ontology)]) == 0


# ---- the principal column, and the silent drop that makes it a refusal ---------------


def test_a_log_table_without_the_principal_column_drops_it_without_complaint(seeded):
    """The failure `require_principal_column` exists to make impossible, demonstrated.

    `append_edit` builds its Arrow batch against the *table's own* schema, and
    `pa.Table.from_pylist` ignores keys that schema does not have. So an append carrying a principal
    into a log table created before this slice succeeds, reports nothing, and discards it — leaving a
    record that says the run had no caller, which is indistinguishable from a run that genuinely had
    none. This is the assertion that fails if that ever stops being true, at which point the refusal
    below can go."""
    import pyarrow as pa

    from loom.action.log import EDIT_COLUMNS

    older = [c for c in EDIT_COLUMNS if c.name != "principal"]
    schema = pa.schema([(c.name, pa.string()) for c in older])
    batch = pa.Table.from_pylist([{"edit_id": "e1", "principal": "https://i.test#alice"}], schema=schema)
    assert "principal" not in batch.column_names
    assert batch.to_pylist()[0]["edit_id"] == "e1"


def test_a_deployment_that_attests_refuses_a_log_it_would_drop_the_caller_from(seeded):
    """Refused at startup rather than discovered in the audit trail six months later.

    The remedy is deliberately not a port verb: `EditLogWriter` takes no table name and creates
    exactly one table, and widening it to alter one would spend the guarantee that keeps DDL out of
    the action runtime's reach. So a deployment says what it cannot do and stops."""
    from loom.action.log import EDIT_COLUMNS, require_principal_column
    from loom.catalog import open_catalogs
    from loom.catalog.base import EDIT_LOG_TABLE, Column, edit_log_writer_for
    from loom.governance import PolicyError

    _, ontology, config = seeded
    catalogs = open_catalogs(config)
    # A log table as it would have been created before this slice: every column but `principal`.
    older = tuple(c for c in EDIT_COLUMNS if c.name != "principal")
    catalogs["local"].ensure_namespace(EDIT_LOG_TABLE)
    catalogs["local"].create_table(EDIT_LOG_TABLE, older, properties={})
    assert isinstance(older[0], Column)
    assert edit_log_writer_for(catalogs["local"]) is not None

    with pytest.raises(PolicyError) as e:
        require_principal_column(ontology, catalogs)
    assert "predates attested identity" in str(e.value)
    assert "no 'principal' column" in str(e.value)


def test_a_log_table_created_now_carries_the_principal(seeded, runtime):
    """The other half: nothing to reconcile when the first append creates the table."""
    from loom.action.log import require_principal_column
    from loom.catalog import open_catalogs

    _, ontology, config = seeded
    runtime.run("upgradeTier", {"customer": "c1", "newTier": "gold"}, actor="tester", principal="https://i.test#alice")
    catalogs = open_catalogs(config)
    require_principal_column(ontology, catalogs)  # does not raise
    row = edits(seeded)[0]
    assert row["principal"] == "https://i.test#alice"
    assert row["actor"] == "tester"


def test_a_run_with_nobody_attested_records_no_principal(seeded, runtime):
    """`None`, not a placeholder. There is no `UNKNOWN_ACTOR` equivalent, because "nobody could be
    named here" is a fact about the surface rather than a name — and `loom run` is that surface."""
    runtime.run("upgradeTier", {"customer": "c1", "newTier": "gold"}, actor="tester")
    assert edits(seeded)[0]["principal"] is None
