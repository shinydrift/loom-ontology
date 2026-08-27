"""Draft a spec from a file — the one place Loom reads a schema instead of being told one.

**This is a scaffold, and the distinction it rests on is the one M9 drew.** `BulkWriter` has no DDL
verb because a *load* must not infer a *migration* — "the never-drop rule pointed at a new plane,
refusing to infer a schema change from the shape of somebody's file". That refusal stands untouched
here, because this command is not on that plane at all: it opens no catalog, holds no port, writes
no file, and runs before there is a table to migrate. It reads one file and prints text.

What keeps the two apart in practice, rather than only in argument, is that **the draft does not
validate**. `primaryKey` comes out as a placeholder no property matches, so `loom validate` fails on
it and `loom apply` never gets a spec to apply. A scaffold that emitted something immediately
servable would be a scaffold that gets committed unread, and the first person to discover what it
guessed would be whoever queried it.

**Parquet only, and the other two are refused by name.** A parquet file *declares* its types, so
reading one is reading rather than guessing. A CSV declares nothing — every type would be sniffed
from a sample, and decimal-versus-double on a money column is the sniff that silently loses
fractions of a cent, which is the exact error `examples/retail/seed.py` comments on. NDJSON is JSON,
which has no decimal and no date, so money would arrive as a double and a date as a string. Both are
addable later behind a flag that says what it is doing; neither is addable quietly.

**What it will not guess, in any format.** `enum` values, because a file shows the values it happens
to contain and not the domain's set — the retail example's `closed` tier is in its enum for a reason
no sample would ever reveal. `unique`, because a file with no duplicates is not a constraint.
Link types, `searchable`, `semantic:` — every one of them a claim about meaning, and this reads
storage. And the one an inferrer gets wrong by instinct: **which columns to leave out.** §2 rule 7
exists because mapping everything is the wrong answer, so a column whose type the spec has no name
for is emitted as a comment explaining that leaving it alone is the outcome, not the failure.

**Nullability is read, not observed.** It comes from the file's declared schema and never from
whether this particular file happens to contain a null. Worth saying because most parquet writers
declare every column nullable by default, so a draft is usually more permissive than the domain is —
which is the direction a draft should err, and still a thing to go through by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ._shape import OBJECT_NAME, identifier_problem

# Only one, and the refusals below are the argument for keeping it that way until each of the others
# has an answer better than a shrug.
INFER_FORMATS = ("parquet",)

UNREADABLE_FORMATS = {
    "csv": (
        "a CSV declares no types at all, so every type would be sniffed from a sample — and "
        "decimal-versus-double on a money column is the sniff that loses fractions of a cent "
        "without anybody noticing"
    ),
    "ndjson": (
        "JSON has no decimal and no date, so money would arrive as a double and a date as a "
        "string — the two types the spec is most careful about"
    ),
}

# A placeholder rather than an omission, and rather than a plausible guess. Omitting `primaryKey`
# fails with "missing key", which reads like the generator broke; this fails with a name that says
# what to do. It must not be a property name, or the draft would validate.
TODO_PRIMARY_KEY = "TODO-pick-the-primary-key"
TODO_CATALOG = "TODO-catalog"
TODO_TABLE = "TODO.table"
TODO_MODE = "TODO-append-merge-or-replace"


class InferError(RuntimeError):
    """The file cannot be read, or describes nothing a draft could be made of."""


@dataclass(frozen=True)
class Column:
    """One column as the file declares it, and what the spec can say about it.

    `spec` is None exactly when the spec has no name for the type, and then `refusal` says why. The
    two travel together because the interesting output is not the mapped columns — it is the
    unmapped ones with their reason attached, which is what a reader has to decide about."""

    name: str
    physical: str
    nullable: bool
    spec: dict | None
    refusal: str | None

    @property
    def mapped(self) -> bool:
        return self.spec is not None


@dataclass(frozen=True)
class Draft:
    """A spec somebody still has to finish, plus the load entry that would fill it."""

    api_name: str
    source: str
    columns: tuple[Column, ...]
    catalog: str = TODO_CATALOG
    table: str = TODO_TABLE
    primary_key: str | None = None
    entry: str | None = None

    @property
    def mapped(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.mapped)

    @property
    def unmapped(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if not c.mapped)

    @property
    def entry_name(self) -> str:
        return self.entry or _kebab(self.api_name)

    @property
    def blocking(self) -> tuple[str, ...]:
        """The placeholders that make `loom validate` fail, in the order they are rendered.

        There are exactly two, and they are exactly the two a file cannot answer: which column
        addresses a row, and which table this type reads. `--key`, `--catalog` and `--table` answer
        them on the command line, which is a person answering them — so a draft that was given all
        three has nothing left that blocks, and saying otherwise would be this command's own note
        contradicting `loom validate`.

        The other prompts in the header (`title`, `searchable`, enum values) are *questions*, not
        placeholders: a draft is poorer for leaving them, and validates either way. Keeping them
        separate is what lets the note be true — a note that cried TODO over a draft that validates
        is a note nobody reads by the third time."""
        todos = []
        if self.primary_key is None:
            todos.append(TODO_PRIMARY_KEY)
        if self.catalog == TODO_CATALOG or self.table == TODO_TABLE:
            todos.append(TODO_CATALOG if self.catalog == TODO_CATALOG else TODO_TABLE)
        return tuple(todos)


# ---- reading the file ------------------------------------------------------------


def read_columns(source: str | Path, fmt: str = "parquet") -> tuple[Column, ...]:
    """The file's declared schema, one `Column` per field, in file order.

    Only the schema is read — never the rows. That is not an optimisation: reading rows is how a
    generator starts observing enum values and nullability, and neither is a fact this file has."""
    if fmt not in INFER_FORMATS:
        raise InferError(_unreadable(fmt))
    path = Path(source)
    named = _named_by_suffix(path)
    if named is not None:
        # `--format` is never derived from the extension — a `.csv` that is really TSV would be
        # guessed wrong, per invocation, and the guess is the thing this command does not make. But
        # the by-name refusal above was then reachable only by typing `--format csv`, which is the
        # one thing nobody does when the extension already says so: `loom infer data.csv` opened it
        # as parquet and handed back pyarrow's "Parquet magic bytes not found in footer". The
        # extension decides no reader here, only which refusal the operator gets to read.
        raise InferError(_unreadable(named, path=path))
    return _read_parquet(path)


def _named_by_suffix(path: Path) -> str | None:
    """The unreadable format this filename claims to be, if any. `.json` is folded into `ndjson`:
    the refusal is about JSON's type system, which neither spelling escapes."""
    suffix = path.suffix.lower().lstrip(".")
    if suffix == "json":
        suffix = "ndjson"
    return suffix if suffix in UNREADABLE_FORMATS else None


