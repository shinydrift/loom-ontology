"""`renamedFrom` — a moved column as a field-id remap rather than an add beside stranded data.

The classification is what matters here, so it runs against the same fake catalog as test_plan.py.
Almost every test below is really one question: *given what the live table actually holds, what does
`renamedFrom` mean right now?* There are four answers, and the point of the key is that three of
them are quiet — the loud one is the half-finished migration Loom refuses to guess at.

The live proof that a rename really does keep its field id and its rows lives in
test_apply_iceberg.py; nothing here can establish that, because the fake catalog would agree to
anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from loom.catalog.base import Column, TableSchema
from loom.errors import Diagnostics
from loom.loader import load_dir
from loom.migrate import Severity, diff_ontology, render_plan
from loom.ontology import build
from loom.validator import check_physical, validate

# The table as it stands *before* the rename: `old_score` is what the spec below renames from.
BEFORE = {
    "id": Column("id", "string", required=True, field_id=1),
    "old_score": Column("old_score", "double", required=False, field_id=2),
}
# ...and after, which is the same column under a new label — note the field id.
AFTER = {
    "id": Column("id", "string", required=True, field_id=1),
    "score": Column("score", "double", required=False, field_id=2),
}


class FakeCatalog:
    def __init__(self, tables, name="main"):
        self.name = name
        self.tables = tables

    def table_exists(self, table: str) -> bool:
        return table in self.tables

    def describe(self, table: str) -> TableSchema:
        return TableSchema(table=table, columns=self.tables[table])

    def scan(self, table, columns=None, predicates=(), limit=None):  # pragma: no cover - unused
        raise NotImplementedError


WIDGET = """
objectType:
  apiName: Widget
  primaryKey: id
  title: id
  backing: {{ catalog: main, table: demo.widgets }}
  properties:
    - {{ name: id, type: string, column: id, unique: true }}
    - {{ name: score, type: double, column: score, nullable: true{extra} }}
"""


def _widget(tmp_path: Path, *, renamed_from: str | None = "old_score", extra: str = ""):
    """The one-property spec every shape below is planned from: `score`, renamed from `old_score`."""
    rename = f", renamedFrom: {renamed_from}" if renamed_from else ""
    (tmp_path / "widget.yaml").write_text(WIDGET.format(extra=rename + extra))
    built, _ = build(tmp_path)
    return built


def _plan(ontology, live):
    diag = Diagnostics()
    plan = diff_ontology(ontology, {"main": FakeCatalog({"demo.widgets": live})}, diag)
    assert diag.errors == [], [e.message for e in diag.errors]
    return plan, diag


def _columns(plan):
    return [(c.kind, c.column, c.severity) for t in plan.changes for c in t.columns]


def _messages(tmp_path: Path) -> str:
    """Load + validate without raising, so a test can assert on the whole batch of errors."""
    diag = Diagnostics()
    loaded = load_dir(tmp_path, diag)
    validate(loaded, diag)
    return " | ".join(e.render() for e in diag.errors)


# --- the four live shapes ---------------------------------------------------------------------
#
# The spec is identical in all four. Only the lake differs, because the lake is the baseline.


def test_the_old_column_alone_is_a_rename(tmp_path):
    plan, _ = _plan(_widget(tmp_path), BEFORE)

    assert _columns(plan) == [("rename", "score", Severity.SAFE)]
    change = plan.changes[0].columns[0]
    assert change.detail == "renamed from old_score"
    assert change.renamed_from == "old_score"
    # Safe rather than physical-safe: the field id, the type and the nullability all survive, so
    # nothing about the *stored* column moves. What does move is the name other readers select by.
    assert "keeps field id 2" in change.reason
    assert "readers outside the ontology" in change.reason


def test_the_new_column_alone_is_a_clean_no_op(tmp_path):
    """The idempotency case, and the reason it needs no bookkeeping: the rename already landed, so
    the live catalog — still the baseline — simply has nothing left to say about it. Not an error,
    and emphatically not a second rename."""
    plan, diag = _plan(_widget(tmp_path), AFTER)

    assert plan.is_empty
    assert diag.warnings == []
    assert render_plan(plan) == "No changes — the catalog already matches the ontology."


def test_neither_column_warns_and_plans_an_ordinary_add(tmp_path):
    """A typo'd `renamedFrom`, or a lake so far behind that it never had the old column either.
    Loom can't rename what isn't there, so it does the only other honest thing — and says so,
    because silently adding here is how the *real* old column ends up stranded."""
    plan, diag = _plan(_widget(tmp_path), {"id": BEFORE["id"]})

    assert _columns(plan) == [("add", "score", Severity.SAFE)]
    warning = " ".join(w.render() for w in diag.warnings)
    assert "renames column 'score' from 'old_score', but 'demo.widgets' has neither" in warning
    assert "planning 'score' as a new column" in warning


def test_both_columns_present_is_breaking_and_says_why(tmp_path):
    """The interesting one. A rename target that already exists is a mistake or a half-finished
    migration, and Loom cannot merge them because merging means dropping one."""
    plan, _ = _plan(_widget(tmp_path), dict(BEFORE, score=AFTER["score"]))

    assert _columns(plan) == [("rename", "score", Severity.BREAKING)]
    reason = plan.changes[0].columns[0].reason
    assert "'old_score' and 'score' both exist in 'demo.widgets'" in reason
    assert "Loom never drops a column, so it cannot merge them" in reason
    assert "move the values across" in reason


def test_both_columns_present_is_a_breaking_change_not_an_aborted_plan(tmp_path):
    """Deliberately routed through `BREAKING` rather than a diagnostic error: the spec is fine, the
    *lake* is in a shape the plan can't resolve. Keeping it a change means the rest of the diff is
    still printed, and `apply` refuses it through the path that already exists."""
    (tmp_path / "other.yaml").write_text(
        """
