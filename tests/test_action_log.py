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
from dataclasses import replace
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
    build_runtime,
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
from loom.governance import PolicyError
from loom.ontology import build
from loom.resolver import build_resolver
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
    assert list(inspect.signature(EditLogWriter.append_edit).parameters) == ["self", "columns", "row"]
    assert list(inspect.signature(EditLogWriter.ensure_log).parameters) == ["self", "columns"]
    assert not hasattr(EditLogWriter, "create_table")
    assert not hasattr(EditLogWriter, "append_rows")
    # And the row port still cannot be used for it: every verb there demands an expectation the log
    # append does not hold.
    for verb in ("insert_row", "replace_row", "delete_row"):
        assert "expect_snapshot_id" in inspect.signature(getattr(RowWriter, verb)).parameters


def test_the_second_verb_widened_the_port_by_nothing(catalog):
    """`ensure_log` is `append_edit` with the row taken out, and that is the whole of the argument
    for adding it to a port whose one-verb shape is load-bearing.

    Same single table, same absent table argument, same DDL that `append_edit` could already reach
    on its first call. What it buys is the only exact answer to *can this deployment record what it
    writes* — a probe answers about an instant, and `table_exists` cannot even ask, since `False` is
    the ordinary state of a catalog whose first append has not happened."""
    verbs = [n for n in vars(EditLogWriter) if not n.startswith("_") and callable(getattr(EditLogWriter, n))]
    assert sorted(verbs) == ["append_edit", "ensure_log"]
    for verb in verbs:
        assert "table" not in inspect.signature(getattr(EditLogWriter, verb)).parameters

    # And it really is create-without-append: the log exists and holds nothing.
    EditLog(catalog=catalog, writer=edit_log_writer_for(catalog)).ensure()
    assert catalog.table_exists(EDIT_LOG_TABLE)
    assert catalog.edits == []


def test_nothing_on_the_edit_log_port_removes_a_record():
    """The invariant a retention window would have spent, asserted where the port is shaped.

    `_record` writes *after* the commit, and the single thing that buys is that a lost record is
    findable: the row write stamped `loom.edit_id` into its own Iceberg snapshot, so a stamp with no
    matching row means one thing. Expire records and it means two — lost, or expired — and the
    reader holding the stamp cannot tell which. So erasure, which this table genuinely owes, can
    only ever be a redaction that keeps the row, by a command holding a port that is not this one."""
    for verb in ("delete_edit", "delete_row", "expire", "redact", "overwrite", "delete_rows"):
        assert not hasattr(EditLogWriter, verb)
    assert not hasattr(EditLog, "delete")
    assert not hasattr(EditLog, "expire")


def test_a_catalog_with_no_edit_log_port_still_writes_the_row(ontology):
    """The log is not a precondition of the write, and the failure is typed rather than raised.

    That was once argued as "no log, no write is a policy, and Loom has a milestone for policies".
    The milestone happened, the policy exists — `governance.edit_log: required`, checked at
    startup — and this behaviour is what the *other* posture means, which is the default. Wiring it
    in here would still be wrong for the original reason: it would be something no deployment could
    turn off, and an action refusing because of a table the spec has never heard of."""

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


# ---- the posture: governance.edit_log ------------------------------------------


def _config(edit_log="optional"):
    from loom.config import LoomConfig

    return LoomConfig(edit_log=edit_log)


def test_a_required_edit_log_refuses_a_deployment_whose_catalog_has_no_port(ontology):
    """The structural half, and the only half that is *permanent*.

    A catalog implementing no `EditLogWriter` does not fail a write once — it writes every row and
    reports `log_failed` afterwards for as long as the deployment lives. That is a fact about the
    pairing of a spec and a deployment, so it is refused where every other such fact is refused, and
    the message names the catalog, the actions that write to it, and the posture that permits it."""

    class NoLogCatalog(FakeRowCatalog):
        append_edit = None

    with pytest.raises(PolicyError) as e:
        build_runtime(ontology, _config("required"), {"rest_main": NoLogCatalog()})

    message = str(e.value)
    assert "governance.edit_log is 'required'" in message
    assert "catalog 'rest_main'" in message and "'upgradeTier'" in message
    assert "'governance.edit_log: optional'" in message


def test_a_required_edit_log_refuses_a_catalog_that_cannot_create_one(ontology):
    """The physical half, which is provable only by doing it.

    So the check *creates* the table rather than probing for one. `table_exists` asks the wrong
    question — `False` is the ordinary state of a catalog whose first append has not happened — and
    creating a table records nothing that might not have happened, which is what keeps this clear of
    the log-then-write ordering `_record` rejected. An empty log is a permission, not an intention."""
    with pytest.raises(PolicyError) as e:
        build_runtime(ontology, _config("required"), {"rest_main": FakeRowCatalog(log_create_fails=True)})
    assert "cannot be created" in str(e.value)


def test_a_required_edit_log_exists_before_the_first_run_rather_than_after_it(ontology):
    """What the posture buys, stated as the thing an operator can check: by the time `build_runtime`
    returns, the log is there. Under the default it appears on the first append and not before."""
    eager = FakeRowCatalog()
    build_runtime(ontology, _config("required"), {"rest_main": eager})
    assert eager.table_exists(EDIT_LOG_TABLE) and eager.edits == []

    lazy = FakeRowCatalog()
    build_runtime(ontology, _config(), {"rest_main": lazy})
    assert not lazy.table_exists(EDIT_LOG_TABLE)


