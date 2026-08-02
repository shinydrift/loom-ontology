"""The edit log — against the fake catalog that records what it was asked to do.

Same bargain as `test_action.py`, one slice on: what is asserted here is the **policy**, because
that is the half a real catalog can only demonstrate by getting it wrong in someone's lake. And one
of these assertions is a thing *only* a fake can make. `test_the_runtime_logs_without_a_schema_port`
works because `FakeRowCatalog` implements `EditLogWriter` and not `CatalogWriter`: the runtime logs,
so it provably never reached for `create_table`. Against real pyiceberg one object implements all
four ports, so no test there can ever say which one was used.

`test_action_log_iceberg.py` proves the same sequence against real Iceberg, plus the two things a
fake cannot show: a record written beside a real row, and the commit stamp that ties them together.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from loom.action import (
    APPLIED,
    EDIT_COLUMNS,
    FAILED,
    LOG_FAILED,
    MAX_ATTEMPTS,
    PREVIEWED,
    REFUSED,
    UNKNOWN_ACTOR,
    ActionRuntime,
    EditLog,
)
from loom.action.log import commit_properties
from loom.catalog.base import (
    EDIT_LOG_TABLE,
    CatalogError,
    EditLogWriter,
    RowWriter,
    edit_log_writer_for,
    writer_for,
)
from loom.ontology import build
from test_action import FakeRowCatalog, Interloper

VALID = Path(__file__).parent / "fixtures" / "valid"


@pytest.fixture(scope="module")
def ontology():
    built, _ = build(VALID)
    return built


@pytest.fixture
def catalog():
    return FakeRowCatalog()


def runtime_for(ontology, catalog):
    return ActionRuntime(ontology=ontology, catalogs={"rest_main": catalog})


def only_edit(catalog):
    edits = catalog.edits
    assert len(edits) == 1, f"expected exactly one record, got {len(edits)}"
    return edits[0]


# ---- the port ------------------------------------------------------------------


def test_the_runtime_logs_without_a_schema_port(ontology, catalog):
    """The fourth port's whole argument, and the one assertion only a fake can make.

    `FakeRowCatalog` implements the read port, `RowWriter` and `EditLogWriter`, and deliberately no
    `CatalogWriter`. A record lands anyway — so recording an edit needs neither `create_table` nor
    `alter_table`, and the runtime never acquired a handle that has them. Had the log been written
    through a `CatalogWriter` held beside the row writer, this test could not exist and "an action
    cannot touch DDL, because the port has no verb for it" would have become a promise about
    intentions."""
    runtime_for(ontology, catalog).run("upgradeTier", {"customer": "c1", "newTier": "silver"})

    assert only_edit(catalog)["status"] == APPLIED
    assert edit_log_writer_for(catalog) is catalog
    with pytest.raises(CatalogError) as e:
        writer_for(catalog)
    assert "schema writes" in str(e.value)


def test_the_edit_log_port_cannot_name_a_table(catalog):
    """Not a policy check inside an implementation — an absence in the signature.

    `append_edit` takes columns and one row. There is no table parameter, so an action holding this
    port has nothing to point at `crm.customers` with, and no way to append a *batch* anywhere. That
    is why the log did not reuse `CatalogWriter.append_rows`, which takes both."""
    params = list(inspect.signature(EditLogWriter.append_edit).parameters)
    assert params == ["self", "columns", "row"]
    assert not hasattr(EditLogWriter, "create_table")
    assert not hasattr(EditLogWriter, "append_rows")
    # And the row port still cannot be used for it: every verb there demands an expectation the log
    # append does not hold.
    for verb in ("insert_row", "replace_row", "delete_row"):
        assert "expect_snapshot_id" in inspect.signature(getattr(RowWriter, verb)).parameters


def test_a_catalog_with_no_edit_log_port_still_writes_the_row(ontology):
    """The log is not a precondition of the write, and the failure is typed rather than raised.

    "No log, no write" is a real audit posture, but it is a *policy*, and Loom has a milestone for
    policies. Wiring it in here would make it something no deployment could turn off — and would
    mean an action refusing because of a table the spec has never heard of."""

    class NoLogCatalog(FakeRowCatalog):
        append_edit = None  # not an EditLogWriter: the attribute is not callable

    catalog = NoLogCatalog()
    result = runtime_for(ontology, catalog).run("upgradeTier", {"customer": "c1", "newTier": "silver"})

    assert result.status == APPLIED and result.ok
    assert catalog.row("crm.customers", "id", "c1")["tier"] == "silver"
    assert [f.code for f in result.failures] == [LOG_FAILED]
    assert not result.retryable


def test_a_failed_append_does_not_turn_an_applied_run_into_a_failed_one(ontology):
    """The row committed. Saying `failed` would tell a caller to retry a delete that has happened —
    the worst answer available, and the reason this diverges from `loom apply`, which *does* go to
    `failed` when `_loom_meta` cannot be written. An apply's result lists the tables that landed, so
    `failed` there is unambiguous; an action has no such list."""
    catalog = FakeRowCatalog(log_fails=True)
    result = runtime_for(ontology, catalog).run("upgradeTier", {"customer": "c1", "newTier": "silver"})

    assert result.status == APPLIED and result.ok
    assert catalog.edits == []
    failure = next(f for f in result.failures if f.code == LOG_FAILED)
    # The id is still on the result, because the *commit* carries it: it is how someone finds this
    # write in the table's history now that the log has not got it.
    assert result.edit_id and failure.detail["editId"] == result.edit_id


# ---- what is recorded, and what is not -----------------------------------------


def test_an_applied_modify_is_recorded_once_with_the_ontologys_own_view(ontology, catalog):
    runtime_for(ontology, catalog).run(
        "upgradeTier", {"customer": "c1", "newTier": "silver"}, actor="ada"
    )

    edit = only_edit(catalog)
    assert edit["actor"] == "ada"
    assert edit["action"] == "upgradeTier"
    assert edit["object_type"] == "Customer"
    assert edit["operation"] == "modify"
    assert edit["catalog"] == "rest_main"
    assert edit["table_name"] == "crm.customers"
    assert edit["object_key"] == "c1"
    assert edit["status"] == APPLIED
    assert edit["attempts"] == 1
    assert json.loads(edit["before"])["tier"] == "gold"
    assert json.loads(edit["after"])["tier"] == "silver"
    assert json.loads(edit["parameters"]) == {"customer": "c1", "newTier": "silver"}
    assert edit["failures"] == ""


def test_the_log_holds_declared_properties_and_never_the_physical_row(ontology, catalog):
    """The never-report rule, extended to a new reader rather than excepted for one.

    The physical row was the alternative and it is a worse leak than the one the rule prevents: an
    unabridged copy of the data in a table nothing governs, retained forever. `region` and `segments`
    were carried across the write untouched — that is the carry-across guarantee — and the log's
    silence about them is the *record* of that guarantee, not a gap in it. Since the concurrency
    slice the commit asserted the snapshot the read saw, so nothing moved under it: what this record
    does not name, the run did not change."""
    runtime_for(ontology, catalog).run("upgradeTier", {"customer": "c1", "newTier": "silver"})

    edit = only_edit(catalog)
    before, after = json.loads(edit["before"]), json.loads(edit["after"])
    assert set(before) == set(after) == {"customerId", "name", "tier", "ltv"}
    assert (before["tier"], after["tier"]) == ("gold", "silver")
    assert "region" not in json.dumps([before, after])
    assert "segments" not in json.dumps([before, after])
    # And the write really did carry them, which is what makes the omission a guarantee.
    assert catalog.row("crm.customers", "id", "c1")["region"] == "emea"


def test_a_delete_records_what_was_there_and_no_more(ontology, catalog):
    """`forgetCustomer` is why the physical row was never an option. An erasure action whose audit
    record keeps a complete copy of the erased row — including the columns the ontology was never
    given, and outliving the row itself — erases nothing.

    What is left is still the ontology's own view of a person, which is real and is not this slice's
    to solve: it is the same question `governance.policies` will face, and spec-v0's open edges name
    it rather than letting it pass."""
    runtime_for(ontology, catalog).run("forgetCustomer", {"customer": "c2"})

    edit = only_edit(catalog)
    assert edit["operation"] == "delete" and edit["status"] == APPLIED
    assert json.loads(edit["before"])["name"] == "Grace Hopper"
    assert edit["after"] == ""  # deleted: there is no after
    assert "amer" not in edit["before"]  # `region`, carried across nothing and recorded nowhere
    assert catalog.row("crm.customers", "id", "c2") is None


def test_a_refused_run_is_recorded_and_still_writes_no_data(ontology, catalog):
    """The invariant restated rather than weakened. A refusal changes nothing it was asked to
    change — and leaves a record that it was asked."""
    refused = runtime_for(ontology, catalog).run(
        "upgradeTier", {"customer": "c1", "newTier": "gold"}, actor="grace"
    )

    assert refused.status == REFUSED
    assert catalog.writes == []  # nothing was written to the data table
    edit = catalog.edits[-1]
    assert edit["status"] == REFUSED and edit["actor"] == "grace"
    failures = json.loads(edit["failures"])
    assert [f["code"] for f in failures] == ["validation_failed"]
    assert failures[0]["message"] == "New tier must differ from current tier"
    # The attempt is recoverable in full: what they asked for, and the state they asked it against.
    assert json.loads(edit["parameters"])["newTier"] == "gold"
    assert json.loads(edit["before"])["tier"] == "gold"


def test_a_run_that_never_named_a_row_is_not_recorded(ontology, catalog):
    """The boundary. A malformed call is not an attempted edit: it reached no object, so its record
    would carry no key and answer no audit question. Request-level logging is the serve boundary's
    job, and this table is called `edits`."""
    result = runtime_for(ontology, catalog).run("upgradeTier", {"newTier": "gold"})

    assert result.status == REFUSED
    assert [f.code for f in result.failures] == ["missing_parameter"]
    assert catalog.edits == []
    assert result.edit_id == ""


def test_an_object_that_does_not_exist_is_recorded_because_the_run_named_it(ontology, catalog):
    """The other side of that boundary, and the reason it is drawn at the key rather than at the
    read. "Who tried to delete a customer that wasn't there" is an audit question; the caller named
    an object, and only the lake disagreed."""
    runtime_for(ontology, catalog).run("forgetCustomer", {"customer": "c99"})

    edit = only_edit(catalog)
    assert edit["status"] == REFUSED and edit["object_key"] == "c99"
    assert json.loads(edit["failures"])[0]["code"] == "object_not_found"
    assert edit["before"] == ""  # there was nothing to record


def test_a_preview_is_never_recorded(ontology, catalog):
    """`loom run` previews before every real run, so logging previews would double the table — and a
    preview writes nothing, which is the thing the log is a record of."""
    result = runtime_for(ontology, catalog).preview("upgradeTier", {"customer": "c1", "newTier": "silver"})

    assert result.status == PREVIEWED
    assert catalog.edits == []
    assert result.edit_id == ""


def test_a_write_that_failed_is_recorded_because_nobody_knows_whether_it_landed(ontology):
    """The status most worth having a record of. `failed` means the runtime had decided to go ahead
    and the write raised — so the record carries the `edit_id`, and the commit (if there was one)
    carries the same id, which is how the question gets answered from the table's history."""
    catalog = FakeRowCatalog(fail_on="crm.customers")
    result = runtime_for(ontology, catalog).run("upgradeTier", {"customer": "c1", "newTier": "silver"})

    assert result.status == FAILED
    edit = only_edit(catalog)
    assert edit["status"] == FAILED and edit["edit_id"] == result.edit_id
    assert json.loads(edit["failures"])[0]["code"] == "write_failed"


