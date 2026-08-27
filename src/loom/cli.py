"""`loom` CLI.

`validate` is structural and offline by default; `--physical` adds the catalog pass. `query` is a
dev command for exercising the read path by hand, and `serve` exposes those same reads as MCP
tools. `plan` dry-runs the migration engine, `apply` executes exactly what `plan` printed, and
`rollback` restores a spec out of `_loom_meta` and re-plans it — the same loop, an older spec.
`run` is `query`'s counterpart on the write path: one declared action, through the same runtime
M4's `run_<action>` tool will call. `ingest` is `run` at batch scale and the one command with no
tool behind it: a declared load, from a file, checked against the ontology and written as one commit
— deliberately reachable only from here, because a verb that writes an arbitrary batch is not
something any agent surface should be able to name. `sequence` is `ingest` in an order: several
declared loads from one manifest, stopping at the first refusal and reporting what landed, because
Iceberg's unit is the table and there is no cross-table transaction to pretend to. `infer` runs
before any of them and is the only command that reads a schema instead of being told one: it drafts
a spec from a file and prints it, touching no catalog and writing nothing.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import CONFIG_FILENAME, find_config, load_config
from .errors import Diagnostics, SourceLoc, SpecError, SpecErrors

# Module-level, unlike every other command's imports, because `main()` builds the parser before it
# knows which command is running — and `infer`'s `--format` choices are the refusal list itself.
# Cheap enough to be unremarkable: `infer` imports pyarrow inside the function that needs it.
from .infer import INFER_FORMATS, UNREADABLE_FORMATS
from .model import Ontology
from .ontology import build


def _missing_config(path: str) -> SpecError:
    return SpecError(
        f"no {CONFIG_FILENAME} found for ontology '{path}'",
        SourceLoc(path),
        "create one beside the ontology directory — see docs/spec-v0.md §6",
    )


def _load_project(path: str, diag: Diagnostics):
    """Resolve the ontology and its loom.yaml together, so one run reports problems from both.

    `build()` raises on spec errors, so its bundle is folded into `diag` rather than propagated —
    otherwise a project with both a broken spec and a broken config would only ever show the spec
    half, and you'd fix it only to hit the config half on the next run."""
    config_path = find_config(path)
    if config_path is None:
        raise SpecErrors([_missing_config(path)])
    config = load_config(config_path, diag)
    try:
        ontology, ont_diag = build(path)
    except SpecErrors as e:
        diag.errors.extend(e.errors)
        diag.raise_if_errors()
        raise  # unreachable: diag now holds at least e.errors
    diag.warnings.extend(ont_diag.warnings)
    diag.raise_if_errors()
    return ontology, config


def cmd_infer(args) -> int:
    """Draft an objectType from a file and print it. The only command that loads no project.

    Note what it does not take: an ontology directory. There is nothing to read one for — the draft
    is a new type, and a command that opened `loom.yaml` here would be a command that could be made
    to consult a catalog later. The path it does not take is the reason it can never migrate
    anything, which is stronger than the same claim made in a docstring.

    Everything goes to stdout so it can be redirected into a file. That is two deliberate steps —
    redirect, then edit — and the draft's placeholders are what make the second one unavoidable."""
    from .infer import InferError, infer_draft, render_draft

    try:
        draft = infer_draft(
            args.source,
            args.api_name,
            fmt=args.format,
            catalog=args.catalog,
            table=args.table,
            key=args.key,
            entry=args.entry,
        )
    except InferError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(render_draft(draft), end="")
    unmapped = draft.unmapped
    if unmapped:
        names = ", ".join(c.name for c in unmapped)
        print(
            f"note: {len(unmapped)} column(s) have no property — {names}. They are unmanaged, not "
            f"lost: see the comments above each one.",
            file=sys.stderr,
        )
    if draft.blocking:
        print(
            "note: this draft does not validate yet — fill in the TODOs, then run 'loom validate'.",
            file=sys.stderr,
        )
    else:
        # `--key`, `--catalog` and `--table` are the placeholders answered on the command line, so
        # there is nothing left to refuse this draft. Saying otherwise would put this command in
        # contradiction with `loom validate`, which is the one thing a scaffold must not do — the
        # header's remaining prompts are questions worth answering, not a failure waiting to happen.
        print(
            "note: the placeholders are answered — this draft validates as it stands. Read it "
            "before you commit it: `title`, `searchable` and any enum are still unanswered.",
            file=sys.stderr,
        )
    return 0


def cmd_validate(args) -> int:
    # Bound before the `try`, not inside the `--physical` branch, so the `except` clause below has
    # the name whichever half ran. It costs nothing: `loom.catalog` reaches pyiceberg only through
    # `factory`, which imports it per catalog type at open time.
    from .catalog import CatalogError

    diag = Diagnostics()
    suffix = ""
    try:
        if args.physical:
            from .catalog import open_catalogs
            from .loader import load_dir
            from .validator import check_physical, validate

            config_path = find_config(args.path)
            if config_path is None:
                raise SpecErrors([_missing_config(args.path)])
            config = load_config(config_path, diag)
            loaded = load_dir(args.path, diag)
            validate(loaded, diag)
            # Structural errors first: introspecting backing tables for a spec that doesn't parse
            # produces noise, not information.
            diag.raise_if_errors()
            check_physical(loaded, open_catalogs(config), diag)
            diag.raise_if_errors()
            # Already loaded and fully validated above — re-parsing via build() would just read
            # every file a second time.
            ontology = Ontology(
                object_types=loaded.objects, link_types=loaded.links, actions=loaded.actions
            )
            suffix = f" · physical ok against {len(config.catalogs)} catalog(s)"
        else:
            # `loom.yaml` is checked here too, and not only under `--physical`. Without this the one
            # command whose whole job is to answer *is this good?* was the only one that never read
            # the deployment half: an unknown embedding provider, a stray `dims:`, an engine type
            # nothing implements, a `sequences:` entry naming an ingest entry that does not exist —
            # every one of those printed `ok` here and `1 problem in ontology spec: loom.yaml: ...`
            # from every other verb, including `validate --physical`. The error machinery already
            # calls a config problem a spec problem; this is the command that agrees with it.
            #
            # **Found, not required.** A missing config stays out of `diag` because the spec half is
            # meaningful on its own — `loom validate tests/fixtures/valid` is the guide's first
            # command and there is no `loom.yaml` beside it. What changes is that `ok` stops
            # overclaiming: the suffix says which halves were read, so a clean spec and an unread
            # config are no longer the same sentence. `--physical` still *requires* one, because
            # catalogs are named in it and there is nothing to check against without them.
            config_path = find_config(args.path)
            if config_path is not None:
                load_config(config_path, diag)
                # Named rather than assumed. `find_config` looks in the ontology dir, then beside
                # it, then in the working directory — so the file checked here is not always the one
                # the caller had in mind, and a bare "loom.yaml ok" would be the same sentence for
                # all three. Every other verb reads it from the same three places and says nothing;
                # this is the command whose whole output is *what was checked*.
                suffix = f" · {config_path} ok"
            else:
                suffix = " · no loom.yaml found — spec checked, deployment not"
            ontology, ont_diag = build(args.path)
            diag.warnings.extend(ont_diag.warnings)
            diag.raise_if_errors()
    except SpecErrors as e:
        print(str(e), file=sys.stderr)
        return 1
    except CatalogError as e:
        # `--physical` is the only half of this command that opens anything, and a catalog that
        # will not open is the ordinary state of a checkout nobody has seeded yet — it is the first
        # thing the guide's reader hits. `CatalogError` already carries the operator's hint; every
        # other catalog-touching command in this module catches it, and this one not doing so
        # printed that hint at the bottom of a stack trace.
        print(f"error: {e}", file=sys.stderr)
        return 1
    for w in diag.warnings:
        print(f"warning: {w.render()}", file=sys.stderr)
    print(f"ok — {ontology.summary()}{suffix}")
    if diag.warnings:
        # Alongside the warnings themselves, so stdout carries only the result line.
        print(f"({len(diag.warnings)} warning(s))", file=sys.stderr)
    return 0


