"""Reading a batch off disk — the one place Loom touches a file that is not a spec.

**This module is the boundary of the claim.** Loom does not connect to Kafka, crawl an object store,
poll a queue or open a JDBC connection, and the reason is not sequencing: a framework that moves data
is a different thing from one that decides whether data may become rows, and the second is what the
ontology is for. A pipeline hands Loom a batch; Loom checks it against the spec, writes it as one
commit, and records that it did. Everything upstream of the file stays somebody else's.

So the three formats here are three spellings of *a file somebody produced*, and adding a fourth is a
small decision about parsers. Adding a *source* would be a large one about what Loom is, and it is
not made here.

Values come back as whatever the format naturally yields — Arrow scalars from Parquet, JSON scalars
from NDJSON, strings from CSV — and are **not** interpreted here. Coercion to declared types is
`model.coerce_value`'s job in the runtime, because that is the function the read path and the action
runtime already coerce with, and a second interpretation of `"42"` living in a file reader would be a
third answer to a question this codebase has settled twice.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SourceError(RuntimeError):
    """The file could not be read as the format the entry declares.

    Raised rather than accumulated, unlike almost everything else in this package: there is no batch
    yet, so there is nothing to report failures *about*. The runtime turns it into a `source_error`
    failure on a refused result."""


@dataclass(frozen=True)
class Batch:
    """Rows as the file had them, plus enough to identify the file afterwards.

    `columns` is the source's own column order and spelling, kept because every refusal about a
    column has to name it as the operator wrote it rather than as the ontology would have. Taken
    from the *header* wherever a format has one — a CSV header row and a Parquet schema each declare
    a column set independently of how many rows follow it. NDJSON has no header, so there its column
    set is the union of the keys the records actually carry, and that difference decides something:

    **A zero-byte file cannot empty a table, and no special case is what makes that true.** An empty
    NDJSON declares no columns, so it fails the ordinary column check every batch faces. That is the
    safe answer rather than an awkward one: a truncated upload and a deliberate empty batch are the
    same zero bytes, and under `mode: replace` one of them wipes a table. A source that means *these
    columns, and no rows* has to be able to say so — a header-only CSV can, an empty Parquet table
    can, and NDJSON cannot.

    `fingerprint` is a SHA-256 of the file's bytes, and it is what `derive_load_id` hashes. Of the
    bytes rather than of the parsed rows, deliberately: reparsing a file to find out whether it is
    the one that already landed is work, and two files that parse identically but differ on disk are
    two files an auditor can tell apart."""

    rows: tuple[Mapping[str, Any], ...]
    columns: tuple[str, ...]
    fingerprint: str
    path: str = ""
    lines: tuple[int, ...] = ()
    """Where each row came from in the file, 1-based, parallel to `rows`.

    Carried so a refusal about a row can name the place an operator can *open*. The runtime used to
    report `enumerate(batch.rows)` — 0-based, and counting parsed rows rather than file lines — so
    the first line of a file was "row 0", a blank line silently shifted every number after it, and
    the reader's own `line 2` sat in the same output meaning something else. Two numbering schemes
    and one word between them.

    Empty for a batch built before this existed; `locate` falls back to the row's position."""

    def __len__(self) -> int:
        return len(self.rows)

    def locate(self, index: int) -> str:
        """How to name row `index` to somebody holding the file.

        `line N` where the format has lines and the reader counted them — the same words
        `_read_ndjson` and `_read_csv` use for a parse failure, because it is the same N. `row N`,
        1-based, for parquet, which has rows and no lines."""
        if index < len(self.lines):
            return f"line {self.lines[index]}"
        return f"row {index + 1}"


