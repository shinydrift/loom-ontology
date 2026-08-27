"""`loom rollback` against a real Iceberg catalog — M2's last definition of done.

The fake catalog proves the policy. This proves the two claims only a metastore can settle: that a
reversed rename really is the *same column* coming back — same field id, same rows, nothing
rewritten — and that a rolled-back add really does stay live rather than quietly vanishing.

Driven through `loom.cli.main` rather than the library, because the parts of a rollback that are
easiest to get wrong are the ones either side of the executor: which version it resolved, and what
it did to files on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from loom.cli import main
from loom.migrate import MetaStore

# The same helpers `test_apply_iceberg` uses to build a populated `hr.people` outside the example
# spec — a rollback needs real rows to survive, and there is one right way to put them there. The
# `project` fixture they go with is in conftest.py.
from test_apply_iceberg import _local, _people_table, _person_spec

pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")

HEADCOUNT = "    - { name: headcount, type: int, column: headcount, nullable: true }"
RENAMED_PLUS_REGION = (
    "    - { name: headcount, type: int, column: staff_count, nullable: true, renamedFrom: headcount }\n"
    "    - { name: region, type: string, column: region, nullable: true }"
)


def test_a_rollback_reverses_the_rename_and_leaves_the_add(project, capsys):
    """The whole slice in one run. Version 2 renamed a column and added another; rolling back to 1
    puts the first one back and cannot put the second one back, and says which is which.

    The rename is the half that doesn't come free: the version-1 spec says `column: headcount` and
    carries no `renamedFrom`, because the key points forward. A plain re-plan of it would add
    `headcount` beside a full `staff_count`. What makes this a rename instead is `_loom_meta` —
    version 2 recorded what it renamed, and rollback inverts it."""
    target, _, config = project
    impl = _people_table(config)
    ontology = target / "ontology"

    _person_spec(target, HEADCOUNT)
    version_1 = (ontology / "person.yaml").read_text()
    assert main(["apply", str(ontology), "--yes"]) == 0

    _person_spec(target, RENAMED_PLUS_REGION)
    assert main(["apply", str(ontology), "--yes"]) == 0
    migrated = _local(config).describe("hr.people")
    assert set(migrated.columns) == {"id", "staff_count", "region"}
    capsys.readouterr()

    assert main(["rollback", str(ontology), "--to", "1", "--yes"]) == 0
    out = capsys.readouterr().out

    after = _local(config).describe("hr.people")
    assert "staff_count" not in after.columns
    # The same field id it had before the rename ever happened, which is what makes every data file
    # written under either name still readable — a rename out and back is not two copies.
    assert after.columns["headcount"].field_id == migrated.columns["staff_count"].field_id == 2
    assert impl.load_table("hr.people").scan().to_arrow().to_pylist() == [
        {"id": "p1", "headcount": 7, "region": None},
        {"id": "p2", "headcount": 9, "region": None},
    ]
    # Reversing an add means dropping, and Loom never drops — so `region` stays, unmapped by the
    # restored spec, and the report names it rather than leaving it to be found later.
    assert "region" in after.columns
    assert "region — mapped by the spec you are leaving, not by version 1" in out
    assert "Rows are untouched" in out
    # And the spec is back, byte for byte — this is what `_loom_meta.spec` is stored verbatim for.
    assert (ontology / "person.yaml").read_text() == version_1


def test_the_rollback_is_a_new_row_the_next_apply_believes(project, capsys):
    """Append-only: a rollback is version 3, not the deletion of version 2. It carries version 1's
    text and hash and records `applied`, which is what makes the *next* run see a spec that is
    already live and write nothing at all."""
    target, _, config = project
    _people_table(config)
    ontology = target / "ontology"
    _person_spec(target, HEADCOUNT)
    assert main(["apply", str(ontology), "--yes"]) == 0
    _person_spec(target, RENAMED_PLUS_REGION)
    assert main(["apply", str(ontology), "--yes"]) == 0
    assert main(["rollback", str(ontology), "--to", "1", "--yes"]) == 0
    capsys.readouterr()

    history = MetaStore(_local(config)).history()
    assert [r.version for r in history] == [1, 2, 3]
    assert history[-1].content_hash == history[0].content_hash
    assert history[-1].spec == history[0].spec
    assert history[-1].status == "applied"
    assert history[-1].summary_data()["rollback_of"] == 1
    assert [e["table"] for e in history[-1].summary_data()["tables"]] == ["local.hr.people"]

    assert main(["apply", str(ontology), "--yes"]) == 0
    assert "Already applied — nothing to do." in capsys.readouterr().out
    assert [r.version for r in MetaStore(_local(config)).history()] == [1, 2, 3]


def test_rolling_back_a_promotion_is_refused_and_writes_nothing(project, capsys):
    """A promotion reverses to a narrowing, and Iceberg will not narrow a column that has rows in
    it. So the rollback goes through the same whole-plan refusal as any other breaking change — and
    the working tree is left alone too, which is the property the ordering exists for."""
    target, _, config = project
    _people_table(config)
    ontology = target / "ontology"
    _person_spec(target, HEADCOUNT)
    assert main(["apply", str(ontology), "--yes"]) == 0
    _person_spec(target, "    - { name: headcount, type: long, column: headcount, nullable: true }")
    assert main(["apply", str(ontology), "--yes"]) == 0
    on_disk = (ontology / "person.yaml").read_text()
    capsys.readouterr()

    assert main(["rollback", str(ontology), "--to", "1", "--yes"]) == 1
    captured = capsys.readouterr()

    assert "long does not promote to int" in captured.out
    assert "no spec file was written either" in captured.err
    assert _local(config).describe("hr.people").columns["headcount"].iceberg_type == "long"
    assert (ontology / "person.yaml").read_text() == on_disk, "the file the user has open, untouched"
    assert [r.version for r in MetaStore(_local(config)).history()] == [1, 2]


def test_a_spec_file_added_since_is_deleted_and_its_table_left_whole(project, capsys):
    """The file-level version of the drop question, and it gets the opposite answer to the column
    one. A `person.yaml` left in place would make the restored spec the old one *plus* whatever came
    after, which is not the spec that was recorded — so the file goes, named before the prompt. The
    table it described does not: that holds data."""
    target, _, config = project
    ontology = target / "ontology"
    assert main(["apply", str(ontology), "--yes"]) == 0
    _people_table(config)
    _person_spec(target, HEADCOUNT)
    assert main(["apply", str(ontology), "--yes"]) == 0
    capsys.readouterr()

    # No `--to`: one step back, which is the shape of "that last apply went wrong".
    assert main(["rollback", str(ontology), "--yes"]) == 0
    out = capsys.readouterr().out

    assert "- person.yaml — to delete; it did not exist at that version" in out
    assert not (ontology / "person.yaml").exists()
    assert _local(config).table_exists("hr.people")
    assert "local.hr.people — the whole table, mapped by the spec you are leaving and not by version 1" in out


def test_rollback_needs_a_config_and_a_history_like_everything_else(tmp_path, capsys):
    """It reads `loom.yaml` and nothing else off disk — deliberately not the spec, which is often
    the thing that no longer parses when someone reaches for this."""
    assert main(["rollback", str(Path(__file__).parent / "fixtures" / "valid")]) == 1
    assert "no loom.yaml found" in capsys.readouterr().err
