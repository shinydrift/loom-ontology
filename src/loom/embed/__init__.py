"""Embedding — the plane a `contains` filter cannot reach, one milestone before it can be searched.

`search_<type>` finds rows that **say** a word. An agent asking *which orders had a payment dispute?*
gets nothing for "sent the money back", "chargeback" or "customer wanted out", because the answer is
in the text and the caller's words are not the data's words. This package is what puts a number on
that gap, and slice 3 is what lets anyone ask.

Three things live here and each is a first:

- **`provider`** — Loom's first dependency on a model. A port, because there are two of them and
  because the thing being abstracted is a *function* rather than a store.
- **`store`** — Loom's first **data** in `_loom_meta`, as opposed to Loom's first *records*. A vector
  is not an audit trail: it describes a row that exists now, it goes stale, and keeping it correct
  needs the upsert and the delete the two log ports are permanently denied.
- **`runtime`** — `loom embed`, which is where *stale* stops being an intuition and becomes a hash
  comparison somebody can run.

What this package does not do is rank. No tool, no lowering, no `via` — a sidecar full of vectors and
nothing that reads them, which is the same order M5 and M10's first slice took: the plane before the
surface, so the thing the surface stands on is settled before anyone can call it.
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
    "provider_for",
    "source_hash",
    "vector_columns",
]