objectType:
  apiName: Gadget
  primaryKey: id
  title: id
  backing: { catalog: main, table: demo.gadgets }
  properties:
    - { name: id, type: string, column: id, unique: true }
    - { name: note, type: string, column: note, nullable: true }
"""
    )
    ontology = _widget(tmp_path)
    diag = Diagnostics()
    catalog = FakeCatalog(
        {"demo.widgets": dict(BEFORE, score=AFTER["score"]), "demo.gadgets": {"id": BEFORE["id"]}}
    )
    plan = diff_ontology(ontology, {"main": catalog}, diag)

    assert diag.errors == [], "a lake Loom can't resolve is not a broken spec"
    # Both tables still on the page — the unrelated one would be invisible had this aborted.
    assert sorted(t.table for t in plan.changes) == ["demo.gadgets", "demo.widgets"]
    out = render_plan(plan)
    assert "  ! main.demo.widgets" in out
    assert "  ~ main.demo.gadgets" in out
    assert "      + note" in out
    assert "`loom apply` will refuse this plan" in out


def test_a_creation_ignores_renamed_from_silently(tmp_path):
    """A table that doesn't exist has no old column. Warning here would fire on every bootstrap of
    an empty warehouse from a spec that has ever renamed anything."""
    diag = Diagnostics()
    plan = diff_ontology(_widget(tmp_path), {"main": FakeCatalog({})}, diag)

    assert [(c.kind, c.column) for c in plan.changes[0].columns] == [("add", "id"), ("add", "score")]
    assert plan.changes[0].action == "create"
    assert diag.warnings == []


# --- ordering ---------------------------------------------------------------------------------


def test_a_rename_is_emitted_before_the_other_edits_to_its_column(tmp_path):
    """One table, one `alter_table`, so the edits are one ordered list. Everything after the rename
    names the column by its new name, which only exists once the rename has happened — and the
    pyiceberg adapter needs to *see* the rename first to translate those later edits back."""
    # `float` and not `int`: `score` is declared `double`, and Iceberg promotes `float -> double`
    # but not `int -> double`, so an int here would plan as breaking rather than as the promotion
    # this ordering test needs to sit behind the rename.
    live = {"id": BEFORE["id"], "old_score": Column("old_score", "float", required=True, field_id=2)}
    plan, _ = _plan(_widget(tmp_path), live)

    assert _columns(plan) == [
        ("rename", "score", Severity.SAFE),
        ("promote", "score", Severity.PHYSICAL_SAFE),
        ("loosen", "score", Severity.SAFE),
    ]
    # The comparisons behind promote/loosen ran against the *old* column, because that is the one
    # that exists — a rename carries its type and nullability across with its field id.
    promote = plan.changes[0].columns[1]
    assert promote.detail == "float -> double"
    assert "field id 2" in promote.reason


def test_a_rename_with_nothing_else_to_do_is_one_edit(tmp_path):
    plan, _ = _plan(_widget(tmp_path), BEFORE)
    assert len(plan.changes[0].columns) == 1


def test_a_rename_beside_a_breaking_change_drags_the_whole_table_breaking(tmp_path):
    """The rename is safe on its own, and it must not land on its own. `old_score` is a string here,
    which does not promote to the declared double — so the table is breaking, and a breaking plan is
    refused whole. Otherwise a run would rename the column and then decline to finish, leaving a
    table that matches neither the old spec nor the new one."""
    live = {"id": BEFORE["id"], "old_score": Column("old_score", "string", required=False, field_id=2)}
    plan, _ = _plan(_widget(tmp_path), live)

    assert _columns(plan) == [
        ("rename", "score", Severity.SAFE),
        ("retype", "score", Severity.BREAKING),
    ]
    assert plan.severity is Severity.BREAKING


# --- the unmanaged footer ---------------------------------------------------------------------


def test_the_old_column_is_not_reported_as_somebody_elses_data(tmp_path):
    """The bug this slice closes. `old_score` is live, and unmapped in the sense that no property
    names it as its `column` — but it is not unmanaged: the plan is about to rename it."""
    plan, _ = _plan(_widget(tmp_path), BEFORE)

    assert plan.unmanaged == ()
    out = render_plan(plan)
    assert "Unmanaged" not in out
    assert "old_score" in out, "it is still named — on the rename line, where it belongs"


def test_the_old_column_is_not_unmanaged_in_the_breaking_shape_either(tmp_path):
    """Even when Loom refuses the rename, `old_score` is the column the refusal is *about*.
    Listing it under "left untouched" as well would be the same claim twice, and the wrong one."""
    plan, _ = _plan(_widget(tmp_path), dict(BEFORE, score=AFTER["score"]))
    assert plan.unmanaged == ()


def test_a_genuinely_unmapped_column_is_still_unmanaged(tmp_path):
    """The exclusion is exactly the rename sources, not a general amnesty."""
    live = dict(BEFORE, etl_batch=Column("etl_batch", "string", required=False, field_id=9))
    plan, _ = _plan(_widget(tmp_path), live)

    assert [(u.table, u.columns) for u in plan.unmanaged] == [("demo.widgets", ("etl_batch",))]


# --- rendering --------------------------------------------------------------------------------


def test_a_rename_renders_as_a_change_in_place(tmp_path):
    plan, _ = _plan(_widget(tmp_path), BEFORE)
    out = render_plan(plan)

    assert "      ~ score  renamed from old_score  safe" in out
    assert "Plan: 0 to create, 1 to change · 1 safe" in out
    assert "drop" not in out


def test_the_breaking_shape_takes_the_breaking_marker(tmp_path):
    plan, _ = _plan(_widget(tmp_path), dict(BEFORE, score=AFTER["score"]))
    out = render_plan(plan)

    assert "      ! score" in out
    assert "      ~ score" not in out
    assert "Plan: 0 to create, 1 to change · 1 breaking" in out


# --- grammar and validation -------------------------------------------------------------------


def test_renaming_from_its_own_column_is_an_error(tmp_path):
    (tmp_path / "widget.yaml").write_text(WIDGET.format(extra=", renamedFrom: score"))
    messages = _messages(tmp_path)

    assert "'renamedFrom' in property 'score' names its own column 'score'" in messages
    assert "drop the key if the column did not move" in messages


def test_renamed_from_must_be_a_column_name(tmp_path):
    (tmp_path / "widget.yaml").write_text(WIDGET.format(extra=", renamedFrom: []"))
    assert "'renamedFrom' in property 'score' must be a non-empty column name" in _messages(tmp_path)


def test_a_misspelled_key_is_caught_like_any_other(tmp_path):
    (tmp_path / "widget.yaml").write_text(WIDGET.format(extra=", renamedFom: old_score"))
    messages = _messages(tmp_path)

    assert "unexpected key 'renamedFom' in property" in messages
    assert "did you mean 'renamedFrom'?" in messages


def test_a_rename_cannot_take_a_column_another_property_maps(tmp_path):
    """The rule that makes renames independent of one another — and therefore makes per-column edit
    ordering enough. Here it catches an intra-table chain: `score` is both a rename target and
    another property's live column."""
    (tmp_path / "widget.yaml").write_text(
        """
objectType:
  apiName: Widget
  primaryKey: id
  title: id
  backing: { catalog: main, table: demo.widgets }
  properties:
    - { name: id, type: string, column: id, unique: true }
    - { name: score, type: double, column: score, nullable: true }
    - { name: rating, type: double, column: rating, nullable: true, renamedFrom: score }
"""
    )
    messages = _messages(tmp_path)

    assert "property 'rating' renames column 'rating' from 'score', which property 'score' already maps" in messages
    assert "cannot take one another property is live on" in messages


