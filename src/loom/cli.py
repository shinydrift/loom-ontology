"""`loom` CLI. v0 ships `validate`; `plan`, `apply`, and `serve` are stubbed as the
migration, catalog, and MCP modules land."""

from __future__ import annotations

import argparse
import sys

from .errors import SpecErrors
from .ontology import build


def cmd_validate(args) -> int:
    try:
        ontology, diag = build(args.path)
    except SpecErrors as e:
        print(str(e), file=sys.stderr)
        return 1
    for w in diag.warnings:
        print(f"warning: {w.render()}", file=sys.stderr)
    print(f"ok — {ontology.summary()}")
    if diag.warnings:
        print(f"({len(diag.warnings)} warning(s))")
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
    v.set_defaults(func=cmd_validate)

    for name in ("plan", "apply", "serve"):
        p = sub.add_parser(name, help=f"{name} (post-v0 stub)")
        p.add_argument("path", nargs="?", default="ontology")
        p.set_defaults(func=_stub(name))

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