def _unreadable(fmt: str, *, path: Path | None = None) -> str:
    why = UNREADABLE_FORMATS.get(fmt)
    subject = f"'{path}' is {fmt}, and " if path is not None else ""
    return (
        f"{subject}'loom infer' reads {', '.join(INFER_FORMATS)}, not '{fmt}'"
        + (f" — {why}" if why else "")
    )


def _read_parquet(path: Path) -> tuple[Column, ...]:
    try:
        import pyarrow.parquet as pq
    except ImportError as e:  # pragma: no cover - the extra is installed wherever ingest is
        raise InferError(
            f"reading '{path}' as parquet needs pyarrow — install the extra: "
            f"pip install 'loom-ontology[iceberg]' ({e})"
        ) from e
    try:
        schema = pq.read_schema(path)
    except OSError as e:
        raise InferError(f"cannot read '{path}': {e}") from e
    except Exception as e:  # pyarrow raises ArrowInvalid for a file that is not parquet
        raise InferError(f"'{path}' is not readable as parquet: {e}") from e

    if not schema.names:
        raise InferError(f"'{path}' declares no columns — there is nothing to draft from it")

    out = []
    for field in schema:
        spec, refusal = _map_type(field.type)
        out.append(
            Column(
                name=field.name,
                physical=str(field.type),
                nullable=field.nullable,
                spec=spec,
                refusal=refusal,
            )
        )
    return tuple(out)


