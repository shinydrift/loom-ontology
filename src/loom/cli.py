"""`loom` CLI.

`validate` is structural and offline by default; `--physical` adds the catalog pass. `query` is a
dev command for exercising the read path by hand, and `serve` exposes those same reads as MCP
tools. `plan`/`apply` stay stubs until the migration engine lands.
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


def _stub(name: str):
    def run(args) -> int:
        print(f"'{name}' is not implemented yet (post-v0)", file=sys.stderr)
        return 2
    return run


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

    for name in ("plan", "apply"):
        p = sub.add_parser(name, help=f"{name} (post-v0 stub)")
        p.add_argument("path", nargs="?", default="ontology")
        p.set_defaults(func=_stub(name))

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