# ---- retries -------------------------------------------------------------------


def test_a_retried_run_is_one_record_not_three(ontology):
    """One row per run. The losing attempts wrote nothing, so they are not edits — they are one edit
    that took two tries, and `attempts` says so. The states they lost to are not this run's to
    describe either: a competing writer coming through Loom has its own record in this table, and one
    that isn't could not be described honestly anyway. Three rows would mean most of the table
    described things that did not happen."""
    inner = FakeRowCatalog()
    catalog = Interloper(inner, strike_on=(1,))
    result = runtime_for(ontology, catalog).run("upgradeTier", {"customer": "c1", "newTier": "silver"})

    assert result.status == APPLIED and result.attempts == 2
    edit = only_edit(inner)
    assert edit["attempts"] == 2 and edit["status"] == APPLIED
    # And the record describes the row actually written over — the final attempt's read, not the
    # first one's, which is the same rule `ActionResult.before` follows.
    assert json.loads(edit["before"])["tier"] == "gold"


def test_a_run_that_never_wins_leaves_one_record_carrying_the_contention(ontology):
    """The record of a contended row, which is the case the "only successes" objection is really
    about. It is one row, and the conflict detail the previous slice shaped is on it verbatim —
    expected, found, attempts, what moved, and whether any of it is this action's business."""
    inner = FakeRowCatalog()
    catalog = Interloper(inner, strike_on=tuple(range(1, MAX_ATTEMPTS + 1)))
    result = runtime_for(ontology, catalog).run("upgradeTier", {"customer": "c1", "newTier": "silver"})

    assert result.status == REFUSED and result.retryable
    edit = only_edit(inner)
    assert edit["status"] == REFUSED and edit["attempts"] == MAX_ATTEMPTS
    detail = json.loads(edit["failures"])[0]["detail"]
    assert detail["table"] == "crm.customers"
    assert detail["attempts"] == MAX_ATTEMPTS
    assert detail["expectedSnapshotId"] != detail["foundSnapshotId"]
    # The competing writer only ever touched `region`, which no property maps — a busy table rather
    # than a contested row, and the detail is diffed through the same projection the log is.
    assert detail["changed"] == [] and detail["contended"] is False


