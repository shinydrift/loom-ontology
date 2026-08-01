"""`loom` CLI.

`validate` is structural and offline by default; `--physical` adds the catalog pass. `query` is a
dev command for exercising the read path by hand, and `serve` exposes those same reads as MCP
tools. `plan` dry-runs the migration engine and `apply` executes exactly what `plan` printed.
"""

from __future__ import annotations

import argparse
import json
import sys

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


def _confirmed(assume_yes: bool) -> bool:
    """Ask before writing to someone's lake.

    Refusing when there's no terminal — rather than assuming yes — is the important half: `apply`
    inside a pipeline should be a deliberate `--yes`, not a side effect of nobody being there to
    object."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(
            "error: refusing to apply without confirmation — no terminal to ask at, pass --yes",
            file=sys.stderr,
        )
        return False
    try:
        answer = input("\nApply these changes? [y/N] ")
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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
