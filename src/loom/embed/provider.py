"""`EmbeddingProvider` — the port, and Loom's first dependency on a model.

Every other port in this codebase abstracts a *store*. This one abstracts a **function**, and the
difference decides the shape: a catalog is asked whether a table exists and answers about the world,
while a provider is asked for the meaning of a string and answers with something only it can produce.
Two providers are not interchangeable the way two catalogs are — swapping them invalidates every
vector in the warehouse — which is why `model` is folded into `source_hash` and why the config key
naming it has no default.

**Two phases, and `config.py` owns the first.** Parsing `mcp.embedding` checks that the file names a
provider Loom knows and a model at all, and imports nothing. This module owns the second: whether the
named model can actually be obtained *here, now*. That split is `_parse_auth` / `build_verifier`'s and
it is the reason a `loom validate` on a spec with `semantic:` needs no model installed and no API key
set.

**`dims` is discovered, never declared.** `_parse_embedding` refuses a `dims` key because declaring a
model's width beside its name is a chance to declare it wrong, and vectors of the declared width
would be written, ranked against each other, and mean nothing. The other half of that argument is
here: the width comes from asking the model, once, by embedding a fixed probe string. It costs one
call per process — trivially, for a local model; one small request, for a hosted one — and it buys a
number that cannot disagree with the vectors beside it.

**The provider set is enumerated in `config.py`, not discovered here.** `EMBEDDING_PROVIDERS` is the
list of places a lake's text may be sent, and a plugin mechanism would make that list a property of
what happens to be installed. `local` is the default for the same reason the HTTP bind is loopback:
no row's text leaves the machine unless a deployment says so.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..config import EmbeddingConfig

PROBE = "loom"
"""The string every provider embeds once, to be asked how wide it is.

A constant rather than the first row's text, so the width is known before any row is read — the hash
that decides which rows *need* embedding already contains `dims`, so a provider that learned its width
from the batch would have to see the batch to decide what the batch is."""

DEFAULT_LOCAL_MODEL = "BAAI/bge-small-en-v1.5"
"""Named in the error that suggests it, never used as a default.

