"""The sidecar — `_loom_meta.vectors__<type>`, and the definition of *stale*.

**Loom's own data, in Loom's own namespace, and not a column in anybody's table.** Three arguments
compound and each is independently sufficient:

- `ALL_KINDS` has no `array`, so a vector cannot be a declared property at all — and widening the
  type system to admit one would make a spec able to *hand Loom a vector*, which is the whole thing
  `semantic:` exists not to be.
- As an undeclared column it would make `loom plan` report Loom's own data as somebody else's,
  permanently. `migrate.diff.Unmanaged` says *this column is not mine*; an in-table vector would
  invert that sentence for the one column Loom is most responsible for.
- `ActionRuntime._read` carries unmapped columns across a modify. So a `run_` that changed the
  embedded text would write **the old vector back beside the new text in the same commit** —
  internally consistent by construction, with nothing left to compare against. That is the one kind
  of staleness that cannot be detected, and this layout is what makes it impossible instead of
  merely unlikely.

**One table per type, because `key` is a join column.** A single global sidecar would need a string
encoding of every primary key and a cast on every ranked query, which is a per-call cost paid to save
a table nobody looks at.

**The sidecar holds only facts about the row it is keyed to.** No source text: that would be a
governed copy sitting outside the table governance is written against, and it would give
`forgetCustomer` a second place to reach. No denormalised link columns: they would optimise the join,
but the cost of a ranked query is the distance computation over the survivors rather than the join,
and they buy a staleness axis `source_hash` structurally cannot see — one customer changing tier
would invalidate the vector row of every order they ever placed — plus a governance hole, since a
denormalised column is not an `ir.TableRef` and no policy rides on it.

**Staleness is defined here, once.** A row *is embedded* iff the sidecar holds a row for its key whose
`source_hash` equals the hash of the text that row has **now** — where the hash covers everything
`ir.VectorRef`'s comparability guard will compare it on: the text, the model, the width and the
property. Fewer than all four and the guard can refuse a row the reconcile calls current, which is
the one disagreement that shows an operator a working deployment and an empty ranking. Nothing is time-based, nothing is
compared against a snapshot id, and there is no threshold — the ROADMAP refuses one permanently,
because any number is a magic one. `embedded_at` is recorded and is never consulted to decide
freshness; it exists to answer *as of when* in a result envelope.

**What `delete_vectors` owes the erasure slice.** An embedding is not a fingerprint. It is a lossy,
partially invertible copy, and inversion works best on exactly the short text worth embedding. So a
row erased from its table leaves recoverable text in its sidecar until a reconcile prunes it —
unreachable through `match_`, since the join to a deleted row returns nothing, but readable by anyone
with warehouse access, and Loom is what put it there. `EmbedRuntime.reconcile`'s orphan prune is
therefore **the** vector erasure path in this milestone, and its lag is exactly the interval between
reconciles. The slice that builds a general erasure command inherits that sentence rather than
discovering it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..catalog.base import VECTOR_KEY_COLUMN, Column, vector_table

if TYPE_CHECKING:
    from ..catalog.base import Catalog, VectorWriter

VECTOR_COLUMN = "vector"
HASH_COLUMN = "source_hash"
MODEL_COLUMN = "model"
DIMS_COLUMN = "dims"
PROPERTY_COLUMN = "property"
EMBEDDED_AT_COLUMN = "embedded_at"


def vector_columns(key_type: str) -> tuple[Column, ...]:
    """This type's sidecar schema. `key_type` is the object's own primary-key spelling.

    Generous with optional columns for `LOAD_COLUMNS`' reason, restated because it is easy to lose:
    these tables are only ever *created*, never altered, so a column left out today can never reach a
    sidecar that already exists. Only the two that would make the row meaningless are required.

    **`model` is a column, and the ROADMAP predicted it would not need to be.** The plan said a model
    swap is recognised by *every hash mismatching at once*, which is true and is a heuristic — and it
    misfires in exactly the case that is least tolerable, a small table whose rows all legitimately
    changed between reconciles. Since the model that produced a vector is a fact about that vector,
    it belongs here by this module's own rule, it costs a dictionary-encoded string per row, and it
    turns the inference into the fact. `source_hash` still folds the model in, so the invalidation
    remains by construction; this is what makes the *refusal* exact rather than probabilistic.

    `dims` is here for the same reason and answers a different question: a reader holding one row can
    tell a truncated vector from a whole one without loading the model that produced it."""
    return (
        Column(VECTOR_KEY_COLUMN, key_type, required=True),
        # Single-valued today — one `semantic:` per type — and present anyway, under the rule above.
        # Going plural later widens a key in the loader; it must not need a table to grow a column.
        Column(PROPERTY_COLUMN, "string", required=True),
        Column(MODEL_COLUMN, "string", required=False),
        Column(DIMS_COLUMN, "int", required=False),
        Column(VECTOR_COLUMN, "list<float>", required=False),
        Column(HASH_COLUMN, "string", required=False),
        Column(EMBEDDED_AT_COLUMN, "timestamptz", required=False),
    )


def source_hash(text: str, model: str, dims: int, property_name: str) -> str:
    """`hash(text ‖ model ‖ dims ‖ property)` — what makes a vector's staleness decidable.

    **The model is in here, not just the text**, so changing provider invalidates every vector by
    construction rather than by anyone remembering to. `dims` is in here too and is not redundant: a
    provider that silently returns truncated vectors under the same model name is a real deployment
    mistake, and folding the width in means the next reconcile notices instead of ranking two
    incompatible generations of vector against each other.

    **`property` is in here for the same reason, and was not until a whole-app probe asked what
    happened without it.** These four are exactly the columns `ir.VectorRef`'s comparability guard
    requires a stored row to match on, and three of them were folded in here while the fourth was
    only written to the sidecar. So the guard and the reconcile could disagree, and did: renaming a
    `semantic:` property — an apiName edit that moves no column, changes no text and makes `loom
    plan` say *No changes* — left every `source_hash` identical, so `loom embed` reported
    `rowsCurrent: 14, rowsEmbedded: 0` while `match_` guarded all fourteen rows out and ranked
    nothing. `VectorRef` claimed the case was covered, on the grounds that "re-pointing `semantic:`
    changes every `source_hash`" — true only when the text changes with it, and a rename is the case
    where it does not. The window that docstring calls *between the deploy and the reconcile* was
    permanent, and the reconcile is the thing `loom serve`'s banner tells the operator to read.

    Folding it in here rather than teaching `existing()` to read the property column is what keeps
    the invariant one sentence: a row is embedded iff its stored hash equals the hash of what it is
    now, where *what it is now* means everything the guard will compare. The upgrade cost is one
    full re-embed per warehouse, which is exactly what a model swap already costs and is the same
    honesty — a vector whose provenance Loom can no longer prove is one it should not rank.

    Length-prefixed rather than concatenated with a separator, so no text can impersonate a different
    (text, model) pair by containing the separator. A contrived collision, but the fix is one line and
    the failure it prevents is a vector that never refreshes."""
    parts = [text, model, str(dims), property_name]
    payload = "".join(f"{len(p)}:{p}" for p in parts)
    return hashlib.sha256(payload.encode()).hexdigest()


def embeddable(text: Any) -> str | None:
    """The text to embed for one row, or None if there is none.

    Null and blank are the same answer here, and that answer is *no vector* rather than *a vector of
    nothing*: a zero-ish embedding of an empty string is a point in the space that would rank against
    real queries and mean nothing. A row with no text is simply absent from `match_`, which is the
    honest shape — and it is **not** staleness, so the reconcile must not chase it forever. It is also
    why the orphan prune keys on *rows with text* rather than on *rows*: text that is blanked leaves a
    vector behind exactly as a deleted row does."""
    if text is None:
        return None
    if not isinstance(text, str):
        # Refused rather than coerced. The validator already requires a `string` property, so a
        # non-string here means the physical column disagrees with the spec, and `str()` on it would
        # embed a repr and hash it as though it were prose.
        return None
    stripped = text.strip()
    return stripped or None


@dataclass(frozen=True)
class VectorRow:
    """One sidecar row, as this module reads and writes it."""

    key: Any
    property: str
    model: str
    dims: int
    vector: Sequence[float]
    source_hash: str
    embedded_at: datetime

    def row(self) -> dict[str, Any]:
        return {
            VECTOR_KEY_COLUMN: self.key,
            PROPERTY_COLUMN: self.property,
            MODEL_COLUMN: self.model,
            DIMS_COLUMN: self.dims,
            VECTOR_COLUMN: list(self.vector),
            HASH_COLUMN: self.source_hash,
            EMBEDDED_AT_COLUMN: self.embedded_at,
        }


@dataclass
class VectorStore:
    """One object type's sidecar, read through the read port and written through `VectorWriter`.

    `EditLog` and `LoadLog`'s shape — two references kept distinct so the read half stays usable
    against a catalog nobody can write to — and here that separation earns more than symmetry: slice
    3 reads this table on every ranked query and must never hold something that can write it."""

    catalog: Catalog
    object_type: str
    key_type: str
    writer: VectorWriter | None = None

    @property
    def table(self) -> str:
        return vector_table(self.object_type)

    @property
    def columns(self) -> tuple[Column, ...]:
        return vector_columns(self.key_type)

    def exists(self) -> bool:
        return self.catalog.table_exists(self.table)

    def snapshot_id(self) -> int | None:
        """The snapshot the next write must assert, or None for a sidecar that is absent or empty.

        Both cases are `None` and both are honest: `AssertRefSnapshotId(None)` means *this table has
        no snapshot*, which is true of one just created and false the moment anything lands in it."""
        if not self.exists():
            return None
        return self.catalog.current_snapshot_id(self.table)

    def existing(self) -> dict[Any, dict[str, Any]]:
        """Every stored row, keyed. Empty if this type has never been embedded.

        Reads `key`, `source_hash` and `model` and not the vectors, which is the whole reason this is
        a projection: the vectors are the entire size of the table, and deciding *what needs
        embedding* never looks at one."""
        if not self.exists():
            return {}
        rows = self.catalog.scan(
            self.table, columns=(VECTOR_KEY_COLUMN, HASH_COLUMN, MODEL_COLUMN, EMBEDDED_AT_COLUMN)
        ).to_pylist()
        return {r[VECTOR_KEY_COLUMN]: r for r in rows}

    def ensure(self) -> None:
        self._writer().ensure_vectors(self.object_type, self.columns)

    def merge(self, rows: Sequence[VectorRow], *, expect_snapshot_id: int | None) -> None:
        if not rows:
            return
        self._writer().merge_vectors(
            self.object_type,
            self.columns,
            [r.row() for r in rows],
            expect_snapshot_id=expect_snapshot_id,
        )

    def prune(self, keys: Sequence[Any]) -> None:
        """Remove these keys. The erasure path — see the module docstring for what it owes.

        No snapshot to assert, and `VectorWriter` has no parameter for one: a delete follows no read
        its correctness depends on, and a check would let a concurrent reconcile refuse an erasure."""
        if not keys:
            return
        self._writer().delete_vectors(self.object_type, list(keys))

    def _writer(self) -> VectorWriter:
        if self.writer is None:  # pragma: no cover - the runtime resolves a writer first
            raise RuntimeError(
                f"the vector store for '{self.object_type}' has no writer — nothing can be written"
            )
        return self.writer


def oldest(stamps: Iterable[Any]) -> datetime | None:
    """The oldest of these `embedded_at` values, or None if there are none.

    The oldest rather than the newest, and that is the whole content of every field computed from it:
    *every vector counted here is at least this current*. Reporting the newest would let one row
    embedded a second ago describe a set last reconciled in March.

    **One definition, two readers, and slice 3 corrected which reader gets which set.** This module
    predicted the ranked surface would consume `embedded_as_of` below — the oldest stamp in a whole
    sidecar — and that is the *operator's* question, which is why `loom embed` still reports it. An
    envelope answers the caller's, and a caller is holding one page of a ranking: the honest claim
    there is about the rows that came back, not about rows they were never shown and cannot ask for.
    Computing the sidecar-wide reading per call would also mean a scan of a column nobody in the
    answer is keyed to, which is the same per-call extra read the milestone refused for the count of
    unembedded rows. So the definition stays here, once, and each caller hands it its own set."""
    present = [s for s in stamps if isinstance(s, datetime)]
    return min(present) if present else None


def embedded_as_of(rows: Mapping[Any, Mapping[str, Any]]) -> datetime | None:
    """The oldest `embedded_at` across a whole sidecar — what `loom embed` reports.

    Computed here because the definition of freshness lives here, and a result envelope should not
    be the place a second one gets invented. See `oldest` for why the ranked surface reads it over a
    different set."""
    return oldest(r.get(EMBEDDED_AT_COLUMN) for r in rows.values())


def now() -> datetime:
    return datetime.now(UTC)
