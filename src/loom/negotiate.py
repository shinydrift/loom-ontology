"""Capability negotiation — what a spec demands of an engine, checked where the two are wired.

An `Engine` reports what it can do; an ontology implies what will be asked of it. This module is
the function between them, and it exists because the gap is otherwise discovered one tool call at
a time: an engine with no `OFFSET` serves page one perfectly and fails on page two.

**A requirement is something a spec can demand and an engine can fail.** That sentence is the whole
boundary, and it is what settles the question `Capabilities` was left holding — `native_merge` is a
field on the same dataclass and is deliberately not negotiated here, because no spec can demand it.
Writes have a universal fallback (the catalog's `RowWriter`, which every catalog implements), so a
`native_merge: false` engine cannot fail a spec; it can only be a slower way to serve one. It
selects an implementation, not a possibility. `NEGOTIATED` / `NOT_NEGOTIATED` below make that a
decision a new flag has to take rather than one it can drift past.

**Three requirements, and one of them is not a spec feature.** M4's roadmap box read "validate spec
features vs. `engine.capabilities()`", and two of the three are exactly that — `joins` comes from
declaring a link, `case_insensitive_like` from declaring a string property searchable. `offset` is
not: *every* generated `search_` / `list_` / `traverse` tool carries an `offset` argument for every
ontology there is, so it is a constant requirement of the **surface** rather than a function of the
spec. It is checked here anyway, and the box is corrected rather than satisfied as written, because
the question a deployment is actually asking is "can this engine serve the tools I am about to
advertise" — and the answer has to cover the parts of that surface no spec chose.

**The outcome is a refusal, never a narrowing.** Loom already refuses rather than degrades in three
places that were each argued separately — `cmd_serve` would rather not start than advertise tools
that fail on every call, `loom apply` refuses a breaking plan whole with no `--force`, and
`mcp.writes: true` refuses a non-loopback bind. The reason they agree is visible from here. The
degradations available are dropping `traverse`, stripping `offset` out of the page schema, and
compiling `Contains` down to `Eq`; the first two make the generated surface a function of the
*engine*, which is the one claim (spec §7 — the surface is a function of the spec and nothing else)
that survived a second transport intact and should not be spent on a config mismatch. The third is
worse than either, and worse than failing: an exact match where the spec promised substring returns
rows, so nothing errors, and the agent believes an answer that is wrong.

**A fourth flag was considered for typed filters and refused, by this module's own rule.** M7 put
range comparisons in a caller's hands, and the question was whether `>=` is a floor every engine
meets or a `NEGOTIATED` capability. It is a floor: every dialect that can say `WHERE c = ?` can say
`WHERE c >= ?`, so a `range_comparisons` flag is one **no adapter could ever set false** — a
requirement nothing can fail is not a requirement, it is the `loom.managed` shape (a field written
and read by nothing) wearing `native_merge`'s hat. `case_insensitive_like` is the contrast that makes
the line real: Trino has no `ILIKE`, so an engine genuinely can fail it. Nothing about typed filters
changes what is demanded here — a searchable `date` demands nothing new, and `case_insensitive_like`
is still demanded by a searchable **string**, because `filters.operators()` offers `contains` for
exactly those properties.

**The write path is not negotiated, and that is not an omission.** `loom run` and the `run_` tools
go through `ActionRuntime`, which reads a whole row and writes it back through the catalog's ports
and never compiles a plan — so it asks the engine for nothing and there is nothing to check. An
engine that fails negotiation still runs actions, in the same sense that an ontology with no engine
configured at all could: the surface that would refuse to start is the read half. What `loom serve`
does with that is refuse anyway, because it builds both halves and one of them cannot stand.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

from .model import Ontology
from .query.engine import Capabilities

NEGOTIATED = frozenset({"joins", "offset", "case_insensitive_like"})
"""Capability flags a `Requirement` can name, and therefore that an ontology can fail an engine on."""

NOT_NEGOTIATED = frozenset({"name", "native_merge"})
"""The rest of `Capabilities`, listed so that leaving a flag out is a decision rather than an
omission — `test_negotiate.py` asserts these two sets cover the dataclass exactly, so a fourth flag
fails a test until somebody says which kind of fact it is.

