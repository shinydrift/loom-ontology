"""Bulk ingest — declared loads, checked against the ontology, written as one commit, recorded.

The plane `_loom_meta.edits` could not see. `apply` changes a table's shape, an action changes one of
its rows, and until this package nothing in Loom could put a batch in — which meant a deployment
could demand it be able to record what it writes, satisfy that demand precisely for every single-row
agent write, and have nothing at all to say about the overwrite that actually moved the numbers.

What this claims and what it does not:

- **It claims the contract and the record.** A batch becomes rows only if every value reads as its
  declared type, every key is present and unique, and the columns line up in both directions. One
  load is one Iceberg commit stamped with its own id, and one row in `_loom_meta.loads`.
- **It does not claim the pipeline.** Loom reads a file. It does not connect to Kafka, crawl an
  object store or open a JDBC connection, and `ingest.source` is where that boundary is drawn and
  argued.
- **It does not migrate.** The `BulkWriter` port has no DDL verb, so a batch that does not fit the
  table is refused and the fix is `loom plan` / `loom apply`.
- **It is off unless a deployment says otherwise.** `governance.ingest` defaults to `refused`.
"""

from __future__ import annotations

from .log import LOAD_COLUMNS, LoadLog, LoadRecord, derive_load_id, require_load_log
from .result import (
    APPLIED,
    FAILED,
    LOG_FAILED,
    PREVIEWED,
    QUARANTINABLE,
    REFUSED,
    Failure,
    IngestResult,
)
from .runtime import IngestError, IngestRuntime, build_ingest
from .sequence import (
    PARTIAL,
    SEQUENCE_COLUMNS,
    SequenceError,
    SequenceResult,
    SequenceRuntime,
    Step,
    build_sequences,
    derive_sequence_id,
    read_manifest,
)
from .source import Batch, SourceError, read_batch

__all__ = [
    "APPLIED",
    "FAILED",
    "LOAD_COLUMNS",
    "LOG_FAILED",
    "PREVIEWED",
    "QUARANTINABLE",
    "REFUSED",
    "Batch",
    "Failure",
    "IngestError",
    "IngestResult",
    "IngestRuntime",
    "LoadLog",
    "LoadRecord",
    "PARTIAL",
    "SEQUENCE_COLUMNS",
    "SequenceError",
    "SequenceResult",
    "SequenceRuntime",
    "Step",
    "build_sequences",
    "derive_sequence_id",
    "read_manifest",
    "SourceError",
    "build_ingest",
    "derive_load_id",
    "read_batch",
    "require_load_log",
]
