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
    from .mcp.registry import json_safe
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
    except (ResolverError, CatalogError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(json.dumps(json_safe(rows), indent=2, default=str))
    print(f"({len(rows)} row(s))", file=sys.stderr)
    return 0


def cmd_run(args) -> int:
    """Run one declared action. The write path's `loom query`, and under the same rule.

    `loom query` mirrors the generated read tools deliberately — if the dev command can do
    something the tools can't, the ontology has a back door. That test is stronger here, because
    this one writes: so it takes an action apiName and named parameters, exactly the shape M4's
    `run_<action>` tool will take, and calls the same `ActionRuntime.run`. It cannot name a table,
    a column, or a predicate, because the runtime has no argument for one."""
    diag = Diagnostics()
    try:
        ontology, config = _load_project(args.path, diag)
    except SpecErrors as e:
        print(str(e), file=sys.stderr)
        return 1

    from .action import ActionError, ActionRuntime
    from .catalog import CatalogError, open_catalogs
    from .mcp.registry import json_safe

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
        runtime = ActionRuntime(ontology=ontology, catalogs=open_catalogs(config))
        # Always previewed first, even for a real run: it is the same four steps minus the write,
        # so what the prompt shows is what is about to happen rather than a second guess at it. It
        # is a preview and not a recording — an effect holding `now()` gets a fresh value on the
        # run, for the same reason `loom apply` re-plans instead of replaying a saved plan.
        preview = runtime.run(args.action, parameters, dry_run=True)
    except ActionError as e:
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
        result = runtime.run(args.action, parameters)
    except CatalogError as e:  # pragma: no cover - the runtime folds these into WRITE_FAILED
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(json_safe(result.as_json()), indent=2, default=str))
    for failure in result.failures:
        print(f"error: {failure.code}: {failure.message}", file=sys.stderr)
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
        # Said out loud, because a snapshot id printed on its own would read as something checked.
        lines.append(f"\n  read at snapshot {result.read_snapshot_id} — recorded, not yet enforced:")
        lines.append("  the write is one Iceberg transaction; the read before it is not.")
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
    from .mcp.server import build_server, serve_stdio

    try:
        server, _ = build_server(ontology, config)
    except CatalogError as e:
        # Better to refuse to start than to advertise tools that will fail on every call.
        print(f"error: {e}", file=sys.stderr)
        return 1

    # stdout is the transport, so every human-facing line goes to stderr.
    print(
        f"loom serve — {ontology.summary()} → {len(server.tools)} tool(s) over {config.mcp.transport}",
        file=sys.stderr,
    )
    for name in sorted(server.tools):
        print(f"  {name}", file=sys.stderr)
    asyncio.run(serve_stdio(server))
    return 0


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