def test_a_swap_is_rejected_by_the_same_rule(tmp_path):
    (tmp_path / "widget.yaml").write_text(
        """
objectType:
  apiName: Widget
  primaryKey: id
  title: id
  backing: { catalog: main, table: demo.widgets }
  properties:
    - { name: id, type: string, column: id, unique: true }
    - { name: a, type: double, column: alpha, nullable: true, renamedFrom: beta }
    - { name: b, type: double, column: beta, nullable: true, renamedFrom: alpha }
"""
    )
    messages = _messages(tmp_path)
    assert "renames column 'alpha' from 'beta'" in messages
    assert "renames column 'beta' from 'alpha'" in messages


def test_one_old_column_cannot_become_two(tmp_path):
    (tmp_path / "widget.yaml").write_text(
        """
objectType:
  apiName: Widget
  primaryKey: id
  title: id
  backing: { catalog: main, table: demo.widgets }
  properties:
    - { name: id, type: string, column: id, unique: true }
    - { name: a, type: double, column: alpha, nullable: true, renamedFrom: old }
    - { name: b, type: double, column: beta, nullable: true, renamedFrom: old }
"""
    )
    messages = _messages(tmp_path)

    assert "property 'a' and property 'b' both rename from column 'old'" in messages
    assert "one column cannot become two" in messages


