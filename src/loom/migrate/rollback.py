"""`loom rollback` — the spec `_loom_meta` recorded, put back.

Deliberately a thin shell over the ordinary loop rather than a second engine: it restores a spec
out of history, re-plans it against the live catalog, and hands that plan to the same executor
under the same whole-plan refusal. It introduces no change kind and no write op that `apply`
doesn't already have.

It departs from "write the old YAML back and re-plan" in exactly one place, and that place is
renames. `renamedFrom` points *forward*, so a spec from before a rename cannot name the column it
has to be renamed back from: a naive re-plan would add `lifetime_value` and strand `ltv_usd`,
which is precisely the failure `renamedFrom` exists to prevent. The information is already in the
history Loom itself wrote, so rollback reads it, composes the chain, inverts it, and overlays it
onto the restored spec's desired columns (`schema.apply_renames`). Nothing is written back into
the YAML — the overlay lives only in the plan.

What it cannot reverse it says, rather than fakes. Of the four ops the write port has, exactly one
reverses within the port:

- An **add** reverses to a drop, and Loom never drops. The column stays live and the restored spec
  no longer maps it, which is to say it is unmanaged from here on. `left_behind` names it, because
  the honest report is worth nothing if it has to be discovered. A table created since the target
  version is the same answer one level up.
- A **promotion** reverses to a narrowing, a **loosening** to a tightening, and both are breaking —
  so the plan is refused whole by the rule that already exists. That is not a hole in rollback. Once
  `total` is a `long`, the spec that says `int` no longer describes this lake, and the way out is
  forward rather than back.

**DDL only.** `apply` never wrote a row, so rollback never deletes one. It touches no snapshot and
expires nothing: rows written since the version being restored are nobody's to throw away.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .diff import MigrationPlan
from .meta import STATUS_APPLIED, AppliedRecord, SpecSnapshot, spec_files
from .schema import DesiredTable, TableKey

if TYPE_CHECKING:
    from ..catalog.base import Catalog

History = Mapping[str, Sequence[AppliedRecord]]
"""Every bound catalog's `_loom_meta`, keyed by catalog name. Read once, passed around."""


class RollbackError(RuntimeError):
    """Rollback cannot get as far as a plan: no such version, catalogs that disagree about what a
    version was, nothing behind the current one.

    Distinct from a refusal, which is a plan Loom read in full and would not run."""


@dataclass(frozen=True)
class RollbackTarget:
    """The version being restored, and everything about it that the plan then needs."""

    version: int
    snapshot: SpecSnapshot
    renames: Mapping[TableKey, Mapping[str, str]]
    """Per table, `{the column the restored spec names: the column that is live now}` — the
    reverse of every rename applied since, composed across versions."""
    held_by: tuple[str, ...]
    """The catalogs whose history holds a row at this version. They agree on its `content_hash` by
    construction, and `resolve_target` refuses if they don't."""
    absent_from: tuple[str, ...]
    """Catalogs configured now with no history at or before this version. Named in the report
    rather than refused or skipped: the restored spec either binds a catalog or it doesn't, and
    either way it is planned like every other one — this only says its own history has nothing
    from that far back to be compared against."""
    status: str
    """The recorded row's status. `partial` is restorable and worth saying out loud."""


@dataclass(frozen=True)
class LeftBehind:
    """A live column, or a whole table, that the rollback will not take back.

    Mirrors `diff.Unmanaged` on purpose — after the rollback that is exactly what these are. The
    difference is which spec last claimed it: an ordinary unmanaged column is one no version of this
    ontology ever mapped, and one of these is mapped by the spec the lake is at now and not by the
    one it is going back to.

    **That is narrower than "a column Loom added", which is what this used to say.** The set is a
    spec diff and nothing else, so a column that was already live and was merely *adopted* by a later
    version — `region` in the retail example, which `loom.yaml`'s own comment says is filled by
    something that is not Loom — lands in it having never been created by an apply. Told it was
    "added after version N", an operator tidying up after a rollback is pointed at somebody else's
    column. The provenance that would separate the two is in `_loom_meta.applied.summary`, and only
    as the plan's display prose; §9 says a rollback must not parse that, so the wording says what a
    spec diff can establish and stops there."""

    catalog: str
    table: str
    columns: tuple[str, ...] = ()
    whole_table: bool = False