# ---- the commit stamp ----------------------------------------------------------


def test_the_write_stamps_the_edit_id_into_its_own_commit(ontology, catalog):
    """The only record of a write that is atomic with it.

    Everything else, the log included, is a second commit that a crash can land on the wrong side of.
    Because the stamp rides inside the transaction, a lost log row is a stamped snapshot with no
    matching record — a gap a reader can find, rather than silence."""
    result = runtime_for(ontology, catalog).run(
        "upgradeTier", {"customer": "c1", "newTier": "silver"}, actor="ada"
    )

    snapshot = catalog.snapshots["crm.customers"]
    assert catalog.commits[("crm.customers", snapshot)] == {
        "loom.edit_id": result.edit_id,
        "loom.action": "upgradeTier",
        "loom.actor": "ada",
    }
    assert only_edit(catalog)["edit_id"] == result.edit_id


def test_every_attempt_of_one_run_carries_the_same_edit_id(ontology):
    """A run has one identity whatever it takes. Three attempts and one commit share an id because
    they are one edit that took three tries — the same reason the log holds one row."""
    inner = FakeRowCatalog()
    catalog = Interloper(inner, strike_on=(1,))
    result = runtime_for(ontology, catalog).run("upgradeTier", {"customer": "c1", "newTier": "silver"})

    stamps = [p for p in inner.commits.values() if p]
    assert stamps and {s["loom.edit_id"] for s in stamps} == {result.edit_id}


