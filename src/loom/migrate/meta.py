"""`_loom_meta` — what `apply` did, recorded in the lake it did it to.

An append-only Iceberg table per catalog. The current state is the row with the highest `version`;
every earlier row is the history. It holds the *source* of the spec that was applied, not a
rendering of the model built from it, because restoring a spec is what a rollback does and a model
is not a thing you can restore into an editor.

Three decisions worth stating, because each had an obvious-looking alternative:

- **In the lake, not beside the YAML.** A state file in the repo describes the checkout it lives
  in, and `apply` is run from many checkouts and from CI. The catalog is the only thing every
  operator of a table already shares.
- **A row per catalog, but one version number.** The row sits next to the data it describes, so a
  lake outlives the project directory that produced it — while `version` counts applies of the
  *spec*, not of a catalog, so "version 7" names the same apply wherever you read it. There is no
  central place to keep that counter, so it is derived: one past the highest version any bound
  catalog holds. A catalog added to the project at version 7 therefore starts its history at 7
  rather than at 1, which is the point.
- **It is not the planner's input.** The diff is taken against the live catalog (see the package
  docstring). This table records history and answers "has this exact spec already been applied
  here?" — it never gets to claim a column exists that isn't there.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..catalog.base import Column
from ..config import CONFIG_FILENAME

if TYPE_CHECKING:
    from ..catalog.base import Catalog, CatalogWriter

META_NAMESPACE = "_loom_meta"
META_TABLE = f"{META_NAMESPACE}.applied"

STATUS_APPLIED = "applied"
STATUS_PARTIAL = "partial"

# Every column optional but `version` and `applied_at`: this table is written by exactly one
# writer (Loom) and read by anything with an Iceberg client, so the cost of a required column is
# a future field that can never be added to an existing history. The two that stay required are
# the ones an empty value would make the row meaningless.
META_COLUMNS: tuple[Column, ...] = (
    Column("version", "long", required=True),
    Column("applied_at", "timestamptz", required=True),
    Column("content_hash", "string", required=False),
    Column("spec", "string", required=False),
    Column("summary", "string", required=False),
    Column("status", "string", required=False),
    Column("loom_version", "string", required=False),
    Column("actor", "string", required=False),
)

_SPEC_SUFFIXES = (".yaml", ".yml")


@dataclass(frozen=True)
class SpecSnapshot:
    """The ontology's source files and their fingerprint.

    `loom.yaml` is deliberately absent: it is deployment config — which warehouse, which engine —
    and the same spec applied against staging and production must produce the same hash, or the
    "already applied" check answers a question nobody asked."""

    files: Mapping[str, str]  # relative posix path -> file text
    content_hash: str

    def serialize(self) -> str:
        """The exact bytes that were hashed. Stored verbatim so a rollback can write the files
        back out without the source directory still being around."""
        return _canonical(self.files)


def snapshot_spec(path: str | Path) -> SpecSnapshot:
    root = Path(path)
    files = {
        p.relative_to(root).as_posix(): p.read_text()
        for p in sorted(root.rglob("*"))
        # `find_config` will accept a loom.yaml *inside* the ontology directory, so excluding it
        # has to be by name rather than by where it sits.
        if p.is_file() and p.suffix in _SPEC_SUFFIXES and p.name != CONFIG_FILENAME
    }
    payload = _canonical(files)
    return SpecSnapshot(files=files, content_hash=hashlib.sha256(payload.encode()).hexdigest())


def _canonical(files: Mapping[str, str]) -> str:
    """Sorted keys, no incidental whitespace: two checkouts of the same commit must hash alike
    regardless of the order the filesystem happened to hand the files over in."""
    return json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class AppliedRecord:
    version: int
    applied_at: datetime
    content_hash: str
    spec: str = ""
    summary: str = ""
    status: str = STATUS_APPLIED
    loom_version: str = ""
    actor: str = ""

    def row(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "applied_at": self.applied_at,
            "content_hash": self.content_hash,
            "spec": self.spec,
            "summary": self.summary,
            "status": self.status,
            "loom_version": self.loom_version,
            "actor": self.actor,
        }

    def summary_data(self) -> Any:
        """The `summary` column parsed back. Tolerates junk: a history row written by a future
        version of Loom should not stop this one from reading the version number next to it."""
        try:
            return json.loads(self.summary) if self.summary else None
        except json.JSONDecodeError:
            return None


@dataclass
class MetaStore:
    """The `_loom_meta` table of one catalog, created on first write.

    Reads go through the read port and writes through the write port — the same object implements
    both here, but keeping the two references distinct means the read half stays usable (for
    `loom plan`, or a future `loom history`) against a catalog nobody can write to."""

    catalog: Catalog
    writer: CatalogWriter | None = None

    def history(self) -> tuple[AppliedRecord, ...]:
        """Every recorded apply, oldest first. Empty if the table was never created."""
        if not self.catalog.table_exists(META_TABLE):
            return ()
        rows = self.catalog.scan(META_TABLE)
        # `.to_pylist()` rather than an import: `scan` is documented to return a pyarrow Table, and
        # nothing above the catalog port should need pyarrow on its import path to read one.
        records = [_record(r) for r in rows.to_pylist()]
        return tuple(sorted(records, key=lambda r: r.version))

    def latest(self) -> AppliedRecord | None:
        history = self.history()
        return history[-1] if history else None

    def current_version(self) -> int:
        """The highest version recorded here, or 0 for a catalog with no history.

        Only ever half the answer: the version an apply writes is global to the spec, so the
        executor takes the maximum of this across every catalog the spec binds. It is read *before*
        the DDL runs, because each managed table is stamped with the version in the same
        transaction as its schema change — a number assigned afterwards could only be stamped by a
        second write, which is the drift the stamp exists to rule out."""
        previous = self.latest()
        return previous.version if previous else 0

    def record(
        self,
        snapshot: SpecSnapshot,
        summary: Any,
        version: int,
        status: str = STATUS_APPLIED,
        actor: str | None = None,
        now: datetime | None = None,
    ) -> AppliedRecord:
        """Append one history row, creating the meta table if this is the catalog's first apply."""
        if self.writer is None:  # pragma: no cover - executor resolves a writer before calling
            raise RuntimeError("MetaStore has no writer — nothing can be recorded")
        entry = AppliedRecord(
            version=version,
            applied_at=now or datetime.now(UTC),
            content_hash=snapshot.content_hash,
            spec=snapshot.serialize(),
            summary=json.dumps(summary, sort_keys=True, separators=(",", ":")),
            status=status,
            loom_version=loom_version(),
            actor=actor or default_actor(),
        )
        self._ensure_table()
        self.writer.append_rows(META_TABLE, [entry.row()])
        return entry

    def _ensure_table(self) -> None:
        if self.catalog.table_exists(META_TABLE):
            return
        assert self.writer is not None
        self.writer.ensure_namespace(META_TABLE)
        self.writer.create_table(META_TABLE, META_COLUMNS, properties={"loom.managed": "true"})