def cmd_query(args) -> int:
    """Exercise the read path without an MCP client. Mirrors the generated tools deliberately —
    if this can do something the tools can't, the ontology has a back door."""
    diag = Diagnostics()
    try:
        ontology, config = _load_project(args.path, diag)
    except SpecErrors as e:
        print(str(e), file=sys.stderr)
        return 1

    from .catalog import CatalogError, open_catalogs
    from .embed import EmbeddingError
    from .governance import PolicyError
    from .mcp.registry import json_safe
    from .negotiate import CapabilityError
    from .resolver import ResolverError, build_resolver

    # Argument shape first, before anything opens a catalog — a typo'd flag shouldn't need a
    # reachable metastore to be reported.
    if args.link and not args.key:
        print("error: --link requires --key", file=sys.stderr)
        return 1
    # A ranked read and a keyed one are different verbs, not two halves of one. `--match --key` has
    # no meaning to guess at: a similarity over the one row you already named is a number about
    # nothing. Refused here rather than resolved by precedence, which would silently answer the
    # question the caller did not ask.
    if args.match is not None and (args.key or args.link):
        print("error: --match cannot be combined with --key or --link", file=sys.stderr)
        return 1
    # `via` is an argument of `match_<object>` and of nothing else, so the dev command has it in
    # exactly the same place. Mirroring the generated tools is this command's whole job: offering a
    # cross-object filter on a read the surface does not offer one on would be a back door with a
    # tidy spelling.
    if args.via and args.match is None:
        print("error: --via requires --match", file=sys.stderr)
        return 1
    # A key addresses a row; a filter selects among rows. `get_<type>` and `traverse` take no
    # `filter` on the generated surface, so there is no tool shape for this command to mirror — and
    # the branch below reaches `resolver.get`/`resolver.traverse`, which never saw `filters` at all.
    # Refused rather than ignored, because the ignoring was silent and reads as confirmation: a
    # `--key c1 --filter tier=bronze` answered with the gold-tier `c1` says a filter selected that
    # row, which is the sentence an operator checking a predicate against a known row is looking for.
    # `get_<type>` takes no page, so an `--offset` beside a bare `--key` has no tool shape to mirror
    # and would be read as *skip, then fetch that row* — which is not a thing this command can do.
    # A `--key --link` traverse does page, so that combination is left alone.
    if args.offset and args.key and not args.link:
        print(
            "error: --offset cannot be combined with --key — a key addresses one row, and there is "
            "no page to skip into. Use --offset with a search, or with --key --link",
            file=sys.stderr,
        )
        return 1
    if args.filter and args.key:
        print(
            "error: --filter cannot be combined with --key — a key addresses one row and a filter "
            "selects among rows. Drop --key to filter, or drop --filter to fetch that row",
            file=sys.stderr,
        )
        return 1
    try:
        filters = _filter_pairs(args.filter or [], "--filter")
        via = _via_pairs(args.via or [])
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    ranked = None
    try:
        # Opened once and handed to both, so a `--match` does not build a second connection to the
        # same lake and get a second opinion about what is in it.
        open_cats = open_catalogs(config)
        resolver = build_resolver(ontology, config, open_cats)
        if args.match is not None:
            ranked = _cli_match(ontology, config, open_cats, resolver, args, filters, via)
            # The tool's shape, not a flattened one: `score` beside the object rather than merged
            # into it, so this command shows what `match_<object>` shows. Merging would also
            # reintroduce the collision `Resolver.match` exists to keep impossible.
            rows = [{"score": m.score, "object": m.object} for m in ranked.matches]
        elif args.key and args.link:
            rows = resolver.traverse(
                args.object_type, args.key, args.link, limit=args.limit, offset=args.offset
            )
        elif args.key:
            row = resolver.get(args.object_type, args.key)
            rows = [row] if row else []
        else:
            rows = resolver.search(
                args.object_type, filters, limit=args.limit, offset=args.offset
            )
    except (ResolverError, CatalogError, CapabilityError, PolicyError, EmbeddingError) as e:
        # A `CapabilityError` reaches here for the same reason `loom query` mirrors the generated
        # tools at all: if the dev command can read out of an engine the served surface refuses to
        # stand on, the ontology has a back door. A `PolicyError` is the same sentence about a
        # deployment instead of an engine, and an `EmbeddingError` the same sentence about a model.
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(json.dumps(json_safe(rows), indent=2, default=str))
    # The mask, on stderr beside the row count rather than in the JSON: this command prints rows
    # rather than an envelope, and an agent is not reading it. What matters is that it is *said* —
    # the withholding itself already happened one layer down, identically to the tool path.
    read = args.object_type if not args.link else resolver.link_direction(args.object_type, args.link).target_object_type
    masked = resolver.masked(read)
    if masked:
        print(f"({read}: {', '.join(masked)} withheld by governance policy)", file=sys.stderr)
    if ranked is not None:
        # The two facts the tool's envelope carries and a bare list of rows cannot: what model the
        # ranking is relative to, and how current the oldest vector behind it is.
        stamp = _zulu(ranked.embedded_as_of) if ranked.embedded_as_of else "never"
        # The third fact, and the only one that is about the rows above being *wrong*: a stale row
        # is returned carrying the text it has now, scored by the text it had then, so nothing in
        # the JSON above distinguishes it from a good hit.
        behind = (
            f" · {ranked.stale_matches} of these ranked by text the row no longer has — "
            f"`loom embed`"
            if ranked.stale_matches
            else ""
        )
        print(
            f"(ranked by {ranked.object_type}.{ranked.property} against '{ranked.model}' · "
            f"oldest vector here embedded {stamp}{behind})",
            file=sys.stderr,
        )
    print(f"({len(rows)} row(s))", file=sys.stderr)
    return 0


def _filter_pairs(pairs: list[str], flag: str) -> dict[str, Any]:
    """`PROP=VALUE` and `PROP.OP=VALUE` as the filter object the generated tool takes.

    The two spellings the tool accepts, in the one encoding a shell has. A property name cannot
    contain a dot, so the split is unambiguous; a null filter is the one thing not expressible here,
    because every CLI value is a string.

    `in` takes a list, and the list is built by **repeating the flag** — `--filter tier.in=gold
    --filter tier.in=platinum` — rather than by splitting one value on a separator. A comma is a
    legal character inside a string value, so `tier.in=a,b` would have to either forbid it or
    silently split one value into two wrong ones, and this command's job is to mirror what the
    generated tool would do with the same filter.

    **One function for two flags**, which is `via`'s doing: a hop takes the identical grammar
    against a different type, so `--via placedBy.tier=gold` has to parse the way `--filter tier=gold`
    parses or the dev command would have two filter grammars where the surface has one. `flag` is
    only what the refusals name."""
    from .filters import MEMBERSHIP

    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"{flag} expects PROP=VALUE or PROP.OP=VALUE, got '{pair}'")
        name, value = pair.split("=", 1)
        name, _, op = name.partition(".")
        if not op:
            if name in out:
                raise ValueError(f"{flag} gives '{name}' both a bare value and operators")
            out[name] = value
        elif isinstance(out.setdefault(name, {}), dict):
            ops = out[name]
            if op == MEMBERSHIP:
                ops.setdefault(op, []).append(value)
            elif op in ops:
                # Only `in` accumulates. Repeating any other operator used to keep the last value
                # silently, which is a filter the caller did not write being answered as if they had.
                raise ValueError(f"{flag} gives '{name}.{op}' twice")
            else:
                ops[op] = value
        else:
            raise ValueError(f"{flag} gives '{name}' both a bare value and operators")
    return out


def _via_pairs(pairs: list[str]) -> dict[str, Any]:
    """`LINK`, `LINK.PROP=VALUE` and `LINK.PROP.OP=VALUE` as the `via` object the tool takes.

    One level deeper than `--filter` and with one spelling of its own: a bare `--via orders` is the
    existence test, `{}`, which is what the argument means when a hop names no filters. It has to be
    expressible here or the flag could say *orders from a gold customer* and not *orders that have a
    customer at all*, and the second is the one a to-many link makes interesting."""
    hops: dict[str, list[str]] = {}
    shape = "--via expects LINK, LINK.PROP=VALUE or LINK.PROP.OP=VALUE"
    for pair in pairs:
        link, dot, rest = pair.partition(".")
        # Checked on both branches, not only the bare one. A link name cannot contain `=`, and
        # `--via 'orders=x.total=5'` splits at the *dot* — so testing it only where there is no dot
        # let a malformed flag through as a link literally named `orders=x`, to be refused by the
        # resolver after a catalog had been opened. This check exists to happen before that.
        if "=" in link:
            raise ValueError(f"{shape}, got '{pair}' — a hop names a link before the property")
        if not dot:
            hops.setdefault(link, [])
        elif "=" not in rest:
            raise ValueError(f"{shape}, got '{pair}'")
        else:
            hops.setdefault(link, []).append(rest)
    return {link: _filter_pairs(ps, f"--via {link}") for link, ps in hops.items()}


