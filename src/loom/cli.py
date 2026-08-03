"""`loom` CLI.

`validate` is structural and offline by default; `--physical` adds the catalog pass. `query` is a
dev command for exercising the read path by hand, and `serve` exposes those same reads as MCP
tools. `plan` dry-runs the migration engine, `apply` executes exactly what `plan` printed, and
`rollback` restores a spec out of `_loom_meta` and re-plans it — the same loop, an older spec.
`run` is `query`'s counterpart on the write path: one declared action, through the same runtime
M4's `run_<action>` tool will call.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from .config import CONFIG_FILENAME, find_config, load_config
from .errors import Diagnostics, SourceLoc, SpecError, SpecErrors
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


def cmd_validate(args) -> int:
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
            ontology, diag = build(args.path)
    except SpecErrors as e:
        print(str(e), file=sys.stderr)
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

    from .catalog import CatalogError
    from .governance import PolicyError
    from .mcp.registry import json_safe
    from .negotiate import CapabilityError
    from .resolver import ResolverError, build_resolver

    # Argument shape first, before anything opens a catalog — a typo'd flag shouldn't need a
    # reachable metastore to be reported.
    if args.link and not args.key:
        print("error: --link requires --key", file=sys.stderr)
        return 1
    filters = {}
    for pair in args.filter or []:
        if "=" not in pair:
            print(f"error: --filter expects PROP=VALUE, got '{pair}'", file=sys.stderr)
            return 1
        name, value = pair.split("=", 1)
        filters[name] = value

    try:
        resolver = build_resolver(ontology, config)
        if args.key and args.link:
            rows = resolver.traverse(args.object_type, args.key, args.link, limit=args.limit)
        elif args.key:
            row = resolver.get(args.object_type, args.key)
            rows = [row] if row else []
        else:
            rows = resolver.search(args.object_type, filters, limit=args.limit)
    except (ResolverError, CatalogError, CapabilityError, PolicyError) as e:
        # A `CapabilityError` reaches here for the same reason `loom query` mirrors the generated
        # tools at all: if the dev command can read out of an engine the served surface refuses to
        # stand on, the ontology has a back door. A `PolicyError` is the same sentence about a
        # deployment instead of an engine.
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
    print(f"({len(rows)} row(s))", file=sys.stderr)
    return 0


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
    lines.append(f"  {result.operation} {result.object_type} {show(result.key)}")
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
    for line in _write_mode(config, ontology) + _governance_mode(resolver) + _transport_mode(config):
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


def _governance_mode(resolver) -> list[str]:
    """What this deployment withholds, said where somebody starting the server will read it.

    Nothing when nothing is withheld — the banner already answers "can this write to my lake", and a
    line saying no policy is in force would be one more thing to read on every start. When there is
    one, it names the properties rather than the count: `2 policies` tells an operator that
    *something* is governed, which is the half of the question they can already see in the config.

    It says *deployment* out loud, because that is the part a reader is most likely to assume
    otherwise. Every caller of this server gets these same masks; there is no per-caller filtering
    here, and a server that let somebody believe there was would be the support ticket.

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
    return [
        f"governed · {len(resolver.policies.policies)} policy/policies {' and '.join(what)}",
        "  (deployment-wide — every caller of this server is filtered the same way, and `loom query` "
        "against this config is filtered identically)",
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
    if result.status == REFUSED:
        print("nothing was rolled back — no spec file was written either", file=sys.stderr)
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
    parser = argparse.ArgumentParser(prog="loom", description="Loom ontology framework")
    sub = parser.add_subparsers(dest="command", required=True)

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
    q.add_argument("--filter", action="append", metavar="PROP=VALUE", help="repeatable search filter")
    q.add_argument("--limit", type=int, default=None)
    q.set_defaults(func=cmd_query)

    r_ = sub.add_parser("run", help="run one declared action (dev tool)")
    r_.add_argument("action", help="action apiName, e.g. upgradeTier")
    r_.add_argument("path", nargs="?", default="ontology", help="path to the ontology dir")
    r_.add_argument("--param", action="append", metavar="NAME=VALUE", help="repeatable action parameter")
    r_.add_argument("--dry-run", action="store_true", help="bind and validate, but write nothing")
    r_.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    r_.set_defaults(func=cmd_run)

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
