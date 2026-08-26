"""One YAML reader for every file Loom is handed, and the one thing it refuses that PyYAML allows.

`yaml.safe_load` resolves a **duplicate key** by keeping the last one, silently. That is the wrong
answer for both files this project reads, because both are files somebody reviews: a `loom.yaml` with
two `governance:` blocks is a deployment whose reviewed governance is not the governance it runs, and
a spec file with `primaryKey` given twice is a schema whose second answer wins with nothing said.

It is not a hypothetical. `examples/retail/dashboard/loom.yaml` shipped a commented `governance:`
block above a live one, so uncommenting it exactly as `docs/guide/dashboard.md` instructs produced a
document with two `governance:` keys — and the policies an operator had just switched on were
dropped on the floor, leaving a dashboard that rendered identically and a walkthrough that appeared
to do nothing. The example now has one place for that block; this module is why the next one cannot
happen quietly.

The refusal is a `yaml.YAMLError`, so both readers report it through the `invalid YAML` path they
already have rather than growing a second one — and it renders as one line, because PyYAML's own
`ConstructorError` layout leads with the mark of the *mapping* and buries the duplicate five lines
below it.
"""

from __future__ import annotations

from typing import Any

import yaml


class DuplicateKey(yaml.YAMLError):
    """A mapping said the same key twice. Carries the line, and prints as one sentence."""

    def __init__(self, key: Any, mark: yaml.error.Mark | None) -> None:
        where = f" (line {mark.line + 1})" if mark is not None else ""
        super().__init__(
            f"duplicate key {key!r}{where} — YAML keeps the last one silently, so the file you "
            f"reviewed and the file Loom reads would differ"
        )


class _Loader(yaml.SafeLoader):
    """`yaml.SafeLoader`, minus the silence about a key said twice."""


def _no_duplicate_keys(loader: _Loader, node: yaml.MappingNode, deep: bool = False) -> dict:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in seen:
                raise DuplicateKey(key, key_node.start_mark)
            seen.add(key)
        except TypeError:  # pragma: no cover - an unhashable key fails the shape checks anyway
            continue
    return loader.construct_mapping(node, deep=deep)


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)


def load(text: str) -> Any:
    """`yaml.safe_load`, with duplicate keys refused rather than resolved."""
    return yaml.load(text, _Loader)  # noqa: S506 - _Loader is a SafeLoader