def _zulu(when: datetime) -> str:
    """An `embedded_at` as a UTC wall clock, because the `Z` on the end has to be earned.

    Both commands that print one read it back through a different stack — `loom embed` off pyarrow,
    `loom query --match` off DuckDB — and DuckDB converts a `timestamptz` to the *host's* zone, so a
    vector embedded at 09:14Z printed as `05:14Z` on a machine in New York. One function so the two
    cannot disagree about the same value, and a conversion rather than a relabelling so the letter
    is true."""
    return f"{when.astimezone(UTC):%Y-%m-%d %H:%M}Z"


def _cli_match(ontology, config, catalogs, resolver, args, filters, via):
    """`--match` through the same `Matcher` the tool uses, or the refusal a deployment has earned.

    A separate function only because it has one refusal of its own: `bind_matching` answers `None`
    for a deployment that cannot rank, which over MCP means *no tool was generated* and here has to
    become a sentence. That is this command's rule read from an angle it had not been read at
    before — it mirrors the generated tools, so where the surface exposes nothing it must not
    quietly do something."""
    from .embed.match import bind_matching
    from .resolver import ResolverError

    matcher = bind_matching(ontology, config, catalogs)
    if matcher is None and config.mcp.embedding is None:
        raise ResolverError(
            "this deployment configures no 'mcp.embedding' provider, so nothing here ranks by "
            "meaning — add it to loom.yaml. `loom serve` generates no match_ tool either, which is "
            "the same answer said as an absence"
        )
    if matcher is None:
        raise ResolverError(
            "no objectType in this ontology declares a 'semantic:' property, so there is nothing "
            "to rank by meaning"
        )
    return matcher.match(
        resolver, args.object_type, args.match, filters, via, limit=args.limit, offset=args.offset
    )


def cmd_run(args) -> int:
    """Run one declared action. The write path's `loom query`, and under the same rule.

    `loom query` mirrors the generated read tools deliberately — if the dev command can do
    something the tools can't, the ontology has a back door. That test is stronger here, because
    this one writes: so it takes an action apiName and named parameters, exactly the shape M4's
    `run_<action>` tool will take, and calls the same `ActionRuntime.run`. It cannot name a table,
    a column, or a predicate, because the runtime has no argument for one.

    It does pass one thing the tool will pass differently: the actor. `default_actor()` lives here
    rather than in the runtime because *here* is where it is true — a person at a terminal, or a CI
    job that set `LOOM_ACTOR`. Over MCP the same string would name whoever started `loom serve`, so
    `run_<action>` will pass what its transport authenticated, through this same argument."""
    diag = Diagnostics()
    try:
        ontology, config = _load_project(args.path, diag)
    except SpecErrors as e:
        print(str(e), file=sys.stderr)
        return 1

    from .action import LOG_FAILED, ActionError, build_runtime
    from .catalog import EDIT_LOG_TABLE, CatalogError, open_catalogs
    from .governance import PolicyError
    from .mcp.registry import json_safe
    from .migrate.meta import default_actor

    # Argument shape before anything opens a catalog, as `loom query` does — a typo'd flag should
    # not need a reachable metastore to be reported.
    parameters: dict[str, str] = {}
    for pair in args.param or []:
        if "=" not in pair:
            print(f"error: --param expects NAME=VALUE, got '{pair}'", file=sys.stderr)
            return 1
        name, value = pair.split("=", 1)
        parameters[name] = value

    try:
        # `build_runtime`, not a runtime built here: it is the one function that pairs this spec
        # with this deployment on the write plane, so `loom run` withholds exactly what a served
        # `run_<action>` withholds. Building one directly is how a dev command becomes the ungoverned
        # path.
        runtime = build_runtime(ontology, config, open_catalogs(config))
        # Always previewed first, even for a real run: it is the same four steps minus the write,
        # so what the prompt shows is what is about to happen rather than a second guess at it. It
        # is a preview and not a recording — an effect holding `now()` gets a fresh value on the
        # run, for the same reason `loom apply` re-plans instead of replaying a saved plan.
        preview = runtime.run(args.action, parameters, dry_run=True)
    except (ActionError, PolicyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except CatalogError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(_render_run(preview, str(args.path)), file=sys.stderr)
    if not preview.ok or args.dry_run:
        print(json.dumps(json_safe(preview.as_json()), indent=2, default=str))
        return 0 if preview.ok else 1

    if not _confirmed(args.yes, "run"):
        print("aborted — nothing was written", file=sys.stderr)
        return 1

    try:
        result = runtime.run(args.action, parameters, actor=default_actor())
    except CatalogError as e:  # pragma: no cover - the runtime folds these into WRITE_FAILED
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(json_safe(result.as_json()), indent=2, default=str))
    for failure in result.failures:
        print(f"error: {failure.code}: {failure.message}", file=sys.stderr)
    unlogged = any(f.code == LOG_FAILED for f in result.failures)
    if result.edit_id and not unlogged:
        print(f"note: recorded in {EDIT_LOG_TABLE} as {result.edit_id}.", file=sys.stderr)
    elif unlogged:
        # The id is still worth printing: the row write stamped it into its own Iceberg commit, so it
        # is how someone finds this write in the table's history now that the log has not got it.
        print(
            f"note: the edit log did not record this run — the commit it stamped carries "
            f"{result.edit_id}.",
            file=sys.stderr,
        )
    elif not result.edit_id:
        # Said out loud rather than left to silence. For a refusal that never named an object this is
        # correct and deliberate; anywhere else it is worth noticing.
        print("note: nothing was recorded in the edit log — this run named no object.", file=sys.stderr)
    if result.read_snapshot_id != preview.read_snapshot_id:
        # The one thing the preview promised to tell them. The CLI is the only caller with a
        # before-and-after to compare, so it is the only one that can say the thinking time
        # mattered — and staying quiet would let the previewed diff pass for what was applied.
        print(
            f"note: the table moved while you decided (previewed at {preview.read_snapshot_id}, "
            f"ran at {result.read_snapshot_id}) — the result above is what happened, not the "
            f"preview above it.",
            file=sys.stderr,
        )
    if result.attempts > 1:
        print(f"note: {result.attempts} attempts — the row was contended.", file=sys.stderr)
    print(f"{result.status} · {result.object_type} {result.key!r}", file=sys.stderr)
    return 0 if result.ok else 1


def _render_run(result, title: str) -> str:
    """What is about to happen, before the prompt. Deliberately shaped like `render_plan`: an
    action is a one-row migration, and the reader's job is the same one — decide whether to run it,
    so the symbols carry the same meanings (`+` adds, `~` changes in place, `-` goes away).

    The shape comes from the *operation*, not from whether there is an `after`: a refused modify
    also has no `after`, and rendering it as a delete would be the most alarming possible way to
    say "nothing happened"."""
    from .mcp.registry import json_safe

    def show(value) -> str:
        return json.dumps(json_safe(value), default=str)

    lines = [f"Loom run — {result.action} on {title}", ""]
    # A run that was refused before it could bind a key has no key, and `null` is not the name of
    # one. It read `modify Customer null`, which is a sentence about a row rather than about the
    # binding that never happened — and the failure underneath already says which parameter was
    # missing. Say what is true instead: the target is undetermined.
    target = show(result.key) if result.key is not None else "(no key bound)"
    lines.append(f"  {result.operation} {result.object_type} {target}")
    before, after = result.before or {}, result.after or {}
    if result.operation == "delete":
        for name in sorted(before):
            lines.append(f"      - {name}  {show(before[name])}")
    elif result.operation == "create":
        for name in sorted(after):
            lines.append(f"      + {name}  {show(after[name])}")
    else:
        for name in sorted(set(before) | set(after)):
            if result.after is not None and before.get(name) != after.get(name):
                lines.append(f"      ~ {name}  {show(before.get(name))} -> {show(after.get(name))}")
    if result.read_snapshot_id is not None:
        # Said out loud, because a snapshot id printed above a y/N prompt would otherwise read as a
        # hold on the table — as if answering slowly were safe because this version was reserved.
        # It is not reserved: this is a preview, and the run that follows does its own read and
        # asserts *that*. So what the prompt asks a person to approve is the shape of the change,
        # which is the only thing it can honestly ask about, and the only thing `run_<action>` —
        # which has no prompt at all — could ever be said to approve either.
        lines.append(f"\n  previewed at snapshot {result.read_snapshot_id} — nothing is held:")
        lines.append("  the run reads again and asserts that read, so a row that moves while you")
        lines.append("  decide is a conflict you are told about, never a silent overwrite.")
    for failure in result.failures:
        lines.append(f"\n  ! {failure.code}: {failure.message}")
    if result.failures:
        lines.append("  nothing was written.")
    return "\n".join(lines)