def _map_type(t) -> tuple[dict | None, str | None]:
    """An Arrow type to the spec's §1 fragment for it, or to the reason there isn't one.

    The refusals are as specific as the mappings on purpose. "Unsupported type" tells a reader to
    give up; naming *which* rule has no word for this one tells them whether to declare the column
    differently, wait for a later spec version, or leave it unmanaged on purpose."""
    import pyarrow as pa

    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return {"type": "string"}, None
    if pa.types.is_boolean(t):
        return {"type": "boolean"}, None
    if pa.types.is_int8(t) or pa.types.is_int16(t) or pa.types.is_int32(t):
        return {"type": "int"}, None
    if pa.types.is_int64(t):
        return {"type": "long"}, None
    if pa.types.is_float32(t) or pa.types.is_float64(t):
        return {"type": "double"}, None
    if pa.types.is_decimal(t):
        return {"type": "decimal", "precision": t.precision, "scale": t.scale}, None
    if pa.types.is_date(t):
        return {"type": "date"}, None
    if pa.types.is_timestamp(t):
        if t.tz is None:
            return None, (
                "Loom's `timestamp` is an Iceberg `timestamptz` and this column is tz-naive, so "
                "declaring it would fail `loom validate --physical` against the table this file "
                "describes — give the column a zone, or leave it unmanaged"
            )
        return {"type": "timestamp"}, None
    if pa.types.is_unsigned_integer(t):
        return None, (
            "the spec's integer kinds are signed (`int`, `long`) and Iceberg has no unsigned type "
            "either — widen it on the way out of whatever wrote this file"
        )
    if pa.types.is_list(t) or pa.types.is_large_list(t) or pa.types.is_struct(t) or pa.types.is_map(t):
        return None, "the spec has no name for this type — §1 defers `array<T>`, `struct` and `map`"
    return None, "the spec has no name for this type (§1)"


# ---- building the draft ----------------------------------------------------------


def infer_draft(
    source: str | Path,
    api_name: str,
    *,
    fmt: str = "parquet",
    catalog: str | None = None,
    table: str | None = None,
    key: str | None = None,
    entry: str | None = None,
) -> Draft:
    """One file plus the two things it cannot contain: what to call this, and where it lives.

    `key` names a *source column*, not a property, because at the point somebody runs this the
    property does not exist yet — they are looking at a file.

    `--as` is checked against §0's identifier grammar before anything is read. It is the one value
    here that comes from the caller rather than the file, and it was the one value nothing checked:
    `--as "not a name"` and `--as daily_sales` both drafted happily and printed *"the placeholders
    are answered — this draft validates as it stands"*, which `loom validate` then refused. A
    scaffold whose whole contract is "this validates" must not be able to emit something that
    doesn't."""
    problem = identifier_problem("objectType apiName", api_name, OBJECT_NAME)
    if problem is not None:
        raise InferError(
            f"{problem}. '--as' names the objectType this draft declares, so it has to be a name a "
            f"spec can hold"
        )
    columns = read_columns(source, fmt)

    names: dict[str, str] = {}
    for column in columns:
        prop = _property_name(column.name)
        if prop in names:
            raise InferError(
                f"columns '{names[prop]}' and '{column.name}' both read as the property "
                f"'{prop}' — rename one at the source, or write the draft by hand"
            )
        names[prop] = column.name

    if key is not None and key not in {c.name for c in columns}:
        raise InferError(f"'{source}' has no column '{key}' to use as the primary key")
    if key is not None:
        chosen = next(c for c in columns if c.name == key)
        if not chosen.mapped:
            raise InferError(
                f"column '{key}' cannot be a primary key: {chosen.refusal}"
            )

    return Draft(
        api_name=api_name,
        source=str(source),
        columns=columns,
        catalog=catalog or TODO_CATALOG,
        table=table or TODO_TABLE,
        primary_key=_property_name(key) if key else None,
        entry=entry,
    )


