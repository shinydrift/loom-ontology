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
