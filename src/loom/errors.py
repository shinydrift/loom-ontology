"""Error types for spec loading and validation.

Design choice: the validator accumulates *every* error it can find and raises them
together, rather than failing on the first. A spec author fixing one typo at a time is
miserable; `loom validate` should report the whole batch in one pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceLoc:
    """Where a problem lives, for a human-readable prefix on the message."""

    file: str
    kind: str | None = None  # objectType | linkType | action
    api_name: str | None = None

    def __str__(self) -> str:
        parts = [self.file]
        if self.kind and self.api_name:
            parts.append(f"{self.kind} '{self.api_name}'")
        elif self.kind:
            parts.append(self.kind)
        return " · ".join(parts)


@dataclass
class SpecError:
    """A single validation or load failure. Never raised alone by the pipeline —
    always collected into a SpecErrors bundle so the caller sees the full picture."""

    message: str
    loc: SourceLoc | None = None
    hint: str | None = None

    def render(self) -> str:
        head = f"{self.loc}: {self.message}" if self.loc else self.message
        return f"{head}\n    hint: {self.hint}" if self.hint else head


class SpecErrors(Exception):
    """One or more SpecErrors. `errors` preserves discovery order."""

    def __init__(self, errors: list[SpecError]):
        self.errors = errors
        super().__init__(self._render())

    def _render(self) -> str:
        n = len(self.errors)
        lines = [f"{n} problem{'s' if n != 1 else ''} in ontology spec:"]
        lines += [f"  - {e.render()}" for e in self.errors]
        return "\n".join(lines)


@dataclass
class Diagnostics:
    """Mutable sink threaded through the pipeline. Collects hard errors and advisory
    warnings separately; `raise_if_errors()` turns accumulated errors into SpecErrors."""

    errors: list[SpecError] = field(default_factory=list)
    warnings: list[SpecError] = field(default_factory=list)

    def error(self, message: str, loc: SourceLoc | None = None, hint: str | None = None) -> None:
        self.errors.append(SpecError(message, loc, hint))

    def warn(self, message: str, loc: SourceLoc | None = None, hint: str | None = None) -> None:
        self.warnings.append(SpecError(message, loc, hint))

    def raise_if_errors(self) -> None:
        if self.errors:
            raise SpecErrors(self.errors)