# --- two declarations over one table ----------------------------------------------------------


def _pair(tmp_path: Path, widget_rename: str, gizmo_rename: str) -> None:
    """Two objectTypes over one `demo.widgets`, each mapping `score`. Modelling a subtype this way
    is normal, so the two have to agree about where `score` came from."""
    for name, api_name, rename in (("widget", "Widget", widget_rename), ("gizmo", "Gizmo", gizmo_rename)):
        (tmp_path / f"{name}.yaml").write_text(
            f"""
objectType:
  apiName: {api_name}
  primaryKey: id
  title: id
  backing: {{ catalog: main, table: demo.widgets }}
  properties:
    - {{ name: id, type: string, column: id, unique: true }}
    - {{ name: score, type: double, column: score, nullable: true{rename} }}
"""
        )


def test_one_declaration_asserting_the_rename_is_enough(tmp_path):
    """Silence is no opinion, not "there was no rename". A subtype written after the rename shipped
    shouldn't have to repeat migration scaffolding to avoid contradicting it."""
    _pair(tmp_path, ", renamedFrom: old_score", "")
    built, _ = build(tmp_path)
    plan, _ = _plan(built, BEFORE)

    assert _columns(plan) == [("rename", "score", Severity.SAFE)]


def test_two_declarations_naming_different_old_columns_is_an_error(tmp_path):
    """A column came from one place. This is the one rename disagreement that can't be reconciled,
    so it is reported next to the type disagreement it resembles."""
    _pair(tmp_path, ", renamedFrom: old_score", ", renamedFrom: legacy_score")
    built, _ = build(tmp_path)
    diag = Diagnostics()
    diff_ontology(built, {"main": FakeCatalog({"demo.widgets": BEFORE})}, diag)

    messages = " | ".join(e.render() for e in diag.errors)
    # Declaration order — sorted filename order for a directory of specs — so the message is the
    # same on every run, like every other line of a plan CI is going to diff.
    assert "'Gizmo.score' and 'Widget.score' both map column 'score' of 'demo.widgets'" in messages
    assert "disagree on where it was renamed from ('legacy_score' vs 'old_score')" in messages
    assert "drop the 'renamedFrom' that is wrong" in messages