@dataclass(frozen=True)
class FileChanges:
    written: tuple[str, ...]  # relative posix paths, as `SpecSnapshot.files` keys them
    deleted: tuple[str, ...]

    @property
    def any(self) -> bool:
        return bool(self.written or self.deleted)


def latest_version(history: History) -> int:
    """The highest version any bound catalog has recorded, or 0 if none has recorded anything."""
    return max((r.version for rows in history.values() for r in rows), default=0)


def resolve_target(history: History, version: int | None) -> RollbackTarget:
    """Which spec `--to version` names, read out of the history of whichever catalog holds it.

    `version` selects a *spec*, not a per-catalog target, and that is what makes the multi-catalog
    case tractable. A version whose text differs from the one before it makes every bound catalog
    stale, so every one of them records a row for it — which means a catalog with no row at version
    5 is a catalog whose text did not change at 5, and it is therefore already at that spec. There
    is exactly one thing to restore and every catalog is re-planned against it.

    With no `version`, the target is the highest recorded version strictly below the current one:
    one step back, which is the shape of "that apply went wrong"."""
    versions = sorted({r.version for rows in history.values() for r in rows})
    if not versions:
        raise RollbackError("nothing has been applied here — there is no history to roll back to")
    if version is None:
        earlier = [v for v in versions if v < versions[-1]]
        if not earlier:
            raise RollbackError(
                f"version {versions[-1]} is the only recorded apply — there is nothing behind it"
            )
        version = earlier[-1]

    held = {
        name: [r for r in rows if r.version == version][-1]
        for name, rows in history.items()
        if any(r.version == version for r in rows)
    }
    if not held:
        listed = ", ".join(str(v) for v in versions)
        raise RollbackError(f"no catalog has a version {version} — recorded versions are {listed}")
    if len({record.content_hash for record in held.values()}) > 1:
        disagreement = ", ".join(
            f"{name} recorded {record.content_hash[:12]}" for name, record in sorted(held.items())
        )
        raise RollbackError(
            f"the catalogs disagree about what version {version} was ({disagreement}) — one of "
            f"them was written outside Loom, and there is no single spec to restore"
        )

    record = held[sorted(held)[0]]
    return RollbackTarget(
        version=version,
        snapshot=_snapshot(record),
        renames=_reverse_renames(history, version),
        held_by=tuple(sorted(held)),
        absent_from=tuple(
            sorted(
                name
                for name, rows in history.items()
                if not any(r.version <= version for r in rows)
            )
        ),
        status=record.status or STATUS_APPLIED,
    )


def left_behind(
    plan: MigrationPlan,
    recorded: Mapping[TableKey, DesiredTable],
    restored: Mapping[TableKey, DesiredTable],
    catalogs: Mapping[str, Catalog],
) -> tuple[LeftBehind, ...]:
    """What this rollback adds to the lake's unmanaged surface, computed rather than guessed.

    `recorded` is the desired state of the spec the lake is at *now* and `restored` of the one it
    is going back to — both out of `_loom_meta`, not off disk, so this works when the YAML in the
    working tree is the thing that no longer parses. A column that the current spec maps, that is
    live, and that the restored spec does not map, is a column this rollback strands."""
    out: list[LeftBehind] = []
    for entry in plan.unmanaged:
        mapped = recorded.get((entry.catalog, entry.table))
        columns = tuple(c for c in entry.columns if mapped is not None and c in mapped.columns)
        if columns:
            out.append(LeftBehind(entry.catalog, entry.table, columns))
    for key, table in recorded.items():
        catalog = catalogs.get(table.catalog)
        if key not in restored and catalog is not None and catalog.table_exists(table.table):
            out.append(LeftBehind(table.catalog, table.table, whole_table=True))
    return tuple(out)