def _property_name(column: str) -> str:
    """`full_name` -> `fullName`. A reading of the column, never a rename of it.

    The physical name is carried through to `column:` verbatim, so this transformation is visible on
    every line it touches and reversible by deleting one word. Which is the only defensible way to
    guess a name: guess loudly, next to the thing you guessed from."""
    parts = [p for p in column.strip("_").split("_") if p]
    if not parts:
        return column
    head, *rest = parts
    return head[:1].lower() + head[1:] + "".join(p[:1].upper() + p[1:] for p in rest)


def _kebab(api_name: str) -> str:
    out = []
    for i, ch in enumerate(api_name):
        if ch.isupper() and i:
            out.append("-")
        out.append(ch.lower())
    return "".join(out)


# ---- rendering -------------------------------------------------------------------


def render_draft(draft: Draft) -> str:
    """The draft as two documents: the objectType file, and the `ingest:` entry to paste.

    Two, because they belong in different files — an objectType is a fact about the ontology and
    goes in the spec directory, while a load is a fact about a deployment and goes in `loom.yaml`.
    Printing them together and saying which is which beats printing the one somebody asked for and
    letting them find out about the other from an error."""
    lines: list[str] = []
    lines += _header(draft)
    lines += _object_type(draft)
    lines.append("")
    lines += _ingest_entry(draft)
    return "\n".join(lines) + "\n"


def _header(draft: Draft) -> list[str]:
    todos = []
    if draft.primary_key is None:
        todos.append("`primaryKey` — no file knows which column addresses a row")
    if draft.catalog == TODO_CATALOG or draft.table == TODO_TABLE:
        todos.append("`backing` — the catalog and table this type reads, named in loom.yaml")
    todos.append("`title` — defaults to the primary key; a human-readable property is usually better")
    todos.append("`searchable` — nothing here is filterable until it is listed there")
    todos.append("enum types — a file shows the values it happens to hold, not the domain's set")
    return [
        f"# Drafted by `loom infer` from {draft.source}.",
        "#",
        "# Read from the file's declared schema: column names, types, and nullability. Nullability",
        "# is what the schema *says* rather than whether this file happens to hold a null, and most",
        "# writers declare everything nullable — so expect this draft to be more permissive than",
        "# the domain, and tighten it here rather than discovering it at a write.",
        "#",
        "# Still to decide, none of which a file can answer:",
        *[f"#   - {t}" for t in todos],
        "#",
        _closing(draft),
    ]


def _closing(draft: Draft) -> str:
    """The last line of the header, which has to agree with `loom validate`.

    It used to say *it does not validate yet* unconditionally, which was false for the invocation
    the guide's own example uses — `--key`, `--catalog` and `--table` leave no placeholder behind,
    and the draft validates. The prompts above stay either way, because they are still worth
    answering; what changes is whether this line claims a refusal that is not going to happen."""
    if draft.blocking:
        return "# It does not validate yet, on purpose. Fill the TODOs in, then `loom validate`."
    return (
        "# The placeholders are answered, so this validates as it stands — run `loom validate`. "
        "Everything\n# above is still worth a pass by hand: a draft that validates is not yet a "
        "spec somebody meant."
    )