def read_batch(path: str | Path, fmt: str) -> Batch:
    """One file, in the format the entry declared. Never in a format guessed from the extension."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as e:
        raise SourceError(f"cannot read '{p}': {e}") from e

    fingerprint = hashlib.sha256(raw).hexdigest()
    readers = {"parquet": _read_parquet, "ndjson": _read_ndjson, "csv": _read_csv}
    reader = readers.get(fmt)
    if reader is None:  # pragma: no cover - config validation rejects unknown formats first
        raise SourceError(f"no reader for format '{fmt}'")
    rows, columns, lines = reader(raw, str(p))
    return Batch(
        rows=tuple(rows),
        columns=tuple(columns),
        fingerprint=fingerprint,
        path=str(p),
        lines=tuple(lines),
    )


def _read_parquet(
    raw: bytes, name: str
) -> tuple[Sequence[Mapping[str, Any]], Sequence[str], Sequence[int]]:
    try:
        import pyarrow.parquet as pq
    except ImportError as e:
        raise SourceError(
            f"reading '{name}' as parquet needs pyarrow — install the extra: "
            f"pip install 'loom-ontology[iceberg]' ({e})"
        ) from e
    try:
        table = pq.read_table(io.BytesIO(raw))
    except Exception as e:
        raise SourceError(f"'{name}' is not readable as parquet: {e}") from e
    # No lines to give, and none invented: parquet is columnar, so a row has a position and not a
    # place in a file. `Batch.locate` falls back to `row N` — 1-based, and never `row 0`.
    return table.to_pylist(), tuple(table.column_names), ()


def _read_ndjson(
    raw: bytes, name: str
) -> tuple[Sequence[Mapping[str, Any]], Sequence[str], Sequence[int]]:
    """One JSON object per line. Blank lines are skipped; anything else that is not an object is an
    error naming the line, because a file that is *almost* NDJSON is the case where a silent skip
    loses rows an operator believed they had loaded."""
    rows: list[Mapping[str, Any]] = []
    columns: list[str] = []
    lines: list[int] = []
    seen: set[str] = set()
    for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise SourceError(f"'{name}' line {number} is not valid JSON: {e}") from e
        if not isinstance(row, dict):
            raise SourceError(f"'{name}' line {number} is a {type(row).__name__}, not a JSON object")
        rows.append(row)
        lines.append(number)
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(str(key))
    return rows, columns, lines


def _read_csv(
    raw: bytes, name: str
) -> tuple[Sequence[Mapping[str, Any]], Sequence[str], Sequence[int]]:
    """A header row and then values, every one of them a string.

    Strings all the way through is the point rather than a limitation: `coerce_value` is the same
    function that reads `"42"` off an MCP call, and letting a CSV reader guess types would introduce
    a second, worse type system — one that decides `007` is an integer and `2026-01-04` is a string
    on evidence the spec already answered.

    An empty value is an empty string and **not** null, which is the one CSV question with no good
    answer and therefore one to state loudly: a property that must be nullable from a CSV needs the
    file to omit the column or the pipeline to write something the type refuses. Guessing that `""`
    means null would make every empty string in every text column unrepresentable."""
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise SourceError(f"'{name}' has no header row, so its columns cannot be named")
    columns = [str(f) for f in reader.fieldnames]
    rows: list[Mapping[str, Any]] = []
    lines: list[int] = []
    for number, row in enumerate(reader, start=2):
        if None in row:
            raise SourceError(
                f"'{name}' line {number} has more values than the header has columns"
            )
        rows.append({k: v for k, v in row.items() if k is not None})
        # `start=2` above is the header offset, so this is the line an operator opens the file to.
        lines.append(number)
    return rows, columns, lines


def write_rejects(path: str | Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """Quarantine the rows a load would not accept, as NDJSON, whatever the source format was.

    One output format regardless of input, because this file is not a re-loadable copy of the source
    — it is a report about rows, and the rows in it failed for reasons a strongly-typed format cannot
    carry. Each record is the source row exactly as it was read, plus `_loom_rejected` explaining
    why, so an operator can grep it and a pipeline can count it. Values that JSON cannot hold are
    rendered with `str`, since this is diagnosis rather than data."""
    p = Path(path)
    with p.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str) + "\n")