def file_changes(root: Path, snapshot: SpecSnapshot) -> FileChanges:
    """What restoring `snapshot` into `root` would do, worked out before anything does it.

    The delete half is the part worth justifying. A spec file added after the version being
    restored is not in the snapshot, and leaving it in place would make the "restored" spec the old
    one *plus* whatever came after — which is not the spec that was recorded, and so not a
    rollback. Unlike a column, a spec file is the user's own and version-controlled, and it is named
    before the prompt rather than removed quietly.

    The scope is exactly what `spec_files` claims and no wider: `*.yaml`/`*.yml` under the ontology
    directory, never `loom.yaml`, never a file of any other kind."""
    current = spec_files(root)
    written = tuple(
        name
        for name, text in sorted(snapshot.files.items())
        if name not in current or current[name].read_text() != text
    )
    return FileChanges(written, tuple(sorted(set(current) - set(snapshot.files))))


def restore_files(root: Path, snapshot: SpecSnapshot, changes: FileChanges) -> None:
    """Write the spec back. The last thing a rollback does, and only once the DDL has — so a
    rollback that was refused, or declined at the prompt, leaves the working tree untouched."""
    for name in changes.written:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(snapshot.files[name])
    for name in changes.deleted:
        (root / name).unlink()


def materialize(snapshot: SpecSnapshot, root: Path) -> Path:
    """Write a recorded spec somewhere it can be parsed and planned — which is deliberately not the
    working tree. The plan has to be printable, and refusable, before a file the user has open is
    touched."""
    for name, text in snapshot.files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return root


def _snapshot(record: AppliedRecord) -> SpecSnapshot:
    try:
        files = json.loads(record.spec) if record.spec else None
    except json.JSONDecodeError as e:
        raise RollbackError(f"version {record.version} did not record a readable spec: {e}") from e
    if not isinstance(files, dict) or not files:
        raise RollbackError(f"version {record.version} recorded no spec source — nothing to restore")
    return SpecSnapshot(files=files, content_hash=record.content_hash)


def _reverse_renames(history: History, version: int) -> dict[TableKey, dict[str, str]]:
    """Every rename applied after `version`, composed and then inverted.

    Composed because renames chain across applies even though a single spec cannot express a chain:
    `a -> b` at version 2 and `b -> c` at version 3 mean the column the version-1 spec calls `a` is
    called `c` in the lake today. Inverted because the restored spec names the *old* column, and
    what the plan needs is the live one to rename it back from.

    A rename that composes back to where it started — a rollback of a rollback — drops out here
    rather than reaching the planner as a column renamed from itself."""
    chains: dict[TableKey, dict[str, str]] = {}
    for catalog, rows in history.items():
        for record in sorted(rows, key=lambda r: r.version):
            if record.version <= version:
                continue
            for entry in _table_entries(record):
                renames = entry.get("renames")
                key = _table_key(catalog, str(entry.get("table", "")))
                if not isinstance(renames, dict) or key is None:
                    continue
                chain = chains.setdefault(key, {})
                for new, old in renames.items():
                    origin = next((o for o, current in chain.items() if current == old), old)
                    chain[origin] = new
    return {
        key: settled
        for key, chain in chains.items()
        if (settled := {old: new for old, new in chain.items() if old != new})
    }


def _table_entries(record: AppliedRecord) -> list[dict]:
    """The per-table entries of a history row, whichever shape it carries: `apply` records a list,
    `rollback` records `{"rollback_of": n, "tables": [...]}`."""
    data = record.summary_data()
    if isinstance(data, dict):
        data = data.get("tables")
    return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []


def _table_key(catalog: str, qualified: str) -> TableKey | None:
    """`summary` qualifies each table with the catalog that recorded it; the planner keys tables by
    the pair. A row that doesn't carry the prefix was written by something that isn't this Loom."""
    prefix = f"{catalog}."
    return (catalog, qualified[len(prefix) :]) if qualified.startswith(prefix) else None