def cmd_ingest(args) -> int:
    """Run one declared bulk load. `loom run`'s counterpart at batch scale.

    It takes an **entry name and a file**, and nothing else that describes the load: not a table, not
    a mode, not a column mapping. Those live in `loom.yaml` because a load is a fact about a
    deployment, and a flag that could contradict the reviewed file is the thing `mcp.writes` refused
    to be. What the command line carries is what genuinely varies per invocation — which file, and
    the three operator decisions below.

    `--load-id` overrides the id derived from the entry, the mode and the file's bytes. It is how an
    operator says *yes, this file again, on purpose* — the one thing the derived id would otherwise
    refuse forever.

    `--reject-to` turns whole-batch refusal into quarantine-and-continue for the failures that are a
    row's own. It cannot rescue a load whose columns are wrong, and the refusals that survive it say
    so by name.

    `--dry-run` previews. Every real load previews first anyway, for `cmd_run`'s reason: what the
    prompt shows should be what is about to happen rather than a second guess at it."""
    diag = Diagnostics()
    try:
        ontology, config = _load_project(args.path, diag)
    except SpecErrors as e:
        print(str(e), file=sys.stderr)
        return 1

    from .catalog import LOAD_LOG_TABLE, CatalogError, open_catalogs
    from .governance import PolicyError
    from .ingest import LOG_FAILED, IngestError, build_ingest
    from .mcp.registry import json_safe
    from .migrate.meta import default_actor

    try:
        runtime = build_ingest(ontology, config, open_catalogs(config))
        # `reject_to` is passed to the preview as well as to the run, and it has to be: without it
        # the preview refuses any batch with a single bad row, the command exits on that refusal, and
        # the flag whose whole purpose is to *avoid* whole-batch refusal never reaches the load it
        # was meant to change.
        preview = runtime.load(
            args.entry,
            args.source,
            load_id=args.load_id,
            dry_run=True,
            reject_to=args.reject_to,
        )
    except (IngestError, PolicyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except CatalogError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(_render_load(preview, str(args.path)), file=sys.stderr)
    if args.dry_run:
        print(json.dumps(json_safe(preview.as_json()), indent=2, default=str))
        return 0 if preview.ok else 1

    # A refused preview still runs for real, and there is nothing to confirm first because a refusal
    # writes nothing. It runs because a preview records nothing — so without this, every refusal an
    # operator hit from the command line would be absent from `_loom_meta.loads`, and *who tried to
    # replace this table* would be answerable only for loads that succeeded.
    if preview.ok and not _confirmed(args.yes, "load"):
        print("aborted — nothing was written", file=sys.stderr)
        return 1

    try:
        result = runtime.load(
            args.entry,
            args.source,
            actor=default_actor(),
            load_id=args.load_id,
            reject_to=args.reject_to,
        )
    except CatalogError as e:  # pragma: no cover - the runtime folds these into WRITE_FAILED
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(json.dumps(json_safe(result.as_json()), indent=2, default=str))
    for failure in result.failures:
        # A quarantined row is not an error this run had: the operator asked for those rows to be
        # set aside, they were, the load applied and the exit code is 0. Printing `error:` in front
        # of them made a successful `--reject-to` load indistinguishable from a refused one to
        # anything reading this output, which is every pipeline that runs this command.
        label = "rejected" if _quarantined(result, failure) else "error"
        print(f"{label}: {failure.code}: {failure.message}", file=sys.stderr)
    unlogged = any(f.code == LOG_FAILED for f in result.failures)
    if result.load_id and not unlogged:
        print(f"note: recorded in {LOAD_LOG_TABLE} as {result.load_id}.", file=sys.stderr)
    elif unlogged:
        # The id is still worth printing: the write stamped it into its own Iceberg commit, so it is
        # how someone finds this load in the table's history now that the log has not got it.
        print(
            f"note: the load log did not record this load — the commit it stamped carries "
            f"{result.load_id}.",
            file=sys.stderr,
        )
    if result.rows_rejected:
        print(
            f"note: {result.rows_rejected} row(s) were rejected and written to {args.reject_to} — "
            f"they are not in the table.",
            file=sys.stderr,
        )
    if result.read_snapshot_id != preview.read_snapshot_id:
        print(
            f"note: the table moved while you decided (previewed at {preview.read_snapshot_id}, "
            f"loaded at {result.read_snapshot_id}) — the result above is what happened, not the "
            f"preview above it.",
            file=sys.stderr,
        )
    print(
        f"{result.status} · {result.rows_written} row(s) into {result.table}", file=sys.stderr
    )
    return 0 if result.ok else 1


def _render_load(result, title: str) -> str:
    """What is about to happen, before the prompt.

    Shaped like `_render_run` and `render_plan`, and the symbol carries the same meaning it does
    there: `+` adds, `~` changes in place, `-` goes away. A mode is exactly a statement about which
    of those three a batch does to the rows already in the table, so the three modes get the three
    symbols rather than a word an operator has to translate."""
    marks = {"append": "+", "merge": "~", "replace": "-"}
    lines = [f"Loom ingest — {result.entry} on {title}", ""]
    lines.append(
        f"  {marks.get(result.mode, '?')} {result.mode} {result.rows_read} row(s) "
        f"into {result.object_type} ({result.table})"
    )
    lines.append(f"      from  {result.source}")
    lines.append(f"      load  {result.load_id or '(none — refused before it was identified)'}")
    if result.mode == "replace":
        # Said in words, above the prompt, because it is the one mode whose whole effect is on rows
        # nobody named and no other command in Loom destroys data it never read.
        lines.append(
            "\n  ! replace empties this table first — every row not in the batch is gone,"
            "\n    including rows this ontology does not describe."
        )
    if result.rows_rejected:
        lines.append(f"\n  {result.rows_rejected} row(s) would be rejected rather than loaded.")
    if result.read_snapshot_id is not None:
        lines.append(f"\n  previewed at snapshot {result.read_snapshot_id} — nothing is held:")
        lines.append("  the load reads again and asserts that read, so a table that moves while you")
        lines.append("  decide is a conflict you are told about, never a silent overwrite.")
    elif result.ok:
        lines.append(
            "\n  an append asserts no snapshot — it reads nothing and puts no row over another."
        )
    for failure in result.failures:
        # `!` is the refusal mark everywhere else in this file, and a quarantined row is not a
        # refusal — the load is about to apply. Marked `·` for the same reason `rollback` marks the
        # columns it is leaving live: something happened to them, and it is not the run stopping.
        quarantined = _quarantined(result, failure)
        lines.append(f"\n  {'·' if quarantined else '!'} {failure.code}: {failure.message}")
        hint = failure.detail.get("hint")
        if hint:
            lines.append(f"    hint: {hint}")
    if any(not _quarantined(result, f) for f in result.failures):
        lines.append("  nothing was written.")
    return "\n".join(lines)


def _quarantined(result, failure) -> bool:
    """Whether this failure is a row `--reject-to` set aside rather than a reason the load stopped.

    Both halves are needed. The code has to be one `--reject-to` may absorb, and the load has to
    have actually gone ahead: the identical `type_error` refuses the whole batch when no
    `--reject-to` was passed, and describing *that* as a quarantined row would be the same lie in
    the other direction. `IngestResult.rows_rejected` counts only rows that were really written
    somewhere, which is what makes it the honest test."""
    from .ingest import QUARANTINABLE

    return result.ok and bool(result.rows_rejected) and failure.code in QUARANTINABLE


def cmd_sequence(args) -> int:
    """Run one declared sequence of loads, in order, and record that they were one run.

    It takes a **sequence name and a manifest**, mirroring `loom ingest`'s entry-and-file exactly:
    the declared thing, and the file that varies per run. A sequence needs several data files, so
    the file it takes is the one that names them — which keeps the principle rather than bending it.

    **There is no `--from` or `--only`.** Resuming a partial run and loading a subset are both
    `loom ingest` per entry, which already exists and already refuses a file it has seen. A flag
    here would be a second, less careful way to run part of a sequence, and the reported
    `stoppedAt` is the whole of what an operator needs to pick up by hand."""
    diag = Diagnostics()
    try:
        ontology, config = _load_project(args.path, diag)
    except SpecErrors as e:
        print(str(e), file=sys.stderr)
        return 1

    from .catalog import LOAD_LOG_TABLE, SEQUENCE_LOG_TABLE, CatalogError, open_catalogs
    from .governance import PolicyError
    from .ingest import LOG_FAILED, IngestError, SequenceError, build_sequences
    from .mcp.registry import json_safe
    from .migrate.meta import default_actor

    try:
        runtime = build_sequences(ontology, config, open_catalogs(config))
        preview = runtime.run(args.sequence, args.manifest, dry_run=True)
    except (IngestError, SequenceError, PolicyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except CatalogError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(_render_sequence(preview, str(args.path)), file=sys.stderr)
    if args.dry_run:
        print(json.dumps(json_safe(preview.as_json()), indent=2, default=str))
        return 0 if preview.ok else 1

    # Unlike `cmd_ingest`, a refused preview does not run the **order** for real: what that would add
    # is a sequence row for a run whose first load is already known to refuse — a record of an order
    # nobody attempted, which is the intention-shaped record `_record` writes after the fact to avoid.
    #
    # The load that stopped it is a different question, and this used to get it wrong. The claim here
    # was that the individual loads record their own refusals — but the rehearsal above is a dry run,
    # and a dry run records nothing, so the refusal was recorded nowhere at all. `record_stop` runs
    # that one entry for real, which is `cmd_ingest`'s own move and makes the claim true rather than
    # merely stated: the same refusal now leaves the same row whichever command met it.
    if not preview.ok:
        recorded = runtime.record_stop(preview, actor=default_actor())
        print("nothing was loaded — the sequence would stop before the end.", file=sys.stderr)
        if recorded is not None and recorded.load_id and not any(
            f.code == LOG_FAILED for f in recorded.failures
        ):
            print(
                f"note: the load that stopped it is recorded in {LOAD_LOG_TABLE} as "
                f"{recorded.load_id}.",
                file=sys.stderr,
            )
        return 1
    if not _confirmed(args.yes, "run"):
        print("aborted — nothing was written", file=sys.stderr)
        return 1

    try:
        result = runtime.run(args.sequence, args.manifest, actor=default_actor())
    except (IngestError, SequenceError) as e:  # pragma: no cover - the preview met these already
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(json.dumps(json_safe(result.as_json()), indent=2, default=str))
    for step in result.steps:
        if step.result is None:
            print(f"error: {step.entry}: the load could not be attempted", file=sys.stderr)
            continue
        for failure in step.result.failures:
            print(f"error: {step.entry}: {failure.code}: {failure.message}", file=sys.stderr)
    if result.recorded:
        print(
            f"note: recorded in {SEQUENCE_LOG_TABLE} as {result.sequence_id}.", file=sys.stderr
        )
    elif result.sequence_id:
        # The loads recorded themselves either way; what is missing is only the grouping.
        print(
            "note: the sequence log did not record this run — each load is still in "
            "_loom_meta.loads under its own id.",
            file=sys.stderr,
        )
    return 0 if result.ok else 1


def _render_sequence(result, title: str) -> str:
    """The order, before the prompt, with what each step would do to its table.

    `render_apply`'s shape rather than `_render_load`'s, because the thing being previewed is a list
    that stops somewhere — and the sentence at the bottom is the one `apply` already had to write:
    this is an order, not a transaction."""
    marks = {"append": "+", "merge": "~", "replace": "-"}
    lines = [f"Loom sequence — {result.sequence} on {title}", ""]
    for step in result.steps:
        load = step.result
        if load is None:
            lines.append(f"  ! {step.entry} — the load could not be attempted")
            continue
        lines.append(
            f"  {marks.get(load.mode, '?')} {step.entry}: {load.mode} {load.rows_read} row(s) "
            f"into {load.object_type} ({load.table})"
        )
        lines.append(f"      from  {step.source}")
        for failure in load.failures:
            lines.append(f"      ! {failure.code}: {failure.message}")
    if result.stopped_at is not None:
        lines.append(f"\n  ! stops at '{result.stopped_at}'.")
        if result.landed:
            lines.append(
                f"    {len(result.landed)} load(s) before it would have landed and would stay "
                f"landed:"
            )
            lines.append(f"    {', '.join(s.entry for s in result.landed)}")
    lines.append(
        "\n  Iceberg's unit is the table, so there is no cross-table transaction to be had:"
        "\n  this sequences the loads, stops at the first refusal, and reports exactly which"
        "\n  ones landed rather than pretending the run was atomic."
    )
    return "\n".join(lines)


def cmd_embed(args) -> int:
    """Bring every declared sidecar level with the text it describes.

    It takes an **ontology path and, optionally, one object type**, and nothing that describes the
    embedding: not a provider, not a model, not a dimension. Those live in `loom.yaml` for
    `cmd_ingest`'s reason — a flag that could contradict the reviewed file would let one run write
    vectors the served surface cannot rank, and the model is folded into every stored hash, so the
    contradiction would be silent until a `match_` returned nothing.

    `--remodel` is the one operator decision here, and it is a decision only a person can make: every
    vector was produced by a model this deployment no longer configures, and re-deriving them is
    expensive but correct. Unlike `loom apply`, which refuses a breaking plan with no flag at all —
    there no safe version exists, and here it is merely expensive and reversible.

    `--dry-run` previews. Every real reconcile previews first, for `cmd_run`'s reason: what the
    prompt shows should be what is about to happen rather than a second guess at it. The preview
    calls the model exactly once, to ask how wide it is — see `EmbedRuntime.reconcile`."""
    diag = Diagnostics()
    try:
        ontology, config = _load_project(args.path, diag)
    except SpecErrors as e:
        print(str(e), file=sys.stderr)
        return 1

    from .catalog import CatalogError, open_catalogs
    from .embed import EmbedError, build_embedder
    from .governance import PolicyError
    from .mcp.registry import json_safe
    from .negotiate import CapabilityError

    try:
        runtime = build_embedder(ontology, config, open_catalogs(config))
        preview = runtime.reconcile(args.object_type, dry_run=True, remodel=args.remodel)
    except (EmbedError, CapabilityError, PolicyError, CatalogError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # Not printed when the run never got as far as reading a type, which is what an unreachable
    # model looks like. A header over an empty list, above the error that explains it, is one more
    # line between an operator and the sentence they need.
    if preview.types:
        print(_render_embed(preview, str(args.path)), file=sys.stderr)
    if args.dry_run or not preview.ok:
        print(json.dumps(json_safe(preview.as_json()), indent=2, default=str))
        for failure in preview.failures:
            print(f"error: {failure.code}: {failure.message}", file=sys.stderr)
        return 0 if preview.ok else 1

    # Nothing to do is not a question worth asking about. A reconcile with no pending rows and no
    # orphans writes nothing, and prompting for it would train an operator to confirm without reading.
    changing = preview.rows_embedded or preview.rows_pruned
    if changing and not _confirmed(args.yes, "embed"):
        print("aborted — nothing was written", file=sys.stderr)
        return 1

    try:
        result = runtime.reconcile(args.object_type, remodel=args.remodel)
    except CatalogError as e:  # pragma: no cover - the runtime folds these into WRITE_FAILED
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(json.dumps(json_safe(result.as_json()), indent=2, default=str))
    for failure in result.failures:
        print(f"error: {failure.code}: {failure.message}", file=sys.stderr)
    if result.rows_pruned:
        print(
            f"note: {result.rows_pruned} vector(s) were pruned — that is the only path by which "
            f"text Loom derived from a deleted row stops being recoverable, and its lag is the "
            f"interval between runs of this command.",
            file=sys.stderr,
        )
    print(
        f"{result.status} · {result.rows_embedded} embedded, {result.rows_pruned} pruned "
        f"via {result.model}/{result.dims}d",
        file=sys.stderr,
    )
    return 0 if result.ok else 1


def _render_embed(result, title: str) -> str:
    """What is about to happen, before the prompt.

    Shaped like `_render_load` and `render_plan`, and the symbols mean what they mean there: `~`
    changes in place — a vector being recomputed is the same row saying something new — and `-` goes
    away. There is no `+`: a first embed and a refresh are the same write, and spelling them
    differently would imply the sidecar distinguishes them. It does not."""
    lines = [f"loom embed — {title} · {result.model or '(no model)'}"]
    for t in result.types:
        marks = []
        if t.rows_embedded:
            marks.append(f"~ {t.rows_embedded} to embed")
        if t.rows_pruned:
            marks.append(f"- {t.rows_pruned} to prune")
        if not marks:
            marks.append("current")
        detail = f"    {t.object_type} · {', '.join(marks)} · {t.rows_current} current"
        # Stated per type rather than summed, because it is the count that is *not* work outstanding
        # and a total would invite reading it as a backlog. See `TypeReconcile`.
        if t.rows_without_text:
            detail += f", {t.rows_without_text} with no text"
        if t.rows_unkeyed:
            detail += f", {t.rows_unkeyed} with no key"
        # The *oldest* stamp, which is the only one an operator can act on: it says every vector
        # here is at least this current. Absent for a sidecar that has never held a row.
        if t.embedded_as_of:
            detail += f" · embedded as of {_zulu(t.embedded_as_of)}"
        lines.append(detail)
    return "\n".join(lines)


def cmd_serve(args) -> int:
    import asyncio

    diag = Diagnostics()
    try:
        ontology, config = _load_project(args.path, diag)
    except SpecErrors as e:
        print(str(e), file=sys.stderr)
        return 1

    from .catalog import CatalogError
    from .governance import PolicyError
    from .mcp.server import build_server, serve_http, serve_stdio
    from .negotiate import CapabilityError

    try:
        server, resolver = build_server(ontology, config)
    except (CatalogError, CapabilityError, PolicyError) as e:
        # Better to refuse to start than to advertise tools that will fail on every call. A
        # capability mismatch is the second half of that sentence: an engine without OFFSET fails
        # not on every call but on the second page, which is the worse shape — it works until it
        # doesn't, and by then a client has the tool list.
        print(f"error: {e}", file=sys.stderr)
        return 1

    # Every human-facing line goes to stderr. That used to be because stdout was the transport,
    # which stops being true the moment a second transport has an address instead of a pipe. The
    # rule stays and the reason is replaced: the banner is *diagnostics*, and diagnostics go to
    # stderr whatever the transport. Splitting it — stdout unless stdio — would give one command two
    # output shapes to keep in step, and would have to be revisited by every transport after this
    # one. Whatever collects these lines should not need to know how the tools are being served.
    print(
        f"loom serve — {ontology.summary()} → {len(server.tools)} tool(s) over {config.mcp.transport}",
        file=sys.stderr,
    )
    for name in sorted(server.tools):
        print(f"  {name}", file=sys.stderr)
    # Said every time, in both modes. "How many tools" does not answer "can this thing write to my
    # lake", and that is the question somebody pointing a client at a production catalog is
    # actually asking. The counts above are what was *built*, so the lines below are what explain
    # the gap between them and what the spec declares — and, over HTTP, who can reach them.
    for line in (
        _write_mode(config, ontology)
        + _semantic_mode(config, ontology)
        + _governance_mode(resolver)
        + _transport_mode(config)
    ):
        print(f"  {line}", file=sys.stderr)
    if config.mcp.transport == "http":
        asyncio.run(serve_http(server, config.mcp))
    else:
        asyncio.run(serve_stdio(server))
    return 0


def _write_mode(config, ontology) -> list[str]:
    """The sentences the banner needs about writes, the actor they will be recorded under, and
    whether this deployment would rather refuse than write unrecorded.

    The edit-log line is printed whatever `mcp.writes` says, because `governance.edit_log` binds the
    runtime and not the surface — `loom run` against this config meets it too. It is worded as the
    startup fact it is: by the time this banner prints, every catalog an action writes to has been
    proved able to hold a log."""
    from .governance import EDIT_LOG_REQUIRED

    actions = len(ontology.actions)
    if not config.mcp.writes:
        if not actions:
            return ["read-only · the spec declares no action"]
        lines = [
            f"read-only · mcp.writes is false, so {actions} declared action(s) are not exposed",
            "  (`loom run` still reaches them — the runtime is not what is switched off, the surface is)",
        ]
    else:
        who = (
            f"recorded as actor '{config.mcp.actor}'"
            if config.mcp.actor
            else "recorded as actor 'unknown' — set mcp.actor to say who this deployment writes as"
        )
        lines = [f"writes enabled · {actions} action(s) exposed, every run {who}"]
    if actions and config.edit_log == EDIT_LOG_REQUIRED:
        lines.append(
            "edit log required · every catalog an action writes to holds one, or this server does "
            "not start"
        )
        lines.append(
            "  (checked before a write, never promised after one — an append that fails once the "
            "row has committed still reports 'log_failed')"
        )
    return lines


def _semantic_mode(config, ontology) -> list[str]:
    """Which types declare a semantic property, and where this deployment's vectors come from.

    Same job as `_write_mode`'s: explain the gap between what the spec declares and what got built.
    A spec may declare `semantic:` on three types and a deployment may configure no provider, and
    the number of tools alone cannot tell those apart from a spec that declares none — which is
    exactly the reading `mcp.writes` needed a line for.

    It names the provider *and* the model because the model is folded into every stored vector's
    hash: two servers on one spec with different models do not share a warehouse's vectors, and
    that is a fact an operator can only get from here.

    Silent when the spec declares nothing, like `_governance_mode` and unlike `_write_mode` — a
    deployment with no semantic property is not being asked "is this switched on", so a line saying
    no would be one more thing to read on every start."""
    declared = tuple(
        f"{obj.api_name}.{obj.semantic}"
        for obj in ontology.object_types.values()
        if obj.semantic is not None
    )
    if not declared:
        return []
    if config.mcp.embedding is None:
        return [
            f"semantic search off · {len(declared)} declared ({', '.join(sorted(declared))}), and "
            "this deployment configures no mcp.embedding provider",
        ]
    return [
        f"semantic search · {', '.join(sorted(declared))} via {config.mcp.embedding.describe()}",
        # The cost, beside the transport line's note that a slow call blocks the server, because
        # this is the call most likely to be the slow one. There is no vector index and that is a
        # decision rather than an omission: pre-filtering means brute-forcing the survivors anyway,
        # and an HNSW index is an optimisation for the unfiltered case.
        #
        # **Both halves, because they are different sizes.** A `filter` narrows what has to be
        # *measured* and cannot narrow what has to be *read*: the surviving keys are known only
        # after the object side is scanned, and there is no pushdown spelling for a key set. So the
        # sentence an operator can act on is "a filter helps, and the sidecar is still read".
        "  (match_ ranks by brute force · the arithmetic is linear in the filtered set, so a "
        "narrow filter is the lever)",
        "  (no vector index · the whole sidecar is read on every call, filtered or not — that is "
        "the I/O floor, and it grows with the embedded rows rather than with the answer)",
        # Only `loom embed` can see this number, so the banner points at the command rather than
        # guessing it: the ranked surface deliberately does not count unembedded rows per call.
        "  (a row with no vector is absent from match_, silently — `loom embed` is what reports "
        "how many, and how far behind)",
        # The other half of the same sidecar being behind, and the half a caller *can* act on: an
        # edited row is not absent, it is ranked by text it no longer has.
        "  (a row edited since it was embedded is not absent — it comes back marked `stale`, ranked "
        "by the text it had then and carrying the text it has now)",
    ]


def _governance_mode(resolver) -> list[str]:
    """What this deployment withholds, said where somebody starting the server will read it.

    Nothing when nothing is withheld — the banner already answers "can this write to my lake", and a
    line saying no policy is in force would be one more thing to read on every start. When there is
    one, it names the properties rather than the count: `2 policies` tells an operator that
    *something* is governed, which is the half of the question they can already see in the config.

    It says *deployment* out loud, because that is the part a reader is most likely to assume
    otherwise. Every caller of this server gets these same masks — a mask cannot carry `when:`, so
    that half is deployment-wide by construction and stays true whatever else this deployment does.

    **What varies per caller is named, and it is named because the rest of the line says it does
    not.** A `when:` guard or a `principal.` reference inside a predicate makes a policy's *rows*
    depend on who is asking, and an operator reading "filter the rows of Order" has to know whether
    they are looking at one answer or a family of them. The claims themselves are not printed: this
    line describes a deployment, and there is no caller yet to describe.

    **Row filters are named here and nowhere a caller can see**, which is the one place the two
    halves of a policy are treated differently on purpose. A mask announces itself in every tool
    description because the property names are already in the spec; a row predicate announces itself
    only to whoever started the process, because to anybody else "these rows are filtered" is a
    statement about data they were not shown. This banner goes to stderr, once, for the operator
    holding the `loom.yaml` it is describing."""
    withheld = [
        f"{name}: {', '.join(resolver.masked(name))}"
        for name in resolver.ontology.object_types
        if resolver.masked(name)
    ]
    filtered = [
        f"{name} (by {', '.join(resolver.policies.filtered_by(name))})"
        for name in resolver.ontology.object_types
        if resolver.policies.filtered_by(name)
    ]
    if not withheld and not filtered:
        return []
    what = []
    if withheld:
        what.append(f"withhold {'; '.join(withheld)}")
    if filtered:
        what.append(f"filter the rows of {'; '.join(filtered)}")
    conditional = [p.name for p in resolver.policies.policies if p.conditional]
    scope = (
        "  (deployment-wide — every caller of this server is filtered the same way, and `loom query` "
        "against this config is filtered identically)"
        if not conditional
        else f"  (masks are deployment-wide; {', '.join(conditional)} filter rows per attested "
        "caller, so `loom query` against this config refuses rather than reading unfiltered)"
    )
    return [
        f"governed · {len(resolver.policies.policies)} policy/policies {' and '.join(what)}",
        scope,
    ]


def _transport_mode(config) -> list[str]:
    """What a socket changes, said where somebody starting the server will read it.

    Nothing for stdio, which has no address and one client. For HTTP, the two facts a person cannot
    infer from "10 tools": where it is, and that it answers one call at a time. The second is a
    scaling claim, and a server that makes it silently is a support ticket."""
    mcp = config.mcp
    if mcp.transport != "http":
        return []
    lines = [f"listening on {mcp.address()} · cleartext HTTP, no TLS — terminate it in front"]
    lines.append(
        "one call at a time · tool calls are serialized, so a slow query blocks the server rather "
        "than queueing beside another"
    )
    if not mcp.is_loopback:
        lines.append(
            f"bound to {mcp.host} · reachable by whoever can reach the port, not only by whoever "
            "can run this binary"
        )
    # Said in both directions, because both are things an operator would otherwise have to infer
    # from the absence of a line. "Who is recorded for a write" is exactly the question the write
    # banner above leaves half-answered: `actor` names the deployment either way, and whether a
    # *caller* is named beside it is the difference this line reports.
    if mcp.auth is not None:
        lines.append(
            f"callers attested · bearer tokens verified against {mcp.auth.issuer} for audience "
            f"'{mcp.auth.audience}' · unauthenticated requests are refused, and every run records "
            "the caller beside the actor"
        )
    else:
        lines.append(
            "no caller attested · every request is served unauthenticated, and runs are recorded "
            "under the deployment's actor alone"
        )
    return lines


def cmd_plan(args) -> int:
    """Dry-run the migration engine: what would `apply` have to do to make the catalog match?

    Note what this deliberately is *not*: `validate --physical`. That pass treats a missing table
    or column as an error, which is exactly what a plan has to be able to report as a creation
    instead. Structural validation still runs first — diffing against a spec that doesn't parse
    produces noise, not a plan."""
    diag = Diagnostics()
    try:
        ontology, config = _load_project(args.path, diag)
    except SpecErrors as e:
        print(str(e), file=sys.stderr)
        return 1

    from .catalog import CatalogError, open_catalogs
    from .migrate import diff_ontology, render_plan

    try:
        plan = diff_ontology(ontology, open_catalogs(config), diag)
        # A plan built on a binding we couldn't resolve would be missing tables without saying so.
        diag.raise_if_errors()
    except SpecErrors as e:
        print(str(e), file=sys.stderr)
        return 1
    except CatalogError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    for w in diag.warnings:
        print(f"warning: {w.render()}", file=sys.stderr)
    print(render_plan(plan, title=str(args.path)))
    return 0


def cmd_apply(args) -> int:
    """Execute the plan. Prints it first, unchanged, because that is what is about to happen.

    Re-planning here rather than reading a plan file is deliberate: a plan is only true of the
    catalog it was taken against, and one saved half an hour ago describes a lake that may have
    moved. The diff is cheap; a stale apply is not."""
    diag = Diagnostics()
    try:
        ontology, config = _load_project(args.path, diag)
    except SpecErrors as e:
        print(str(e), file=sys.stderr)
        return 1

    from .catalog import CatalogError, open_catalogs
    from .migrate import Severity, apply_plan, diff_ontology, render_apply, render_plan, snapshot_spec

    try:
        catalogs = open_catalogs(config)
        plan = diff_ontology(ontology, catalogs, diag)
        diag.raise_if_errors()
    except SpecErrors as e:
        print(str(e), file=sys.stderr)
        return 1
    except CatalogError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    for w in diag.warnings:
        print(f"warning: {w.render()}", file=sys.stderr)
    print(render_plan(plan, title=str(args.path), executing=True))

    # A breaking plan needs no confirmation — the executor refuses it, and asking first would
    # imply an answer that would change the outcome.
    if not plan.is_empty and plan.severity is not Severity.BREAKING and not _confirmed(args.yes):
        print("aborted — nothing was applied", file=sys.stderr)
        return 1

    print()
    try:
        result = apply_plan(plan, catalogs, snapshot_spec(args.path))
    except CatalogError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(render_apply(result))
    return 0 if result.ok else 1


def cmd_rollback(args) -> int:
    """Restore the spec `_loom_meta` recorded, and bring the physical schema back in line with it.

    Note what it deliberately does *not* load: the spec on disk. Only `loom.yaml`, for the
    catalogs. The spec you are rolling back *from* is quite often the one that no longer parses,
    and needing it would make rollback unavailable exactly when it is wanted.

    The working tree is written last, and only if the run was not refused. Everything before that
    is planned against a copy of the recorded spec in a temporary directory, so a rollback you
    decline — or one the executor refuses — leaves the lake and the files exactly as they were.
    """
    diag = Diagnostics()
    config_path = find_config(args.path)
    if config_path is None:
        print(str(SpecErrors([_missing_config(args.path)])), file=sys.stderr)
        return 1

    from .catalog import CatalogError, open_catalogs
    from .migrate import (
        REFUSED,
        MetaStore,
        RollbackError,
        Severity,
        apply_plan,
        desired_tables,
        diff_ontology,
        file_changes,
        latest_version,
        left_behind,
        materialize,
        render_apply,
        render_rollback,
        resolve_target,
        restore_files,
    )

    try:
        config = load_config(config_path, diag)
        diag.raise_if_errors()
        catalogs = open_catalogs(config)
        history = {name: MetaStore(catalog).history() for name, catalog in catalogs.items()}
        target = resolve_target(history, args.to)
        # The spec the lake is at now, read from history rather than from disk for the same reason
        # as everything else here: `left_behind` compares the two recorded specs, and the one on
        # disk may be the reason someone is rolling back.
        current = resolve_target(history, latest_version(history))
    except SpecErrors as e:
        print(str(e), file=sys.stderr)
        return 1
    except (CatalogError, RollbackError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="loom-rollback-") as tmp:
        try:
            restored, _ = build(materialize(target.snapshot, Path(tmp) / "restored"))
            recorded, _ = build(materialize(current.snapshot, Path(tmp) / "recorded"))
        except SpecErrors as e:
            print(f"error: a recorded spec cannot be loaded by this version of Loom\n{e}", file=sys.stderr)
            return 1

        try:
            plan = diff_ontology(restored, catalogs, diag, renames=target.renames)
            diag.raise_if_errors()
        except SpecErrors as e:
            print(str(e), file=sys.stderr)
            return 1
        except CatalogError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

        # `diag` is clean by here, so a second pass over the same two specs adds no diagnostics —
        # it is only being asked which columns each one maps.
        left = left_behind(
            plan, desired_tables(recorded, diag), desired_tables(restored, diag), catalogs
        )
        changes = file_changes(Path(args.path), target.snapshot)

    for w in diag.warnings:
        print(f"warning: {w.render()}", file=sys.stderr)
    print(render_rollback(target, plan, left, changes, title=str(args.path)))

    if plan.severity is not Severity.BREAKING and not _confirmed(args.yes, "roll back"):
        print("aborted — nothing was rolled back", file=sys.stderr)
        return 1

    print()
    try:
        result = apply_plan(plan, catalogs, target.snapshot, rollback_of=target.version)
    except CatalogError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(render_apply(result))
    if result.touched_nothing:
        # Two ways in: the plan was refused, or its first table failed. The lake is identical
        # either way, so the working tree has to be too — a restored spec beside a lake that never
        # moved is a checkout describing something that does not exist, and the next `loom plan`
        # would read it as a migration to perform rather than one that failed.
        why = (
            "no spec file was written either"
            if result.status == REFUSED
            else "no table changed, so no spec file was written either"
        )
        print(f"nothing was rolled back — {why}", file=sys.stderr)
        return 1

    restore_files(Path(args.path), target.snapshot, changes)
    if changes.any:
        deleted = f", deleted {len(changes.deleted)}" if changes.deleted else ""
        print(f"Restored {len(changes.written)} spec file(s){deleted} in {args.path}.")
    return 0 if result.ok else 1


def _confirmed(assume_yes: bool, action: str = "apply") -> bool:
    """Ask before writing to someone's lake.

    Refusing when there's no terminal — rather than assuming yes — is the important half: `apply`
    inside a pipeline should be a deliberate `--yes`, not a side effect of nobody being there to
    object."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(
            f"error: refusing to {action} without confirmation — no terminal to ask at, pass --yes",
            file=sys.stderr,
        )
        return False
    try:
        answer = input(f"\n{action[0].upper()}{action[1:]} these changes? [y/N] ")
    except EOFError:  # pragma: no cover - a tty that closes mid-question
        return False
    return answer.strip().lower() in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    from .migrate.meta import loom_version

    # One narrative, two streams, and the order has to survive `2>&1`. Every command here writes
    # its report to stdout and its errors and notes to stderr; stdout is block-buffered the moment
    # it is redirected, so a refusal written *after* a report would land *above* it in any captured
    # log — which is how a refused `loom rollback` came to be read as "restored, then refused".
    # Line buffering costs nothing at this scale and makes the two streams interleave as written.
    # Suppressed rather than guarded: a captured stdout under pytest is not always reconfigurable,
    # and ordering is not what those tests are checking.
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(prog="loom", description="Loom ontology framework")
    # The one thing every other surface could already answer and the CLI could not. `loom_version`
    # is the same function `_loom_meta` stamps into every recorded apply and every recorded load,
    # so `loom --version` and the `loom_version` column in a history row can never disagree about
    # which build wrote what.
    parser.add_argument(
        "--version",
        action="version",
        version=f"loom {loom_version()}",
        help="print the installed loom-ontology version",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    n = sub.add_parser("infer", help="draft an objectType from a data file (writes nothing)")
    n.add_argument("source", help="path to the file to read the schema of")
    # Required, and not derived from the filename. `customers.parquet` -> `Customer` needs a
    # singulariser, which is a guess about English rather than about data, and it would be the one
    # guess this command makes that no comment in the output could justify.
    n.add_argument("--as", dest="api_name", required=True, metavar="NAME", help="apiName for the drafted objectType")
    n.add_argument(
        "--format",
        default="parquet",
        choices=list(INFER_FORMATS) + sorted(UNREADABLE_FORMATS),
        help="source format; only parquet is readable — the others are listed so the refusal names them",
    )
    n.add_argument("--catalog", default=None, metavar="NAME", help="backing catalog, as named in loom.yaml")
    n.add_argument("--table", default=None, metavar="NS.TABLE", help="backing table for this type")
    n.add_argument(
        "--key",
        default=None,
        metavar="COLUMN",
        help="source column to use as the primary key — named as a column, since the property does not exist yet",
    )
    n.add_argument("--entry", default=None, metavar="NAME", help="name for the drafted ingest entry")
    n.set_defaults(func=cmd_infer)

    v = sub.add_parser("validate", help="load and validate an ontology directory")
    v.add_argument("path", nargs="?", default="ontology", help="path to the ontology dir")
    v.add_argument(
        "--physical",
        action="store_true",
        help="also check backing tables/columns against the live catalogs in loom.yaml",
    )
    v.set_defaults(func=cmd_validate)

    q = sub.add_parser("query", help="run one ontology read (dev tool)")
    q.add_argument("object_type", help="objectType apiName, e.g. Customer")
    q.add_argument("path", nargs="?", default="ontology", help="path to the ontology dir")
    q.add_argument("--key", help="primary key — fetch one object")
    q.add_argument("--link", help="with --key, follow this link instead")
    q.add_argument(
        "--match",
        metavar="TEXT",
        help=(
            "rank by meaning against the objectType's 'semantic:' property, nearest first; "
            "--filter narrows before the ranking"
        ),
    )
    q.add_argument(
        "--filter",
        action="append",
        metavar="PROP[.OP]=VALUE",
        help=(
            "repeatable search filter, ANDed — e.g. tier=gold, salesDate.gte=2026-01-01; "
            "repeat PROP.in=VALUE to build a membership list"
        ),
    )
    q.add_argument(
        "--via",
        action="append",
        metavar="LINK[.PROP[.OP]=VALUE]",
        help=(
            "with --match, narrow by a linked object — e.g. placedBy.tier=gold keeps only rows "
            "with such a linked object; a bare LINK keeps the rows that have one at all"
        ),
    )
    q.add_argument("--limit", type=int, default=None)
    # The other half of a page. Without it this command could not reach row 501 of anything, while
    # the refusal it printed above 500 — "Ask for that many and page with 'offset'" — named a flag
    # that did not exist. Both halves come from `Resolver`, so the cap, the refusals and the
    # defaults are the generated tools' and not a second set with the same numbers.
    q.add_argument(
        "--offset", type=int, default=0, help="rows to skip, for paging past --limit"
    )
    q.set_defaults(func=cmd_query)

    r_ = sub.add_parser("run", help="run one declared action (dev tool)")
    r_.add_argument("action", help="action apiName, e.g. upgradeTier")
    r_.add_argument("path", nargs="?", default="ontology", help="path to the ontology dir")
    r_.add_argument("--param", action="append", metavar="NAME=VALUE", help="repeatable action parameter")
    r_.add_argument("--dry-run", action="store_true", help="bind and validate, but write nothing")
    r_.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    r_.set_defaults(func=cmd_run)

    i = sub.add_parser("ingest", help="run one declared bulk load from a file")
    i.add_argument("entry", help="the ingest entry declared in loom.yaml, e.g. orders-nightly")
    i.add_argument("source", help="path to the file to load, in the format the entry declares")
    i.add_argument("path", nargs="?", default="ontology", help="path to the ontology dir")
    i.add_argument("--dry-run", action="store_true", help="check the batch, but write nothing")
    i.add_argument(
        "--load-id",
        default=None,
        metavar="ID",
        help="override the id derived from the entry, the mode and the file — how you say "
        "'this file again, on purpose'",
    )
    i.add_argument(
        "--reject-to",
        default=None,
        metavar="PATH",
        help="write rows that fail their own checks here as NDJSON and load the rest, instead of "
        "refusing the whole batch",
    )
    i.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    i.set_defaults(func=cmd_ingest)

    sq = sub.add_parser("sequence", help="run a declared order of bulk loads from a manifest")
    sq.add_argument("sequence", help="the sequence declared in loom.yaml, e.g. nightly")
    sq.add_argument("manifest", help="path to a YAML mapping of entry name to file path")
    sq.add_argument("path", nargs="?", default="ontology", help="path to the ontology dir")
    sq.add_argument("--dry-run", action="store_true", help="check every load, but write nothing")
    sq.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    sq.set_defaults(func=cmd_sequence)

    e = sub.add_parser("embed", help="bring the vector sidecars level with the text they describe")
    e.add_argument("path", nargs="?", default="ontology", help="path to the ontology dir")
    # A flag rather than a leading positional, which is where every other command puts its subject.
    # The difference is that this command *has* no required subject: it reconciles every type that
    # declares `semantic:`, so a bare `loom embed ontology` is the ordinary call — and two optional
    # positionals would make that one ambiguous with `loom embed Customer`, resolvable only by
    # guessing which of them names a directory. `--type` narrows the run; it does not identify it.
    e.add_argument(
        "--type",
        dest="object_type",
        default=None,
        metavar="NAME",
        help="reconcile one objectType declaring 'semantic:' (default: every one that does)",
    )
    e.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    e.add_argument(
        "--remodel",
        action="store_true",
        help="re-embed vectors produced by a different model — how you say 'yes, the model "
        "changed, do it anyway'",
    )
    e.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    e.set_defaults(func=cmd_embed)

    s = sub.add_parser("serve", help="serve the ontology as MCP tools over stdio")
    s.add_argument("path", nargs="?", default="ontology", help="path to the ontology dir")
    s.set_defaults(func=cmd_serve)

    p = sub.add_parser("plan", help="dry-run the migration the ontology implies")
    p.add_argument("path", nargs="?", default="ontology", help="path to the ontology dir")
    p.set_defaults(func=cmd_plan)

    a = sub.add_parser("apply", help="execute the migration the ontology implies")
    a.add_argument("path", nargs="?", default="ontology", help="path to the ontology dir")
    a.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    a.set_defaults(func=cmd_apply)

    r = sub.add_parser("rollback", help="restore a spec `_loom_meta` recorded and re-apply it")
    r.add_argument("path", nargs="?", default="ontology", help="path to the ontology dir")
    r.add_argument(
        "--to",
        type=int,
        default=None,
        metavar="VERSION",
        help="the recorded version to restore (default: the one before the current)",
    )
    r.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    r.set_defaults(func=cmd_rollback)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