def _record(row: Mapping[str, Any]) -> AppliedRecord:
    return AppliedRecord(
        version=int(row.get("version") or 0),
        applied_at=row.get("applied_at"),
        content_hash=str(row.get("content_hash") or ""),
        spec=str(row.get("spec") or ""),
        summary=str(row.get("summary") or ""),
        status=str(row.get("status") or ""),
        loom_version=str(row.get("loom_version") or ""),
        actor=str(row.get("actor") or ""),
    )


def loom_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("loom-ontology")
    except PackageNotFoundError:  # pragma: no cover - only when running from a source tree
        return "unknown"


def default_actor() -> str:
    """Who ran it. `LOOM_ACTOR` first, so CI can name the pipeline rather than record `runner`."""
    override = os.environ.get("LOOM_ACTOR")
    if override:
        return override
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - no passwd entry (some containers)
        return "unknown"


def table_properties(snapshot: SpecSnapshot, version_number: int) -> dict[str, str]:
    """Provenance stamped on each managed table, in the same transaction as its schema change.

    Duplicates what `_loom_meta` records, on purpose: someone looking at one table in any Iceberg
    client should be able to see that Loom manages it and which spec it is at, without knowing
    that a meta table exists."""
    return {
        "loom.managed": "true",
        "loom.spec_hash": snapshot.content_hash,
        "loom.applied_version": str(version_number),
    }
