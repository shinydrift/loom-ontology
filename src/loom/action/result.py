"""What an action run says happened — typed, never an opaque string.

M4 has to surface validation-rule failures and write conflicts as "typed results an agent can act
on", and a shape invented at the MCP layer would be a shape the runtime never agreed to. So it is
defined here, at the bottom, and `run_<action>` will serialize it rather than compose it.

Two consequences worth naming, because both are choices rather than defaults:

- **Nothing an author, a caller or the data can cause is an exception.** A rule that returned
  false, a key that matched nothing, a key that matched twice, a parameter that couldn't be read as
  its declared type — all of them come back as a `Failure` on a refused `ActionResult`. Exceptions
  are reserved for programming errors. An agent that has to parse a traceback has not been given a
  typed result.
- **Every failure is reported, not just the first.** The same bargain `Diagnostics` makes for the
  spec author: an agent fixing one precondition per call is as miserable as a human fixing one typo
  per run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# --- statuses -------------------------------------------------------------------

APPLIED = "applied"
"""The write committed."""

PREVIEWED = "previewed"
"""`--dry-run`: bound, read and validated, and then deliberately not written."""

REFUSED = "refused"
"""Something the caller, the spec or the data caused. **Nothing was written** — every check runs
before the single write call, so a refusal is always a no-op, exactly as a refused `loom apply`
is."""

FAILED = "failed"
"""The write itself failed after the runtime had decided to go ahead."""

# --- failure codes --------------------------------------------------------------
#
# A closed set. M4 maps these onto MCP error shapes, and a code invented ad hoc at the tool layer
# would be one no client could have been written against.

MISSING_PARAMETER = "missing_parameter"
UNKNOWN_PARAMETER = "unknown_parameter"
TYPE_ERROR = "type_error"
VALIDATION_FAILED = "validation_failed"
EXPRESSION_ERROR = "expression_error"
OBJECT_NOT_FOUND = "object_not_found"
OBJECT_EXISTS = "object_exists"
AMBIGUOUS_KEY = "ambiguous_key"
WRITE_FAILED = "write_failed"
CONFLICT = "conflict"
"""The row moved between the read and the write.

Defined now and raised by nobody. This slice *records* the snapshot every read-then-write saw
(`ActionResult.read_snapshot_id`) but does not check it, so the concurrency slice is a check and a
single new `Failure` — not a new result shape that every caller written against this one would
have to learn. It is the only retryable code, which is why `retryable` exists at all."""

RETRYABLE = frozenset({CONFLICT})
"""Codes where running the same call again is a sensible response. Everything else needs the
caller, the spec or the data to change first."""


@dataclass(frozen=True)
class Failure:
    """One reason a run did not apply.

    `message` is for whoever reads it — for `VALIDATION_FAILED` it is the spec's own
    `validation[].message`, verbatim, because that sentence is the author's and the runtime has
    nothing better to say. `detail` carries the machine-facing specifics: the rule's source text,
    the parameter name, the key that matched twice."""

    code: str
    message: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            out["detail"] = dict(self.detail)
        if self.retryable:
            out["retryable"] = True
        return out


@dataclass(frozen=True)
class ActionResult:
    """The whole outcome of one `run_<action>` / `loom run`.

    `before` and `after` are the object **as the ontology sees it** — property-named, and carrying
    only declared properties. The columns no property maps are carried across the write untouched
    (that is the point of the full-row read) but they are deliberately *not* reported: they are
    somebody else's data, and putting them in an agent-facing result would leak past a governance
    layer that has not yet been written.

    `read_snapshot_id` is the Iceberg snapshot the pre-write read saw. **Recorded, not enforced.**
    The write itself is one Iceberg transaction; the read and the write together are not, and the
    gap between them is the next slice's."""

    action: str
    object_type: str
    operation: str
    status: str
    key: Any = None
    before: Mapping[str, Any] | None = None
    after: Mapping[str, Any] | None = None
    read_snapshot_id: int | None = None
    failures: tuple[Failure, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in (APPLIED, PREVIEWED)

    @property
    def retryable(self) -> bool:
        return any(f.retryable for f in self.failures)

    def as_json(self) -> dict[str, Any]:
        """The serialization `run_<action>` will hand an agent. Values are left as they are; the
        MCP layer's `json_safe` is what knows how to render a Decimal without going through a
        float, and doing it twice would be two answers to that question."""
        return {
            "action": self.action,
            "objectType": self.object_type,
            "operation": self.operation,
            "status": self.status,
            "key": self.key,
            "before": dict(self.before) if self.before is not None else None,
            "after": dict(self.after) if self.after is not None else None,
            # Named for what it is. A field called `snapshotId` would read as something that was
            # checked.
            "readSnapshotId": self.read_snapshot_id,
            "concurrency": "recorded, not enforced",
            "failures": [f.as_json() for f in self.failures],
        }