def _object_type(draft: Draft) -> list[str]:
    lines = [
        "objectType:",
        f"  apiName: {draft.api_name}",
        f"  displayName: {draft.api_name}",
        f"  primaryKey: {draft.primary_key or TODO_PRIMARY_KEY}",
        f"  backing: {{ catalog: {draft.catalog}, table: {draft.table} }}",
        "  properties:",
    ]
    for column in draft.columns:
        if column.mapped:
            lines.append(f"    - {{ {_property(draft, column)} }}")
        else:
            lines += _left_out(column)
    if draft.unmapped:
        lines += _rule_seven()
    return lines


def _property(draft: Draft, column: Column) -> str:
    name = _property_name(column.name)
    spec = dict(column.spec or {})
    parts = [f"name: {name}", f"type: {spec.pop('type')}"]
    parts += [f"{k}: {v}" for k, v in spec.items()]
    parts.append(f"column: {column.name}")
    if column.nullable and name != draft.primary_key:
        parts.append("nullable: true")
    if name == draft.primary_key:
        parts.append("unique: true")
    return ", ".join(parts)


def _left_out(column: Column) -> list[str]:
    """A column with no property, said out loud in the place the property would have been.

    Where, rather than in a summary at the end, because position is the whole point: a reader
    scanning the properties list sees the gap at the line the column would have occupied, and a
    diff of two drafts shows a type becoming unmappable at the column it happened to."""
    return _comment(f"{column.name} ({column.physical}) — not mapped: {column.refusal}", indent=4)


def _rule_seven() -> list[str]:
    """Said once, after the list, rather than once per column.

    Not an error and not a silence. §2 rule 7 is that a column the ontology does not map is somebody
    else's data: `plan` reports it and leaves it alone, and every write carries it across untouched.
    So the honest rendering of "no type for this" is a note about what happens to it anyway — and
    repeating that paragraph under every unmapped column would bury the columns in it."""
    return _comment(
        "The unmapped columns above are not a gap. A column no property claims is unmanaged: "
        "`loom plan` reports it, nothing ever drops it, and every write carries it across "
        "untouched (§2 rule 7). What it cannot be is loaded — a source column no property claims "
        "is refused at load time rather than dropped, so an `ingest:` entry for this type reads a "
        "file without them, and their own writer keeps filling them.",
        indent=4,
    )


def _comment(text: str, *, indent: int) -> list[str]:
    """Wrap prose into a YAML comment block. The one piece of formatting worth doing properly:
    an unwrapped 300-character refusal is a refusal nobody reads to the end of."""
    import textwrap

    pad = " " * indent
    return [f"{pad}# {line}" for line in textwrap.wrap(text, width=96 - indent) or [""]]


def _ingest_entry(draft: Draft) -> list[str]:
    """The load that would fill the table, commented out and addressed to the other file."""
    renamed = [
        (_property_name(c.name), c.name) for c in draft.mapped if _property_name(c.name) != c.name
    ]
    lines = [
        "# ---- and in loom.yaml, if this file is also how the table gets filled ----------------",
        "#",
        "# `mode` is required and never defaulted: the three modes differ in what they *destroy*,",
        "# so a default would make the safest reading of an under-specified config the one nobody",
        "# wrote down. `format` is required for the matching reason — a `.csv` that is really TSV",
        "# guesses wrong, and the guess would be made per invocation rather than reviewed once.",
        "#",
        "# ingest:",
        f"#   - name: {draft.entry_name}",
        f"#     objectType: {draft.api_name}",
        f"#     mode: {TODO_MODE}",
        "#     format: parquet",
    ]
    if renamed:
        lines.append("#     columns:")
        lines += [f"#       {prop}: {column}" for prop, column in renamed]
    else:
        lines.append("#     # every property reads a column of its own name, so no `columns:` block")
    lines += [
        "#",
        "# And `governance.ingest: allowed`, which defaults to `refused` — declaring a load and",
        "# performing one are two decisions.",
    ]
    if draft.unmapped:
        lines += [
            "#",
            "# The unmapped columns above must not be in the file this entry loads: a source column",
            "# no property claims is refused at load time rather than dropped.",
        ]
    return lines