def test_the_stamp_is_who_what_and_which_and_no_payload():
    """A snapshot summary is carried in every read of the table's metadata, so it is not the place
    for `before`/`after`. Three keys: the id that ties the commit to the record, and enough to read
    the history without the log table at all."""
    assert commit_properties("abc", "upgradeTier", "ada") == {
        "loom.edit_id": "abc",
        "loom.action": "upgradeTier",
        "loom.actor": "ada",
    }


# ---- the actor -----------------------------------------------------------------


def test_the_runtime_never_falls_back_to_default_actor(ontology, catalog, monkeypatch):
    """`default_actor()` reads `LOOM_ACTOR` and is honest for the commands a person runs. On this
    path it would name whoever started `loom serve`, so every MCP caller in a deployment would record
    the same string — and a log that confidently names the wrong principal is worse than one that
    says it does not know."""
    monkeypatch.setenv("LOOM_ACTOR", "the-serve-process")

    runtime_for(ontology, catalog).run("upgradeTier", {"customer": "c1", "newTier": "silver"})

    edit = only_edit(catalog)
    assert edit["actor"] == UNKNOWN_ACTOR
    assert catalog.commits[("crm.customers", catalog.snapshots["crm.customers"])][
        "loom.actor"
    ] == UNKNOWN_ACTOR


def test_the_actor_is_per_call_not_per_runtime(ontology, catalog):
    """`loom serve` is long-lived and a caller is not, so the argument is on `run` rather than on the
    runtime. One runtime, two callers, two records."""
    runtime = runtime_for(ontology, catalog)
    runtime.run("upgradeTier", {"customer": "c1", "newTier": "silver"}, actor="ada")
    runtime.run("upgradeTier", {"customer": "c2", "newTier": "gold"}, actor="grace")

    assert [e["actor"] for e in catalog.edits] == ["ada", "grace"]