def test_the_table_wide_rules_catch_what_one_declaration_cannot_see(tmp_path):
    """`Gizmo.tally` renames from a column that `Widget.score` is live on. Neither objectType is
    wrong on its own — which is exactly why the rules are applied a second time over the whole
    table, where the planner is the only thing with both declarations in scope."""
    _pair(tmp_path, "", "")
    # Gizmo drops its own `score` mapping and renames a different column from it. Widget still maps
    # `score`, so the collision only exists once both are laid over the one table.
    (tmp_path / "gizmo.yaml").write_text(
        (tmp_path / "gizmo.yaml")
        .read_text()
        .replace(
            "    - { name: score, type: double, column: score, nullable: true }",
            "    - { name: tally, type: double, column: tally, nullable: true, renamedFrom: score }",
        )
    )
    built, _ = build(tmp_path)  # neither declaration is wrong on its own
    diag = Diagnostics()
    diff_ontology(built, {"main": FakeCatalog({"demo.widgets": BEFORE})}, diag)

    messages = " | ".join(e.render() for e in diag.errors)
    assert (
        "table 'demo.widgets': 'Gizmo.tally' renames column 'tally' from 'score', which "
        "'Widget.score' already maps" in messages
    )


# --- linkType through tables ------------------------------------------------------------------


def _enrolments(tmp_path: Path, rename: str = "") -> None:
    (tmp_path / "student.yaml").write_text(
        """
objectType:
  apiName: Student
  primaryKey: id
  title: id
  backing: { catalog: main, table: edu.students }
  properties:
    - { name: id, type: string, column: id, unique: true }
"""
    )
    (tmp_path / "course.yaml").write_text(
        """
objectType:
  apiName: Course
  primaryKey: code
  title: code
  backing: { catalog: main, table: edu.courses }
  properties:
    - { name: code, type: string, column: code, unique: true }
"""
    )
    (tmp_path / "enrolments.yaml").write_text(
        f"""
linkType:
  apiName: enrolments
  cardinality: many_to_many
  from: {{ objectType: Student, property: id }}
  to: {{ objectType: Course, property: code }}
  through:
    catalog: main
    table: edu.enrolments
    fromColumn: student_id
    toColumn: course_code{rename}
"""
    )


def test_a_through_column_renames_like_any_other(tmp_path):
    """A mapping table is planned by the same machinery as a backing table, so leaving it out would
    make it the one physical table Loom plans but cannot rename a column on."""
    _enrolments(tmp_path, "\n    renamedFrom: { fromColumn: student_ref }")
    built, _ = build(tmp_path)
    live = {
        "edu.students": {"id": Column("id", "string", required=True, field_id=1)},
        "edu.courses": {"code": Column("code", "string", required=True, field_id=1)},
        "edu.enrolments": {
            "student_ref": Column("student_ref", "string", required=True, field_id=1),
            "course_code": Column("course_code", "string", required=True, field_id=2),
        },
    }
    diag = Diagnostics()
    plan = diff_ontology(built, {"main": FakeCatalog(live)}, diag)

    assert diag.errors == []
    assert _columns(plan) == [("rename", "student_id", Severity.SAFE)]
    assert plan.unmanaged == (), "the renamed-from column is not someone else's data here either"


