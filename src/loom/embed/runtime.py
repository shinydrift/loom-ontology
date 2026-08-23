"""`loom embed` — the reconcile, and the only thing in Loom that calls a model.

**A command, not a hook, and "automatic" was always about derivation rather than timing.** You never
hand Loom a vector; that is what `semantic:` buys. What you do choose is *when* the derivation runs,
and neither of the alternatives survives contact with the rest of the system:

- **At query time** calls a model on every call, which turns a ranked read into a network round trip
  and makes the surface's latency a property of somebody's API quota.
- **At write time**, as a `run_` hook, covers the minority of writes by a wide margin. M9 is why:
  `loom ingest` writes four million rows without passing the action runtime at all, so a hook there
  would keep the sidecar current for exactly the writes that are already the smallest.

So the reconcile is not optional even in a world where inline embedding exists, and building it first
is building the part that has to be right.

**It is idempotent, resumable, and commits per batch.** A run that fails halfway leaves the batches it
committed embedded and the rest to be found by the next run, because *what needs embedding* is
recomputed from hashes every time rather than tracked in a cursor. That is also what makes the
batching safe: holding a hundred thousand vectors in memory to write them as one commit would be a
gigabyte of Python floats bought to make a failure less resumable.

**Governance applies to the ranking, never to the reconcile.** A row hidden from a caller by
`governance.policies` is still embedded here, and that is not a leak — the predicate rides on
`ir.TableRef` at the point a type becomes a table, so a governed row does not *exist* for that caller
when slice 3 ranks. The alternative makes the sidecar a function of the policy set: change a `rows:`
clause and every vector silently needs recomputing, with nothing in the hash able to see it. What
governance *does* refuse here is a masked semantic property, and it refuses it in `bind_policies`
where the other four mask refusals live, so `loom embed` cannot become the back door that fills a
sidecar for a property `loom serve` will not rank.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..action.result import APPLIED, CONFLICT, FAILED, PREVIEWED, REFUSED, Failure
from ..catalog.base import CatalogError, ConcurrencyError, vector_writer_for
from ..model import ObjectType, Ontology
from .provider import EmbeddingError, EmbeddingProvider, provider_for
from .store import VectorRow, VectorStore, embeddable, embedded_as_of, now, source_hash

__all__ = [
    "APPLIED",
    "BATCH_ROWS",
    "CONFLICT",
    "EMBED_FAILED",
    "FAILED",
    "MODEL_CHANGED",
    "PREVIEWED",
    "REFUSED",
    "WRITE_FAILED",
    "EmbedError",
    "EmbedResult",
    "EmbedRuntime",
    "TypeReconcile",
    "build_embedder",
]

# `CONFLICT` is imported from `action.result` rather than spelled again — `ingest.result`'s rule,
# that the vocabularies overlap where the meaning is identical, and *the table moved under me* is
# identical. It is also the only code here that `Failure.retryable` reports true for, and that
# asymmetry wants a sentence rather than a second `RETRYABLE` frozenset: a reconcile recomputes what
# it needs from hashes every run, so **re-running this command is always safe**, whatever failed.
# Per-failure retryability is load-bearing for a one-shot action, which cannot be blindly repeated.
# Here it is barely load-bearing at all.

MODEL_CHANGED = "model_changed"
"""Refused: the sidecar holds vectors from a different model than this deployment configures."""

EMBED_FAILED = "embed_failed"
WRITE_FAILED = "write_failed"

BATCH_ROWS = 256
"""Rows per model call and per commit.

