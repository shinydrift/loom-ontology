"""Shape-checking helpers shared by the YAML-backed grammars (ontology spec + project config).

Small on purpose. These exist so every grammar Loom parses reports problems the same way —
same wording, same "did you mean" hints, same accumulate-don't-raise contract — instead of each
loader inventing its own phrasing.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable

from .errors import Diagnostics, SourceLoc


def suggest(bad: str, options: Iterable[str]) -> str | None:
    match = difflib.get_close_matches(bad, list(options), n=1)
    return f"did you mean '{match[0]}'?" if match else None


def check_keys(raw: dict, allowed: set[str], loc: SourceLoc, diag: Diagnostics, ctx: str) -> None:
    for k in raw:
        if k not in allowed:
            diag.error(f"unexpected key '{k}' in {ctx}", loc, suggest(k, allowed))


def require(raw: dict, key: str, loc: SourceLoc, diag: Diagnostics, ctx: str):
    if key not in raw or raw[key] is None:
        diag.error(f"missing required key '{key}' in {ctx}", loc)
        return None
    return raw[key]
