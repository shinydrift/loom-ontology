"""The ranked read plane — a model on one side, a sidecar on the other, the resolver between.

Everything below this module either builds a plan or fills a table. This is the one object that
holds both halves at once, and it exists because the two things a ranked read needs are the two
things the resolver deliberately does not have: something that can call a model, and a handle on the
lake.

**Why the resolver has neither, restated rather than assumed.** `Resolver.match` takes a *vector*, so
every read in that layer stays a pure function of the ontology and its arguments — no network in the
middle of building a plan, no 150MB runtime imported to answer a `get_`. And the resolver reads
through an `Engine`, never a `Catalog`, which is what keeps *the LLM never receives raw SQL*
structural. A ranked read needs one question a plan cannot ask (**has anything been embedded yet?**)
and one answer a plan cannot compute (**the query vector**), so they are asked here.

**It holds a `VectorStore` with no writer, which is slice 2's sentence coming true.** The store was
split into a read half and a write half so that "slice 3 reads this table on every ranked query and
must never hold something that can write it" could be a fact about the object rather than a
convention. `bind_matching` constructs them without a `VectorWriter`, so the serving process
*cannot* reach `merge_vectors` or `delete_vectors` through the read plane, whatever anybody adds to
it later.

**Two refusals live here, and both are refusals rather than empty results for one reason.** A caller
handed an empty ranking cannot tell *nothing was similar* from *nothing was ever embedded* or *you
asked with no words*. That is the argument `{"in": []}` was refused on, met again one plane over: an
answer indistinguishable from a real one is worse than a sentence saying what to do.

What is deliberately **not** refused here is a model swap. A sidecar full of vectors from another
model ranks nothing, because the comparability guard admits none of them — and the envelope names
the model this deployment configures, so an empty page is readable rather than mysterious. Refusing
it would need a per-call scan of the sidecar's `model` column to find out, which is the same per-call
extra read this milestone refused for the count of unembedded rows; `loom embed` and the serve banner
are where an operator is told.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..catalog.base import vector_table
from ..model import Ontology
from ..resolver import MatchResult, ResolverError
from .provider import EmbeddingProvider, provider_for
from .store import VectorStore, embeddable

if TYPE_CHECKING:
    from ..resolver import Resolver

__all__ = ["Matcher", "bind_matching"]


@dataclass(frozen=True)
class Matcher:
    """This deployment's ability to rank: one provider, and one read-only sidecar per object type.

    One provider for the process, as `EmbedRuntime` holds one for a run and for the same reason —
    the model is half of what makes a vector comparable, so a per-type provider would make a
    deployment that ranks two types against two models expressible."""

    provider: EmbeddingProvider
    stores: Mapping[str, VectorStore]
    """Keyed by object type — exactly the types that declare `semantic:`. A type absent from here has
    no `match_` tool, which is why the tool set can be built from `targets()` without a lake."""

    @property
    def model(self) -> str:
        return self.provider.model

    def targets(self) -> tuple[str, ...]:
        return tuple(self.stores)

    def match(
        self,
        resolver: Resolver,
        object_type: str,
        text: str,
        filters: Mapping[str, Any] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> MatchResult:
        """Embed the caller's words and rank this type's rows against them.

        The resolver arrives per call rather than being held, because it is the one thing here that
        varies with the caller: what a `match_` returns is governed by the policy set selected for
        the principal of the call in flight, and this object is built once for the process.
        """
        store = self.stores.get(object_type)
        if store is None:
            declared = ", ".join(sorted(self.stores)) or "none"
            raise ResolverError(
                f"'{object_type}' declares no 'semantic:' property, so it cannot be ranked by "
                f"meaning (types that can: {declared})"
            )
        # The caller's argument before the lake, which is `cmd_query`'s ordering: a blank query
        # should not need a reachable metastore to be refused. `embeddable` is the same function
        # that decides a *row* has no text, and it decides it the same way here — a query of
        # whitespace is the absence of a question, not a question about whitespace.
        query = embeddable(text)
        if query is None:
            raise ResolverError(
                f"match_{object_type} needs something to match against — 'text' was empty or blank, "
                "and a ranking with no query would order every row by nothing"
            )
        if not store.exists():
            raise ResolverError(
                f"nothing has been embedded for '{object_type}' yet — its vector sidecar "
                f"'{vector_table(object_type)}' does not exist. Run `loom embed --type "
                f"{object_type}`. Nothing about this deployment is wrong: a spec may declare "
                "'semantic:' and be served before a reconcile has ever run"
            )
        vector = self.provider.embed([query])[0]
        return resolver.match(
            object_type, vector, self.provider.model, filters, limit=limit, offset=offset
        )


def bind_matching(ontology: Ontology, config, catalogs: Mapping[str, Any]) -> Matcher | None:
    """Pair this spec with this deployment on the ranked plane, or answer that it has none.

    `None` in two cases and neither is a failure. **No `mcp.embedding`** is a deployment that reads
    without embedding — `mcp.writes: false`'s posture exactly, and the distinction `EmbeddingConfig`
    draws: negotiation asks *could this engine ever serve what this spec describes* and refuses a
    contradiction, while this asks *does this deployment switch it on*. **No type declaring
    `semantic:`** is a spec with nothing to rank, which is every spec written before this milestone.

    Nothing here opens a model or reads a table. `provider_for` stays offline by construction, so a
    server whose embedding model is a 150MB download still starts in the time it always did and pays
    on the first `match_` — the same split `_parse_auth` and `build_verifier` draw.

    It does **not** re-run the pairing refusals: `bind_reads` owns `check_capabilities` (an engine
    with no array arithmetic cannot serve a spec that declares `semantic:`) and `bind_policies` (a
    mask over a semantic property is refused before this deployment starts). Both fire on every
    surface that can rank, because every one of them binds reads first."""
    if config.mcp.embedding is None:
        return None
    stores = {
        name: VectorStore(
            catalog=catalogs[obj.backing_catalog],
            object_type=name,
            key_type=obj.pk_property.type.iceberg_type(),
        )
        for name, obj in ontology.object_types.items()
        if obj.semantic_property is not None
    }
    if not stores:
        return None
    return Matcher(provider=provider_for(config.mcp.embedding), stores=stores)