# ---- the table -----------------------------------------------------------------


def test_the_log_table_is_created_on_first_write_not_by_apply(ontology, catalog):
    """`apply` does not create it and does not know it exists — the spec never names this table, so
    `plan` cannot propose it and `validate --physical` cannot check it. Making `apply` the creator
    would give the log a precondition the write does not have, and Loom writes to lakes it never
    migrated. This catalog has never been applied to."""
    assert not catalog.table_exists(EDIT_LOG_TABLE)

    runtime_for(ontology, catalog).run("upgradeTier", {"customer": "c1", "newTier": "silver"})

    assert catalog.table_exists(EDIT_LOG_TABLE)
    assert catalog.edit_columns == EDIT_COLUMNS


def test_only_two_columns_are_required_because_the_rest_can_never_be_added(ontology):
    """`append_edit` only ever *creates* the table, so a column left out today can never reach a log
    that already exists — the same trap `_loom_meta.applied` names. Hence a generous schema, almost
    all of it optional, and anything still unsettled inside a JSON column rather than waiting for a
    column that cannot arrive."""
    required = [c.name for c in EDIT_COLUMNS if c.required]
    assert required == ["edit_id", "recorded_at"]
    assert {"before", "after", "failures", "parameters"} <= {c.name for c in EDIT_COLUMNS}
    # `table` and `key` are reserved words in dialects Loom already targets, and this table is meant
    # to be read from any SQL engine somebody points at the lake.
    names = {c.name for c in EDIT_COLUMNS}
    assert "table" not in names and "key" not in names


def test_the_log_reads_back_oldest_first(ontology, catalog):
    """Iceberg promises no scan order, and an audit trail read out of sequence is a trap."""
    runtime = runtime_for(ontology, catalog)
    runtime.run("upgradeTier", {"customer": "c1", "newTier": "silver"}, actor="one")
    runtime.run("upgradeTier", {"customer": "c1", "newTier": "gold"}, actor="two")

    history = EditLog(catalog=catalog).history()
    assert [r["actor"] for r in history] == ["one", "two"]
    assert EditLog(catalog=FakeRowCatalog()).history() == ()


def test_plan_never_proposes_a_change_to_the_log_table(ontology):
    """Not because of a marker — `loom.managed` is written by `apply` and read by nothing. `plan`
    only ever visits the tables the *spec* declares (`diff_ontology` iterates `desired_tables`), which
    is why `_loom_meta.applied` has never been proposed either. Pinned here because the edit log is
    the first Loom-created table an action can conjure, and a planner that started proposing changes
    to a table no spec has heard of would be proposing them against a schema only Loom knows.

    The migration fake rather than the row fake, because this is a question about the planner: it
    holds an edit log alongside the two backing tables, exactly as a lake that has been both applied
    to and run against would."""
    from loom.errors import Diagnostics
    from loom.migrate import diff_ontology
    from test_apply import CUSTOMERS as CUSTOMER_COLUMNS
    from test_apply import ORDERS, FakeWritableCatalog

    catalog = FakeWritableCatalog(
        tables={
            "crm.customers": CUSTOMER_COLUMNS,
            "sales.orders": ORDERS,
            EDIT_LOG_TABLE: {c.name: c for c in EDIT_COLUMNS},
        }
    )

    diag = Diagnostics()
    plan = diff_ontology(ontology, {"rest_main": catalog}, diag)

    assert not diag.errors
    touched = {c.table for c in plan.changes} | {u.table for u in plan.unmanaged}
    assert touched == set()  # the two backing tables already match the spec
    assert EDIT_LOG_TABLE not in {table for _catalog, table in plan.targets}


def test_the_log_is_rows_so_rollback_never_touches_it():
    """Rollback reverses DDL and only DDL, which is stated in spec §9.1 and is why this is an
    assertion about the *port*: `rollback` executes through `apply_plan`, which holds a
    `CatalogWriter`, and a `CatalogWriter` has no verb that can remove a row from anything."""
    from loom.catalog.base import CatalogWriter

    verbs = {n for n in dir(CatalogWriter) if not n.startswith("_")}
    assert verbs == {"ensure_namespace", "create_table", "alter_table", "append_rows"}
    assert not any("delete" in v or "replace" in v for v in verbs)