def test_a_required_edit_log_changes_nothing_after_the_write(ontology):
    """The boundary the posture cannot cross, and it is not a limitation — it is `_record`'s
    argument, which no config weakens.

    A log that could be created and then refuses a record is exactly the window `require_edit_log`
    says it does not close. By the time the append runs the row has committed, so `failed` would
    tell a caller to retry a delete that already happened. It stays `applied` + `log_failed` under
    either posture, and what is recoverable stays recoverable: the commit carries the `edit_id`, so
    the missing record is a gap somebody can find."""
    catalog = FakeRowCatalog(log_fails=True)
    runtime = build_runtime(ontology, _config("required"), {"rest_main": catalog})
    result = runtime.run("upgradeTier", {"customer": "c1", "newTier": "silver"})

    assert result.status == APPLIED and result.ok
    assert [f.code for f in result.failures] == [LOG_FAILED]
    assert catalog.row("crm.customers", "id", "c1")["tier"] == "silver"
    assert result.edit_id


def test_a_spec_that_declares_no_action_has_nothing_for_the_posture_to_be_about(ontology):
    """An empty subject rather than a lenient answer. A spec with no actions writes nothing, so
    there is no run this posture could refuse and no catalog it could demand a log of — including a
    catalog that could not provide one."""

    class NoLogCatalog(FakeRowCatalog):
        append_edit = None

    readonly = replace(ontology, actions={})
    assert build_runtime(readonly, _config("required"), {"rest_main": NoLogCatalog()}).catalogs


def test_a_catalog_the_config_never_declared_is_one_fault_and_is_reported_once(ontology):
    """An unwritable deployment rather than an unloggable one, so this check stays quiet about it.

    Every run of those actions already fails with `catalog_for`'s message, which names the missing
    catalog and says where to declare it. Refusing here as well would report one fault as two, and
    the second report would be the less useful one — "cannot record what it writes" is a strange way
    to say "there is no catalog to write to"."""
    assert build_runtime(ontology, _config("required"), {}).catalogs == {}


def test_the_read_plane_is_not_asked_about_a_log_it_cannot_write(ontology):
    """The one governance key that binds a single plane, asserted as an absence.

    `build_resolver` binds masks and row filters because a read is what they withhold from. It has
    no business with `edit_log`: the read plane writes no rows, so it produces no records, so there
    is nothing it could fail to record. A resolver refusing to build over an unloggable catalog
    would make `loom query` unusable to protect an audit trail it never touches."""
    assert "edit_log" not in inspect.getsource(build_resolver)


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


# --- probe #7: the record of a run survives an ordinary commit race ---------------------------


def test_the_log_append_is_retried_when_it_loses_a_commit_race():
    """An append to the edit log asserts no snapshot, so a conflict on it means nothing.

    Iceberg still refuses the second of two concurrent appends — "Table has been updated by another
    process" — and with one attempt per run that refusal was final. Six concurrent runs through a
    served `run_` tool lost two audit rows to it. The row write already retries a conflict and calls
    the retry the price of a coarse check; the log had the price and not the retry. It matters most
    for a *refusal*, whose lost record is undetectable: a refusal leaves no commit to stamp with the
    `edit_id` that would make the gap findable."""
    from loom.action.log import MAX_LOG_ATTEMPTS, EditLog
    from loom.catalog.base import CatalogError

    class Flaky:
        """Loses every race but the last, the way a busy log table does."""

        def __init__(self, failures):
            self.remaining = failures
            self.appended = []

        def ensure_log(self, columns):
            pass

        def append_edit(self, columns, row):
            if self.remaining:
                self.remaining -= 1
                raise CatalogError("Table has been updated by another process")
            self.appended.append(row)

    writer = Flaky(MAX_LOG_ATTEMPTS - 1)
    EditLog(catalog=None, writer=writer).record(_an_edit())
    assert len(writer.appended) == 1, "the record should survive a losing streak it can outlast"


def test_a_log_append_that_keeps_losing_is_still_reported_as_lost():
    """The retry raises the ceiling; it does not pretend there is none. The last failure comes back
    untouched so `log_failed` still says exactly what Iceberg said."""
    from loom.action.log import MAX_LOG_ATTEMPTS, EditLog
    from loom.catalog.base import CatalogError

    attempts = []

    class AlwaysLoses:
        def ensure_log(self, columns):
            pass

        def append_edit(self, columns, row):
            attempts.append(row)
            raise CatalogError("Table has been updated by another process")

    with pytest.raises(CatalogError, match="another process"):
        EditLog(catalog=None, writer=AlwaysLoses()).record(_an_edit())
    assert len(attempts) == MAX_LOG_ATTEMPTS


def _an_edit():
    """One minimal record. Only the two required columns matter here — this is about the append."""
    from datetime import UTC, datetime

    from loom.action.log import EditRecord

    return EditRecord(
        edit_id="e1",
        recorded_at=datetime.now(UTC),
        actor="probe",
        action="upgradeTier",
        object_type="Customer",
        operation="modify",
        catalog="rest_main",
        table_name="crm.customers",
        object_key="c1",
        status="refused",
        attempts=1,
    )