Not a flag, and the two reasons point the same way: a batch is a tuning parameter of a provider
rather than a decision about a load — unlike `--load-id`, which is an operator saying something only
they know — and every value of it produces the same sidecar. It bounds memory and it bounds how much
work a failure discards, which is the whole of what it is for."""


class EmbedError(RuntimeError):
    """A malformed command, or a pairing that cannot stand.

    `IngestError`'s scope exactly: asking to embed an object type that does not exist, or that
    declares no `semantic:`, is a command that never named a reconcile — not a reconcile that failed.
    Everything an operator, the lake or a model can cause comes back as a `Failure` on a refused
    result instead."""


@dataclass(frozen=True)
class TypeReconcile:
    """What one object type's reconcile did, or would do.

    `rows_embedded` counts vectors written, `rows_pruned` counts vectors removed, and the two are
    reported apart because they answer different questions — the first is *how far behind was the
    sidecar*, the second is *how much text outlived its row*, and the second is the one an operator
    reading for erasure is looking for.

    `rows_without_text` is here rather than folded into a total, because it is the one count that is
    **not** work outstanding: a row whose semantic property is null or blank has no vector and never
    will, so a reconcile that reported it as pending would never converge."""

    object_type: str
    table: str
    embedded_as_of: datetime | None = None
    """The oldest `embedded_at` in this sidecar *before* the run, or None if it held nothing.

    Reported here rather than only in slice 3's result envelope, because the definition of freshness
    lives in `embed.store` and this is the first command that can state it. Before rather than after
    on purpose: after the run every vector is current by construction, which is a number that would
    always say *now* and therefore say nothing."""

    rows_read: int = 0
    rows_embedded: int = 0
    rows_pruned: int = 0
    rows_current: int = 0
    rows_without_text: int = 0
    rows_unkeyed: int = 0
    rows_ambiguous: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "objectType": self.object_type,
            "table": self.table,
            "embeddedAsOf": self.embedded_as_of,
            "rowsRead": self.rows_read,
            "rowsEmbedded": self.rows_embedded,
            "rowsPruned": self.rows_pruned,
            "rowsCurrent": self.rows_current,
            "rowsWithoutText": self.rows_without_text,
            "rowsUnkeyed": self.rows_unkeyed,
            "rowsAmbiguous": self.rows_ambiguous,
        }


@dataclass(frozen=True)
class EmbedResult:
    """What a whole reconcile did. One entry per object type it visited.

    Whole-run rather than per-type status, because a reconcile is one operator decision: a run that
    embedded `Order` and could not reach the model for `Ticket` is a run that did not finish, and
    reporting it as two thirds of a success is how a cron job goes green while a surface goes stale.
    """

    status: str
    types: tuple[TypeReconcile, ...] = ()
    failures: tuple[Failure, ...] = ()
    model: str = ""
    dims: int = 0

    @property
    def ok(self) -> bool:
        return self.status in (APPLIED, PREVIEWED)

    @property
    def rows_embedded(self) -> int:
        return sum(t.rows_embedded for t in self.types)

    @property
    def rows_pruned(self) -> int:
        return sum(t.rows_pruned for t in self.types)

    def as_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "model": self.model,
            "dims": self.dims,
            "types": [t.as_json() for t in self.types],
            "failures": [f.as_json() for f in self.failures],
        }


@dataclass
class EmbedRuntime:
    """The reconcile, holding a spec, the catalogs behind it, and one provider.

    One provider for the whole run, deliberately: `dims` is probed once and every hash in every type
    is built from the same (model, dims) pair. A per-type provider would make a mid-run model change
    expressible, which is the one thing this command refuses."""

    ontology: Ontology
    catalogs: Mapping[str, Any]
    provider: EmbeddingProvider
    targets: tuple[str, ...] = ()

    def reconcile(
        self, object_type: str | None = None, *, dry_run: bool = False, remodel: bool = False
    ) -> EmbedResult:
        """Bring every declared sidecar level with the text it describes.

        `object_type` narrows the run to one type; absent, every type declaring `semantic:` is
        visited. `remodel` is the operator saying *yes, the model changed, re-embed everything* — see
        `_model_changed` for why that is refused by default.

        **A dry run asks the model its width and nothing else.** `dims` is folded into every
        `source_hash`, so a preview that guessed it would report on hashes the real run will not
        compute — it would preview a different reconcile. One probe string is the smallest honest
        version of this command."""
        names = self._targets(object_type)
        try:
            model, dims = self.provider.model, self.provider.dims
        except EmbeddingError as e:
            return EmbedResult(status=FAILED, failures=(Failure(EMBED_FAILED, str(e)),))

        types: list[TypeReconcile] = []

        def done(status: str, failure: Failure | None = None) -> EmbedResult:
            return EmbedResult(
                status=status,
                types=tuple(types),
                failures=(failure,) if failure else (),
                model=model,
                dims=dims,
            )

        try:
            # **Every type's model is checked before any type is written.** A refusal is whole-run
            # because the operator made one decision, and a refusal that fires on the third type
            # after the first two committed could not honestly say *nothing has been written*. This
            # costs one extra read of each sidecar's hash projection — not its vectors — and buys a
            # sentence that is true.
            for name in names:
                obj = self.ontology.object_types[name]
                self._check_model(obj, self._store(obj).existing(), model, remodel)

            for name in names:
                obj = self.ontology.object_types[name]
                self._one(obj, model, dims, types, dry_run=dry_run, remodel=remodel)
        except _Refused as r:
            return done(REFUSED, r.failure)
        except EmbeddingError as e:
            return done(FAILED, Failure(EMBED_FAILED, str(e)))
        except ConcurrencyError as e:
            # Ahead of `CatalogError`, which it subclasses: a sidecar that moved is a second
            # reconcile running, not a broken metastore, and the two want different answers.
            return done(REFUSED, Failure(CONFLICT, str(e)))
        except CatalogError as e:
            return done(FAILED, Failure(WRITE_FAILED, str(e)))
        return done(PREVIEWED if dry_run else APPLIED)

    def _targets(self, object_type: str | None) -> tuple[str, ...]:
        if object_type is None:
            return self.targets
        if object_type not in self.ontology.object_types:
            known = ", ".join(sorted(self.ontology.object_types)) or "none"
            raise EmbedError(
                f"objectType '{object_type}' is not declared by this ontology (known: {known})"
            )
        if object_type not in self.targets:
            declared = ", ".join(self.targets) or "none"
            raise EmbedError(
                f"objectType '{object_type}' declares no 'semantic:' property, so it has nothing to "
                f"embed (types that do: {declared})"
            )
        return (object_type,)

    def _one(
        self,
        obj: ObjectType,
        model: str,
        dims: int,
        types: list[TypeReconcile],
        *,
        dry_run: bool,
        remodel: bool,
    ) -> None:
        """One type: read the text, diff it against the sidecar, prune, then write.

        **Appends its record through `finally`, so a failure keeps the counts of the work that
        already committed.** A run that prunes five orphans and embeds five hundred vectors before
        the model becomes unreachable really did those things, and reporting zeroes would suppress
        the erasure note the CLI prints off `rows_pruned` — the one line an operator most needs to
        have seen.

        Which is also why the counts a *real* run reports are what happened rather than what was
        planned. A preview has nothing else to report and reports the plan; `IngestResult` draws the
        same line in the same place."""
        prop = obj.semantic_property
        assert prop is not None  # `targets` is built from `semantic_property`, which the validator ensures
        store = self._store(obj)

        current, skipped = self._current_text(obj, prop.column, model, dims)
        stored = store.existing()

        pending = [k for k, (_, digest) in current.items() if stored.get(k, {}).get("source_hash") != digest]
        orphans = [k for k in stored if k not in current]
        pruned = 0
        embedded = 0
        try:
            if dry_run:
                pruned, embedded = len(orphans), len(pending)
                return
            # **Prune before writing**, and the order is the erasure argument rather than an
            # optimisation: an orphan is text that outlived the row it described, so a run that
            # fails after the merge should still have removed it. The reverse ordering makes the one
            # operation with a deadline behind it the one most likely to be skipped.
            if orphans:
                store.prune(orphans)
                pruned = len(orphans)
            if pending:
                embedded = self._write(store, obj, prop.name, current, pending, model, dims)
        finally:
            types.append(
                TypeReconcile(
                    object_type=obj.api_name,
                    table=store.table,
                    embedded_as_of=embedded_as_of(stored),
                    rows_read=len(current) + sum(skipped.values()),
                    rows_embedded=embedded,
                    rows_pruned=pruned,
                    rows_current=len(current) - len(pending),
                    rows_without_text=skipped["without_text"],
                    rows_unkeyed=skipped["unkeyed"],
                    rows_ambiguous=skipped["ambiguous"],
                )
            )

    def _current_text(
        self, obj: ObjectType, column: str, model: str, dims: int
    ) -> tuple[dict[Any, tuple[str, str]], dict[str, int]]:
        """Every keyed, embeddable, unambiguous row of the object's table as `key -> (text, hash)`.

        The whole table, without a limit and without a predicate, which is the one scan in Loom that
        is unbounded by design: *which rows need embedding* is a question about all of them, and a
        page of the answer is not an answer. It reads two columns — the key and the text — so the
        cost is a projection over a column store rather than the table.

        **Three kinds of row are counted out rather than embedded**, and the counts are returned
        beside the map so nothing is dropped silently:

        - **no key.** `match_` returns rows a caller then `get_`s, and a row with no key can be
          addressed by neither.
        - **no text.** Null or blank — the absence of text rather than a stale vector. See
          `store.embeddable`.
        - **an ambiguous key.** Two rows sharing a primary key, which `loom ingest` in `append` mode
          can produce. The sidecar is keyed, so only one of them could have a vector, and either
          choice is wrong: a last-one-wins map would make the stored vector a function of file
          layout and flip on the next compaction, re-embedding for no reason and ranking a row by
          its twin's text. `ActionRuntime._read` refuses this same situation by name as
          `ambiguous_key`; the reconcile is not a place to refuse a whole table over one bad key, so
          it skips and reports."""
        catalog = self.catalogs[obj.backing_catalog]
        key_column = obj.pk_property.column
        rows = catalog.scan(obj.backing_table, columns=(key_column, column)).to_pylist()

        out: dict[Any, tuple[str, str]] = {}
        skipped = {"without_text": 0, "unkeyed": 0, "ambiguous": 0}
        seen: set[Any] = set()
        for row in rows:
            key = row.get(key_column)
            if key is None:
                skipped["unkeyed"] += 1
                continue
            if key in seen:
                # The first sighting is withdrawn too — it is no more the right answer than the
                # second — so a duplicated key has no vector at all until the table is fixed.
                out.pop(key, None)
                skipped["ambiguous"] += 1
                continue
            seen.add(key)
            text = embeddable(row.get(column))
            if text is None:
                skipped["without_text"] += 1
                continue
            out[key] = (text, source_hash(text, model, dims))
        return out, skipped

    def _check_model(
        self, obj: ObjectType, stored: Mapping[Any, Mapping[str, Any]], model: str, remodel: bool
    ) -> None:
        """Refuse a silent re-embed of the whole warehouse, and name the flag that permits it.

        **Deliberately unlike `loom apply`, which refuses a breaking plan with no force flag at all.**
        There, no safe version of the operation exists, so a flag would only be a way to ask for the
        unsafe one. Here the operation is safe and merely *expensive and reversible* — every vector
        recomputed, once, at whatever the provider charges — so the honest posture is to make sure
        the operator meant it rather than to forbid it.

        The check reads the `model` column rather than inferring a swap from *every hash mismatching
        at once*. The inference is what the plan called for and it is a heuristic that misfires
        exactly where it hurts: a small table whose rows all legitimately changed between reconciles
        looks identical to a model swap. Since the model is recorded, the fact is available."""
        if remodel or not stored:
            return
        others = sorted(
            {
                str(r.get("model"))
                for r in stored.values()
                if r.get("model") not in (None, "", model)
            }
        )
        if not others:
            return
        raise _Refused(
            Failure(
                MODEL_CHANGED,
                f"'{obj.api_name}' has vectors from {', '.join(repr(m) for m in others)}, and this "
                f"deployment configures '{model}'. Every vector would be recomputed, which is a "
                f"model swap rather than a warehouse of edits — pass --remodel if that is what you "
                f"meant. Nothing has been written.",
            )
        )

    def _write(
        self,
        store: VectorStore,
        obj: ObjectType,
        property_name: str,
        current: Mapping[Any, tuple[str, str]],
        pending: Sequence[Any],
        model: str,
        dims: int,
    ) -> int:
        """Embed and commit in batches of `BATCH_ROWS`, re-reading the snapshot before each.

        Returns how many vectors actually committed, so a failure part-way through reports the work
        that landed rather than the work that was planned.

        The snapshot is read per batch rather than once, because each commit moves the table this
        one is asserting against — an expectation taken before the first batch would refuse every
        batch after it, and a run that refuses itself is worse than no check at all.

        **What that leaves the assertion catching is narrow, and worth stating rather than
        overselling.** Reading the snapshot immediately before the merge means the window it closes
        is the microseconds between the two, not the whole reconcile — so two concurrent reconciles
        can and do interleave. That is benign here in a way it is not for an action, and the reason
        is `source_hash`: both runs embed the same text with the same model (a differing model is
        refused before either writes), so an interleaved write stores the same vector. The one case
        that is not identical — a slow run committing a vector for text that has since changed —
        stores a hash of the *old* text, which is precisely the mismatch the next reconcile is
        looking for. Stale here is detectable by construction rather than prevented by a lock."""
        store.ensure()
        written = 0
        for start in range(0, len(pending), BATCH_ROWS):
            keys = list(pending[start : start + BATCH_ROWS])
            texts = [current[k][0] for k in keys]
            vectors = self.provider.embed(texts)
            if len(vectors) != len(keys):  # pragma: no cover - both providers check this themselves
                raise EmbeddingError(
                    f"the provider returned {len(vectors)} vector(s) for {len(keys)} text(s)"
                )
            stamped = now()
            store.merge(
                [
                    VectorRow(
                        key=key,
                        property=property_name,
                        model=model,
                        dims=dims,
                        vector=vector,
                        source_hash=current[key][1],
                        embedded_at=stamped,
                    )
                    for key, vector in zip(keys, vectors, strict=True)
                ],
                expect_snapshot_id=store.snapshot_id(),
            )
            written += len(keys)
        return written

    def _store(self, obj: ObjectType) -> VectorStore:
        catalog = self.catalogs[obj.backing_catalog]
        return VectorStore(
            catalog=catalog,
            object_type=obj.api_name,
            key_type=obj.pk_property.type.iceberg_type(),
            writer=vector_writer_for(catalog),
        )


@dataclass
class _Refused(Exception):
    """Internal: one type's refusal, unwound to the whole run.

    A refusal is whole-run because the operator made one decision. Carrying it as an exception rather
    than a return value keeps `_one` reading as the reconcile it is, with the two refusals it can
    raise stated where they are decided."""

    failure: Failure = field(default_factory=lambda: Failure("", ""))


def build_embedder(ontology: Ontology, config, catalogs: Mapping[str, Any] | None = None):
    """Pair this spec with this deployment on the embedding plane, or refuse the pairing.

    **It goes through `bind_reads`**, which is the function that already pairs a spec with a
    deployment for reading, and that is a decision rather than convenience. Two of its refusals are
    exactly this command's:

    - `check_capabilities` refuses an engine with no `vector_search` against a spec that declares
      `semantic:`. A deployment whose engine can never rank should not be filling a sidecar nothing
      will read, and the alternative — embed succeeding where serve refuses — is the back door
      `loom query` was deliberately built not to be.
    - `bind_policies` refuses a `mask:` over a semantic property. Without this call, `loom embed`
      would compute and store vectors for a property `loom serve` refuses to rank, which withholds
      the ranking while writing the thing the refusal was about.

    What it does *not* borrow is the row predicates. A `PolicySet` is produced and discarded here on
    purpose — see the module docstring for why a sidecar must not be a function of the policy set.

    Absent `mcp.embedding` is a refusal *of this command* rather than of the deployment, which is the
    same distinction `EmbeddingConfig` draws for the surface: a deployment configuring no provider is
    one of the deployments that reads without embedding, and `loom serve` starts fine. There is just
    nothing for `loom embed` to do, and saying so beats a traceback."""
    from ..catalog import open_catalogs
    from ..resolver import bind_reads

    open_cats = catalogs if catalogs is not None else open_catalogs(config)
    bind_reads(ontology, config, open_cats)

    if config.mcp.embedding is None:
        raise EmbedError(
            "this deployment configures no 'mcp.embedding', so there is no model to embed with — "
            "add it to loom.yaml. Nothing else about the deployment is wrong: a spec may declare "
            "'semantic:' and be served without it, which is what withholds the match_ tools"
        )

    targets = tuple(
        name
        for name, obj in ontology.object_types.items()
        if obj.semantic_property is not None
    )
    if not targets:
        raise EmbedError(
            "no objectType in this ontology declares a 'semantic:' property, so there is nothing to "
            "embed"
        )
    return EmbedRuntime(
        ontology=ontology,
        catalogs=open_cats,
        provider=provider_for(config.mcp.embedding),
        targets=targets,
    )