def test_an_unknown_key_under_through_renamed_from_is_an_error(tmp_path):
    _enrolments(tmp_path, "\n    renamedFrom: { fromColum: student_ref }")
    messages = _messages(tmp_path)

    assert "unexpected key 'fromColum' in through.renamedFrom" in messages
    assert "did you mean 'fromColumn'?" in messages


def test_a_through_side_cannot_rename_from_the_other_side(tmp_path):
    _enrolments(tmp_path, "\n    renamedFrom: { fromColumn: course_code }")
    messages = _messages(tmp_path)

    assert "through.fromColumn renames column 'student_id' from 'course_code'" in messages


def test_a_through_rename_of_its_own_column_is_an_error(tmp_path):
    _enrolments(tmp_path, "\n    renamedFrom: { toColumn: course_code }")
    assert "'renamedFrom' in through.toColumn names its own column 'course_code'" in _messages(tmp_path)


# --- validate --physical ----------------------------------------------------------------------


def test_a_pending_rename_is_still_a_physical_mismatch_but_says_so(tmp_path):
    """`validate --physical` asks whether the spec matches the lake *right now*, and a spec with a
    rename still to run does not: the read path would select `score` off a table that hasn't got
    one. So it stays an error — but the reason is a migration nobody has run, not a typo."""
    _widget(tmp_path)
    diag = Diagnostics()
    loaded = load_dir(tmp_path, diag)
    check_physical(loaded, {"main": FakeCatalog({"demo.widgets": BEFORE})}, diag)

    rendered = " | ".join(e.render() for e in diag.errors)
    assert "property 'score' maps to column 'score', which does not exist in 'demo.widgets'" in rendered
    assert "its 'renamedFrom' column 'old_score' is still there" in rendered
    assert "run `loom apply` to perform the rename" in rendered


def test_once_the_rename_has_landed_physical_validation_is_clean(tmp_path):
    """And the key stays in the spec while that is true — it is never `validate`'s business to tell
    you to delete it."""
    _widget(tmp_path)
    diag = Diagnostics()
    loaded = load_dir(tmp_path, diag)
    check_physical(loaded, {"main": FakeCatalog({"demo.widgets": AFTER})}, diag)

    assert diag.errors == []
    assert diag.warnings == []


# --- the shape of the key itself ---------------------------------------------------------------


def test_a_chain_is_not_expressible(tmp_path):
    """`renamedFrom` is one hop, deliberately: a list would let a, b and c be live at once, and the
    refusal would then have to explain which *pair* it can't merge. A lake that skipped the middle
    apply lands in the warned add-a-new-column shape instead, which is at least loud."""
    (tmp_path / "widget.yaml").write_text(WIDGET.format(extra=", renamedFrom: [b, a]"))
    assert "'renamedFrom' in property 'score' must be a non-empty column name" in _messages(tmp_path)


def test_the_key_survives_its_own_migration(tmp_path):
    """Lifetime, stated as a test: the same spec plans the rename against a lake that needs it and
    nothing at all against one that has had it. That is why Loom never suggests removing the key —
    one file is deployed to lakes at different versions, and it is right about both."""
    ontology = _widget(tmp_path)

    before, before_diag = _plan(ontology, BEFORE)
    after, after_diag = _plan(ontology, AFTER)

    assert _columns(before) == [("rename", "score", Severity.SAFE)]
    assert after.is_empty
    assert before_diag.warnings == after_diag.warnings == []


@pytest.mark.parametrize("live", [BEFORE, AFTER])
def test_planning_twice_is_stable(tmp_path, live):
    """Whatever shape the lake is in, a second plan against an unchanged lake says the same thing —
    the diff is a pure function of (spec, catalog), with nothing accumulated between runs."""
    ontology = _widget(tmp_path)
    assert _columns(_plan(ontology, live)[0]) == _columns(_plan(ontology, live)[0])
