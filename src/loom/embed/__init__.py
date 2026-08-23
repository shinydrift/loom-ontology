"""Embedding — the plane a `contains` filter cannot reach, one milestone before it can be searched.

`search_<type>` finds rows that **say** a word. An agent asking *which orders had a payment dispute?*
gets nothing for "sent the money back", "chargeback" or "customer wanted out", because the answer is
in the text and the caller's words are not the data's words. This package is what puts a number on
that gap, and slice 3 is what lets anyone ask.

Four things live here and three of them are a first:

- **`provider`** — Loom's first dependency on a model. A port, because there are two of them and
  because the thing being abstracted is a *function* rather than a store.
- **`store`** — Loom's first **data** in `_loom_meta`, as opposed to Loom's first *records*. A vector
  is not an audit trail: it describes a row that exists now, it goes stale, and keeping it correct
  needs the upsert and the delete the two log ports are permanently denied.
- **`runtime`** — `loom embed`, which is where *stale* stops being an intuition and becomes a hash
  comparison somebody can run.
- **`match`** — the reader those three exist for, and the answer to what the paragraph below used to
  say this package could not do.

The order that got here was the plane before the surface, which is the order M5 and this milestone's
first slice took: a sidecar full of vectors and nothing that reads them, so the thing the surface
stands on was settled before anyone could call it. What is still not built is `via` — a ranking can
name one type's rows and not yet the rows of a type linked to it.

**`match` is deliberately not re-exported below.** It sits *above* the resolver — it is the one thing
here that takes one — and importing it from this file would make `loom.resolver` import
`loom.embed.match` import `loom.resolver`. Callers reach it as `loom.embed.match`, which is also the
honest picture of where it sits.
"""

from __future__ import annotations

from .provider import (
    DEFAULT_LOCAL_MODEL,
    EmbeddingError,
    EmbeddingProvider,
    LocalProvider,
    OpenAIProvider,
    provider_for,
)
from .runtime import (
    APPLIED,
    BATCH_ROWS,
    CONFLICT,
    EMBED_FAILED,
    FAILED,
    MODEL_CHANGED,
    PREVIEWED,
    REFUSED,
    WRITE_FAILED,
    EmbedError,
    EmbedResult,
    EmbedRuntime,
    TypeReconcile,
    build_embedder,
)
from .store import (
    VectorRow,
    VectorStore,
    embeddable,
    embedded_as_of,
    oldest,
    source_hash,
    vector_columns,
)

__all__ = [
    "APPLIED",
    "BATCH_ROWS",
    "CONFLICT",
    "DEFAULT_LOCAL_MODEL",
    "EMBED_FAILED",
    "FAILED",
    "WRITE_FAILED",
    "MODEL_CHANGED",
    "PREVIEWED",
    "REFUSED",
    "EmbedError",
    "EmbedResult",
    "EmbedRuntime",
    "EmbeddingError",
    "EmbeddingProvider",
    "LocalProvider",
    "OpenAIProvider",
    "TypeReconcile",
    "VectorRow",
    "VectorStore",
    "build_embedder",
    "embeddable",
    "embedded_as_of",
    "oldest",
    "provider_for",
    "source_hash",
    "vector_columns",
]