`mcp.embedding.model` is required precisely so Loom never picks a model on somebody's behalf. This is
the model an operator most likely wants when they have not thought about it, offered in a message
where they can still say otherwise, which is a different thing from a value that applies itself."""

_OPENAI_ENDPOINT = "https://api.openai.com/v1/embeddings"
_OPENAI_KEY_ENV = "OPENAI_API_KEY"


class EmbeddingError(RuntimeError):
    """A failure on the model plane: no library, no key, no network, a refused request.

    Its own class rather than a `CatalogError`, because the two are answered differently. A catalog
    failure means the lake is unreachable; this means Loom cannot compute what it would have written,
    and the reconcile that catches it has written nothing rather than half of something."""


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Text in, vectors out, plus the two facts every stored vector's hash is built from.

    **`embed` is a batch verb and has no single-text sibling.** Every caller has a batch — the
    reconcile embeds the rows whose text changed — and a per-row verb would invite a per-row API
    call, which is the difference between one request and ten thousand. A caller with one string
    passes a list of one.

    **Order is the contract.** The returned sequence is positional: the *i*th vector is the embedding
    of the *i*th text. There is no key, no id, and nothing to correlate on, which means an
    implementation that reorders for batching efficiency must reorder back. It is stated here because
    the failure is silent — every row gets a plausible vector, belonging to a different row.
    """

    model: str
    """The model's name, exactly as it goes into `source_hash`. Not necessarily the string the config
    asked for: a provider that resolves an alias must report what it resolved to, or a warehouse
    embedded before the alias moved would look current."""

    @property
    def dims(self) -> int:
        """The width of every vector this provider returns."""
        ...

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Embed each text, in order. Raises `EmbeddingError` on any failure of the whole batch."""
        ...


@dataclass
class LocalProvider:
    """`provider: local` — a model that runs in this process, over `fastembed`.

    **fastembed rather than sentence-transformers, and the cost of that is a smaller model.** The
    alternative is the quality baseline and it arrives with torch, so `pip install
    loom-ontology[embed]` would pull something over a gigabyte to make a default work. This is the
    default provider — what an operator gets for writing `model:` and nothing else — and a default
    that expensive is one people route around. fastembed runs the same class of model on onnxruntime
    at a fraction of the install, and the deployments that want the larger model can reach it through
    `provider: openai` or, when somebody asks, a third entry in `EMBEDDING_PROVIDERS`.

    **The import is inside the method**, as pyiceberg's is one layer down and for the same reason:
    `import loom` must not require a model runtime, and `loom validate` on a spec declaring
    `semantic:` must work on a machine that will never embed anything.
    """

    model: str
    _impl: Any = field(default=None, repr=False)
    _dims: int | None = field(default=None, repr=False)

    @property
    def dims(self) -> int:
        if self._dims is None:
            self._dims = len(self._embed_raw([PROBE])[0])
        return self._dims

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        if not texts:
            return ()
        return self._embed_raw(texts)

    def _embed_raw(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        vectors = tuple(tuple(float(x) for x in v) for v in self._model().embed(list(texts)))
        if len(vectors) != len(texts):  # pragma: no cover - guards a fastembed API change
            raise EmbeddingError(
                f"the local model returned {len(vectors)} vector(s) for {len(texts)} text(s) — "
                f"the port's contract is positional, so a short batch cannot be matched back to its "
                f"rows and this write is refused rather than guessed at"
            )
        return vectors

    def _model(self) -> Any:
        if self._impl is not None:
            return self._impl
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise EmbeddingError(
                f"'provider: local' needs fastembed — install the extra: "
                f"pip install 'loom-ontology[embed]' ({e})"
            ) from e
        try:
            self._impl = TextEmbedding(model_name=self.model)
        except Exception as e:
            raise EmbeddingError(
                f"the local embedding model '{self.model}' could not be loaded: {e}\n"
                f"  hint: fastembed names models as they are named on Hugging Face, e.g. "
                f"'{DEFAULT_LOCAL_MODEL}'"
            ) from e
        return self._impl


@dataclass
class OpenAIProvider:
    """`provider: openai` — a hosted model, over one HTTPS request per batch.

    **The key comes from the environment and there is no config key for it**, which is the one place
    this differs from every other deployment fact in `loom.yaml`. A `loom.yaml` is a reviewed file in
    a repository; a credential in one is a credential in a diff. `mcp.auth` makes the same call from
    the other direction — it names an issuer and a JWKS URI and never a secret.

    **`httpx` rather than the stdlib**, because it is already a hard dependency of `mcp` and this
    milestone should not add a second HTTP client to a project that has one.
    """

    model: str
    _dims: int | None = field(default=None, repr=False)

    @property
    def dims(self) -> int:
        if self._dims is None:
            self._dims = len(self.embed([PROBE])[0])
        return self._dims

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        if not texts:
            return ()
        import os

        key = os.environ.get(_OPENAI_KEY_ENV)
        if not key:
            raise EmbeddingError(
                f"'provider: openai' needs {_OPENAI_KEY_ENV} in the environment — it is read from "
                f"there rather than from 'loom.yaml', because a config file is reviewed in a "
                f"repository and a key in one is a key in a diff"
            )
        try:
            import httpx
        except ImportError as e:  # pragma: no cover - httpx ships with the mcp extra
            raise EmbeddingError(
                f"'provider: openai' needs httpx — install the extra: "
                f"pip install 'loom-ontology[embed]' ({e})"
            ) from e

        try:
            response = httpx.post(
                _OPENAI_ENDPOINT,
                headers={"Authorization": f"Bearer {key}"},
                json={"model": self.model, "input": list(texts)},
                timeout=60.0,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as e:
            raise EmbeddingError(f"the embedding request to OpenAI failed: {e}") from e

        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingError(
                f"OpenAI returned {len(data) if isinstance(data, list) else 'no'} embedding(s) for "
                f"{len(texts)} text(s) — the port's contract is positional, so a short batch cannot "
                f"be matched back to its rows and this write is refused rather than guessed at"
            )
        try:
            # Sorted by `index` rather than trusted in arrival order. The API documents that it may
            # return them out of order, and the port's contract is positional — this is the one line
            # standing between that and every row getting its neighbour's meaning.
            ordered = sorted(data, key=lambda d: d.get("index", 0))
            return tuple(tuple(float(x) for x in d["embedding"]) for d in ordered)
        except (KeyError, TypeError, ValueError) as e:
            # A right-length, wrong-shape response is still a failed embed rather than a crash. The
            # length check above cannot see inside the entries, and a bare KeyError here would leave
            # `loom embed` with a traceback where it has a failure code.
            raise EmbeddingError(
                f"OpenAI returned {len(data)} entries that are not embeddings ({e!r})"
            ) from e


def provider_for(config: EmbeddingConfig) -> EmbeddingProvider:
    """The exchange point: a parsed config becomes something that can embed, or refuses here.

    `_port_for`'s sibling in spirit, and the same argument for existing — the caller should be told
    which provider refused and what it was being asked for, rather than hit an ImportError three
    frames down. It is deliberately *not* where the model is loaded: constructing a provider stays
    cheap and offline, and the first `embed` or `dims` is what reaches for a runtime. That keeps
    `build_embedder` able to pair a spec with a deployment without a network."""
    if config.provider == "local":
        return LocalProvider(model=config.model)
    if config.provider == "openai":
        return OpenAIProvider(model=config.model)
    # `_parse_embedding` refuses an unknown provider against `EMBEDDING_PROVIDERS`, so reaching here
    # means that set grew an entry this function was not taught about.
    raise EmbeddingError(  # pragma: no cover - config validation rejects unknown providers first
        f"no embedding provider is implemented for '{config.provider}'"
    )
