"""Shape-checking helpers shared by the YAML-backed grammars (ontology spec + project config).

Small on purpose. These exist so every grammar Loom parses reports problems the same way —
same wording, same "did you mean" hints, same accumulate-don't-raise contract — instead of each
loader inventing its own phrasing.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable

from .errors import Diagnostics, SourceLoc


def snake_case(api_name: str) -> str:
    """`Customer` -> `customer`, `PurchaseOrder` -> `purchase_order`.

    MCP tool names are identifiers agents type, so they get the conventional spelling rather than
    the api name verbatim.

    **It lives here rather than beside its caller because the validator needs it too.** This
    function is not injective over §0's own identifier grammar — `ABCTest` and `AbcTest` are both
    legal PascalCase and both come out `abc_test` — so a spec can declare two distinct object types
    whose whole generated tool set is the same three names. `_validate_tool_names` is what refuses
    that, and it must be reachable from a command that opens no catalog and imports no MCP SDK.
    `loom.mcp.registry` re-exports it, so the surface that generates the names still spells them
    with the same function that checked them."""
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", api_name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


# §0's identifier grammar. Here rather than in `validator.py` for `snake_case`'s reason, one command
# further along: the validator is not the only place that has to know the shape of a legal name —
# `loom infer` *generates* one from `--as`, and while this pattern lived beside its checker the
# on-ramp emitted `apiName: not a name`, printed "this draft validates as it stands", and exited 0
# on a draft `loom validate` then refused. A grammar with one reader is a grammar the next writer
# does not know about.
OBJECT_NAME = re.compile(r"^[A-Z][A-Za-z0-9]*$")
MEMBER_NAME = re.compile(r"^[a-z][A-Za-z0-9]*$")


def identifier_problem(kind: str, name: str, pattern: re.Pattern[str]) -> str | None:
    """The one sentence §0 gives for a name outside its grammar, or None. One wording, three
    callers: the validator's diagnostic, `loom infer`'s refusal, and any grammar added after."""
    if pattern.match(name):
        return None
    shape = "PascalCase" if pattern is OBJECT_NAME else "camelCase"
    return (
        f"{kind} '{name}' is not a legal identifier — {shape}, matching '{pattern.pattern}' "
        f"(spec-v0 §0). Letters and digits only: no spaces, no underscores, no leading digit"
    )


def suggest(bad: str, options: Iterable[str]) -> str | None:
    match = difflib.get_close_matches(bad, list(options), n=1)
    return f"did you mean '{match[0]}'?" if match else None


def check_keys(raw: dict, allowed: set[str], loc: SourceLoc, diag: Diagnostics, ctx: str) -> None:
    for k in raw:
        if k not in allowed:
            # A YAML key is not necessarily a string: under YAML 1.1 the bare keys `on`, `off`,
            # `yes` and `no` resolve to booleans, so a hand-written grammar can be handed one — and
            # `suggest` would raise on it rather than report it. The same trap is why a governance
            # policy names its subject `objectType:` and not the obvious `on:`.
            diag.error(f"unexpected key '{k}' in {ctx}", loc, suggest(k, allowed) if isinstance(k, str) else None)


def require(raw: dict, key: str, loc: SourceLoc, diag: Diagnostics, ctx: str):
    if key not in raw or raw[key] is None:
        diag.error(f"missing required key '{key}' in {ctx}", loc)
        return None
    return raw[key]