`name` identifies an adapter and asserts nothing. `native_merge` is a routing hint: it says writes
*could* go through the engine, never that they must, so no spec can demand it and no engine can
fail one for it."""


class CapabilityError(RuntimeError):
    """An engine cannot do something the surface this ontology generates requires."""


@dataclass(frozen=True)
class Requirement:
    """One thing an ontology needs an engine to be able to do.

    `demanded_by` names what asked, in the spec's own vocabulary, because "this engine does not
    support joins" is not actionable and "linkType 'placedBy' is traversed as a join" is — the two
    ways out of a refusal are changing the engine and changing the spec, and only the second one
    needs to know which declaration to look at."""

    capability: str
    demanded_by: tuple[str, ...]
    because: str

    def __post_init__(self) -> None:
        # What makes `NEGOTIATED` load-bearing rather than documentation. A requirement naming a
        # flag outside it — `native_merge`, or a fourth one added without a decision — would
        # otherwise start refusing ontologies for a capability nothing can demand, and the coverage
        # test alone cannot see it, because it only reads the sets.
        if self.capability not in NEGOTIATED:
            raise ValueError(
                f"'{self.capability}' is not a negotiated capability (negotiated: "
                f"{', '.join(sorted(NEGOTIATED))}). A requirement is something a spec can demand "
                "and an engine can fail; if this is one, add it to NEGOTIATED, and if it is a "
                "routing hint, it belongs in NOT_NEGOTIATED and in no Requirement."
            )

    def met_by(self, capabilities: Capabilities) -> bool:
        return bool(getattr(capabilities, self.capability))


def requirements(ontology: Ontology) -> tuple[Requirement, ...]:
    """What this ontology's generated surface will ask an engine to do.

    Pure, and takes no engine: what a spec demands is a fact about the spec, so it is assertable
    without a catalog, a connection or an adapter — the same reason `Engine.compile` is pure.

    Ordered as `Capabilities` declares its fields, so a refusal listing several reads the same way
    every time."""
    out: list[Requirement] = []

    links = tuple(
        f"linkType '{link.api_name}' ({link.frm.object_type} -> {link.to.object_type}"
        + (f", through {link.through.catalog}.{link.through.table}" if link.through else "")
        + ")"
        for link in ontology.link_types.values()
    )
    if links:
        because = "a traverse joins the two backing tables on the linked properties"
        if any(link.through is not None for link in ontology.link_types.values()):
            # Said only when it is true of this ontology. A reason that describes a shape the spec
            # in front of you does not have is one more thing to rule out while reading a refusal.
            because += ", and twice for a link that goes through a join table"
        out.append(
            Requirement(capability="joins", demanded_by=tuple(sorted(links)), because=because)
        )

    if ontology.object_types:
        # Not conditional on anything a spec says: the page arguments are Loom's own vocabulary and
        # every read tool carries them. See the module docstring.
        out.append(
            Requirement(
                capability="offset",
                demanded_by=("every generated search_/list_ tool, and traverse",),
                because=(
                    "the page arguments are on every read tool, so asking for a second page is an "
                    "OFFSET — an engine without one serves page 1 and fails page 2"
                ),
            )
        )

    # `Resolver._filters` emits a `Contains` for exactly this condition and an `Eq` for everything
    # else, so a searchable *enum* — a closed set, where substring matching would only add
    # ambiguity — demands nothing here.
    likes = tuple(
        f"{obj.api_name}.{name}"
        for obj in ontology.object_types.values()
        for name in obj.searchable
        if name in obj.properties and obj.properties[name].type.kind == "string"
    )
    if likes:
        out.append(
            Requirement(
                capability="case_insensitive_like",
                demanded_by=tuple(sorted(likes)),
                because=(
                    "a searchable string property matches on case-insensitive substring, which "
                    "compiles to a LIKE over lowered values"
                ),
            )
        )

    return tuple(out)


def unmet(ontology: Ontology, capabilities: Capabilities) -> tuple[Requirement, ...]:
    """Every requirement this engine does not meet. Empty is the whole of "yes"."""
    return tuple(req for req in requirements(ontology) if not req.met_by(capabilities))


def check_capabilities(ontology: Ontology, capabilities: Capabilities) -> None:
    """Raise unless this engine can serve everything this ontology's surface will ask for.

    All of them at once, not the first: an operator who has to swap an adapter should learn what it
    needs to support in one reading, which is the same reason `Diagnostics` collects spec errors
    rather than raising on the first."""
    missing = unmet(ontology, capabilities)
    if not missing:
        return
    lines = [f"engine '{capabilities.name}' cannot serve this ontology:"]
    for req in missing:
        lines.append(f"  - {req.capability} — required by {', '.join(req.demanded_by)}")
        lines.append(f"      {req.because}")
    lines.append("Loom does not narrow a surface to fit an engine: the tools it generates are a")
    lines.append("function of the spec. Point `engine:` at an adapter that supports these, or")
    lines.append("remove from the spec whatever demands them.")
    raise CapabilityError("\n".join(lines))


def capability_fields() -> frozenset[str]:
    """Every field on `Capabilities`, so the coverage assertion has one place to read them from."""
    return frozenset(f.name for f in fields(Capabilities))
