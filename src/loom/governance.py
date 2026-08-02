"""Governance — what a deployment withholds, checked where a spec and a deployment are wired.

An ontology says what exists; a `loom.yaml` says what this deployment will show of it. This module
is the grammar between them, and the enforcement lives one rung below every surface that asks —
in `Resolver._projection` and in `_Run._project` — so `loom query`, a `get_` tool and an action's
`before` all withhold the same property for the same reason.

**No policy in this milestone names a principal, and that is the decision the grammar is built
around.** The obvious shape for a policy is "this caller sees these rows", and it was rejected for a
reason that is structural rather than a matter of sequencing: `loom query` and `loom run` have no
transport, so nothing can ever attest an identity to them, and a spawned stdio server carries no
bearer token either. A grammar that could only express a policy against an authenticated caller
would therefore make the *direct* half of M5's own claim — a direct call and an agent call filter
identically — ungovernable by construction, and would leave governance existing only over HTTP,
which is precisely the transport-dependent surface M4's second slice spent a slice proving Loom does
not have.

So what M5 enforces is **deployment-scoped**: one `loom.yaml` filters one way, for every caller of
it, and you serve two audiences by running two deployments. `mcp.actor` gains no second reader — it
remains a string an operator declared about a deployment, reaching the edit log and nothing else —
so nothing here is shaped around a value that an attested per-call principal is going to replace.
When that lands, it arrives as a new *source* for a principal, and `when:` below is the clause it
turns on. Until then `when:` is refused rather than accepted-and-ignored, which is
`_check_governance`'s own posture one level down: a config that is silently ignored reads, to
whoever wrote it, exactly like one that was obeyed.

**Two rules decide almost everything else.**

*The schema is public; the data is not.* A mask **announces itself** — the property list is already
in the spec, in the tool description and in the JSON Schema, so saying "withheld" tells a caller
nothing the surface did not already say. A row predicate (the next slice) will not announce itself,
because the rows *are* the data and "you may not see this one" is an existence oracle over it. The
same principle decides a question that looks unrelated: filtering on a masked property is a refusal
rather than an empty result, because an empty result is an oracle (a substring filter on a withheld
column binary-searches its value) and a refusal only repeats what the mask already said.

*Policies subtract, never add.* A policy can withhold; none can grant, and none can widen what the
config already permits — which is why `mcp.writes` stays a switch of its own rather than being
subsumed into this list. Composition is therefore trivial and total: masks union, and the order
policies are declared in cannot matter. `test_governance.py` asserts the monotonicity rather than
trusting it.

**A masked property is one that no surface returns — including the write path's.** Enforcing on the
read path alone would have shipped a mask that does not mask: `run_<action>` reports `before` and
`after` as declared properties, and `dryRun` makes that readable without changing anything, so a
masked `ssn` would have been one preview away from any caller with an action targeting that type.
`_Run._project` masks for the same reason `Resolver._projection` does.

That leaves the two places an action could still touch what it cannot see, and both are settled
**here, at bind time, by refusing** rather than in the runtime by evaluating: an action whose
validation rule reads a masked property is an oracle the caller drives (`object.ssn == ssnParam`,
with the author's own message on failure), and an effect that writes one destroys data the deployment
said this caller may not read. Both are static facts about the spec — `model.properties_in_play`
already had to name exactly this set for the conflict detail — so a deployment that combines the two
never starts, and the runtime needs no new branch. It is also what keeps the edit log's guarantee
true word for word: *what the record does not name, the run did not change*.

**It is checked in `build_resolver`**, beside `check_capabilities`, and for the reason that slice
wrote down when it borrowed this milestone's principle a milestone early: that function is the one
place a spec and a deployment are paired, so `loom query` refuses exactly what `loom serve` refuses.
Not in `loom validate`, which validates an ontology and does not require a `loom.yaml` at all — a
spec that is valid stays valid whatever a deployment withholds of it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ._shape import check_keys, suggest
from .errors import Diagnostics, SourceLoc
from .model import Ontology, properties_in_play

ENFORCED_KEYS = frozenset({"name", "objectType", "mask"})
"""The keys a policy may carry today, all of which change what a caller gets."""


@dataclass(frozen=True)
class Reserved:
    """A key the grammar names, refuses, and will one day honour.

    Named rather than omitted so that `governance:` can be reviewed for what it will hold, and
    refused rather than ignored for the reason the block itself is refused when Loom cannot enforce
    it. `why` is what the message says; `hint` is the way out that exists today."""

    why: str
    hint: str


RESERVED_KEYS: Mapping[str, Reserved] = {
    "rows": Reserved(
        why="row predicates are not enforced yet",
        hint="drop it until the next slice lands — a declared row filter that Loom ignores is worse "
        "than one it refuses, because it reads like protection",
    ),
    "audit": Reserved(
        why="'no log, no write' is not enforced yet",
        hint="drop it until the slice that makes an unloggable run refuse; today a run whose record "
        "cannot be written still happens and reports 'log_failed'",
    ),
    "when": Reserved(
        why="a principal-conditioned policy needs a caller this deployment can attest, and neither "
        "transport Loom speaks authenticates anybody",
        hint="policies are deployment-scoped: run one deployment per audience, with the policies "
        "that audience gets, until an authenticated transport lands",
    ),
}
"""Every other key, with the reason it is refused.

Together with `ENFORCED_KEYS` this covers `POLICY_KEYS` exactly, under a test — the same device
`negotiate.NEGOTIATED` uses, so a fourth key has to be declared as one kind or the other instead of
arriving as a third: silently accepted. That third kind is how `loom.managed` got written by `apply`
and read by nothing for two milestones."""

POLICY_KEYS = ENFORCED_KEYS | set(RESERVED_KEYS)


class PolicyError(RuntimeError):
    """A policy names something the ontology does not have, or withholds something it cannot."""


@dataclass(frozen=True)
class Policy:
    """One entry under `governance.policies`, as written.

    `object_type` is spelled `objectType:` in YAML, which is Loom's own vocabulary for the same
    thing (§7's `traverse` takes an `objectType`) and dodges a trap the obvious spelling walks into:
    under YAML 1.1 the bare key `on:` resolves to the boolean `True`, so a policy written that way
    would arrive with a key no grammar could name."""

    name: str
    object_type: str
    mask: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicySet:
    """Policies bound to an ontology — resolved, checked, and ready to withhold.

    Empty is the whole of "this deployment governs nothing", and it is the default everywhere, so
    every construction of a `Resolver` or an `ActionRuntime` that predates this milestone still
    means what it meant.

    `masks` is the resolution rather than a cache: object type -> property -> the policy that
    withheld it. Resolved once at bind rather than per read, because the answer cannot change
    between two calls of a process — which is a consequence of policies being deployment-scoped, and
    the thing that stops being true when a principal arrives per call."""

    policies: tuple[Policy, ...] = ()
    masks: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A `PolicySet` holding policies whose masks are not in the resolution is the failure this
        # whole module is against — a config that reads like protection and enforces none, while
        # every layer above reports that a policy is in force. It is reachable only by constructing
        # one directly instead of through `bind_policies`, which is exactly the mistake worth
        # failing loudly on. Stated as "nothing declared was dropped" rather than "every policy
        # contributed", because two policies may legitimately withhold the same property and only
        # one of them can be the one that named it first.
        dropped = [
            f"{p.object_type}.{name}"
            for p in self.policies
            for name in p.mask
            if name not in self.masks.get(p.object_type, {})
        ]
        if dropped:
            raise ValueError(
                f"{', '.join(dropped)} is declared masked and is not in the resolution — a "
                "PolicySet must be built by bind_policies(), which resolves it against an ontology"
            )
        # And the same statement read backwards: nothing is withheld except by a policy that says
        # so. Without it a mask could be enforced with no declaration behind it, which the banner
        # would report as "0 policies withhold ltv" — true, unattributable, and exactly the kind of
        # thing an operator cannot act on.
        declared = {p.name for p in self.policies}
        orphaned = [
            f"{object_type}.{name} (by '{by}')"
            for object_type, props in self.masks.items()
            for name, by in props.items()
            if by not in declared
        ]
        if orphaned:
            raise ValueError(
                f"{', '.join(orphaned)} is withheld by a policy this set does not declare — every "
                "mask is attributable to a policy somebody wrote"
            )

    def __bool__(self) -> bool:
        return bool(self.policies)

    def masked(self, object_type: str) -> tuple[str, ...]:
        """The properties withheld from every read of this object type, in declared order."""
        return tuple(self.masks.get(object_type, {}))

    def masked_by(self, object_type: str, property_name: str) -> str | None:
        """Which policy withholds this property, or None if nothing does."""
        return self.masks.get(object_type, {}).get(property_name)


def parse_policies(raw: object, loc: SourceLoc, diag: Diagnostics) -> tuple[Policy, ...]:
    """`governance.policies` as written, shape-checked but not yet resolved against an ontology.

    Two phases for the reason the ontology loader has two: this one runs where the config is read
    and there is no spec in hand, and `bind_policies` runs where the two are paired. Splitting them
    is also what lets a `loom.yaml` be reported on in one pass with everything else wrong in it,
    accumulating rather than raising."""
    if raw is None:
        return ()
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        diag.error("'governance.policies' must be a list of policies", loc)
        return ()

    out: list[Policy] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        ctx = f"governance.policies[{index}]"
        if not isinstance(entry, dict):
            diag.error(f"{ctx} must be a mapping", loc)
            continue
        check_keys(entry, set(POLICY_KEYS), loc, diag, ctx)

        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            diag.error(f"{ctx} needs a non-empty 'name' — it is what a refusal names", loc)
            continue
        name = name.strip()
        ctx = f"governance policy '{name}'"
        if name in seen:
            diag.error(f"duplicate policy name '{name}' — names identify a policy in a refusal", loc)
            continue
        seen.add(name)

        for key, reserved in RESERVED_KEYS.items():
            if key in entry:
                diag.error(f"{ctx} sets '{key}', but {reserved.why}", loc, reserved.hint)

        object_type = entry.get("objectType")
        if not isinstance(object_type, str) or not object_type.strip():
            diag.error(f"{ctx} needs an 'objectType' naming the type it governs", loc)
            continue

        mask = entry.get("mask")
        if mask is None:
            # Not a warning. A policy that withholds nothing is the shape this module exists to
            # prevent: it reads, to whoever wrote it and to whoever reviews the deployment, exactly
            # like one that is protecting something.
            diag.error(
                f"{ctx} withholds nothing", loc,
                "give it a 'mask' of property names — a policy with no effect reads like protection",
            )
            continue
        if isinstance(mask, (str, bytes)) or not isinstance(mask, Sequence):
            diag.error(f"{ctx}: 'mask' must be a list of property names, got {mask!r}", loc)
            continue
        if not mask:
            diag.error(f"{ctx}: 'mask' is empty, so the policy withholds nothing", loc)
            continue
        if not all(isinstance(m, str) and m.strip() for m in mask):
            diag.error(f"{ctx}: 'mask' entries must be non-empty property names, got {list(mask)!r}", loc)
            continue

        out.append(
            Policy(
                name=name,
                object_type=object_type.strip(),
                mask=tuple(dict.fromkeys(m.strip() for m in mask)),
            )
        )
    return tuple(out)


def bind_policies(ontology: Ontology, policies: Sequence[Policy]) -> PolicySet:
    """Resolve policies against the ontology they govern, or refuse the pairing.

    Every problem at once rather than the first, for `check_capabilities`' reason: an operator
    reconciling a policy file with a spec should learn the whole of what disagrees in one reading.

    Four things a mask cannot withhold, and none of them is a limitation of the implementation:

    - a property no object type declares, or a type the spec has never heard of — a policy that
      protects a misspelling protects nothing, and a mask is exactly the config whose typo is
      invisible in the output it produces;
    - a **primary key**, which every surface addresses a row by: withholding it would leave the
      object impossible to `get_`, to traverse to, or to run an action against, so the honest
      spelling of that intention is to not expose the object type at all. It also guarantees a
      projection is never empty, which is what stops a mask from compiling to a `SELECT` with no
      columns;
    - a property a **link** joins on, whose value is the link's whole meaning;
    - a property an **action** reads in a rule or writes in an effect — see the module docstring.
      This is the one refusal that is about a combination rather than a declaration: the spec is
      fine and the policy is fine, and it is the deployment of the two together that cannot stand.
    """
    problems: list[str] = []
    masks: dict[str, dict[str, str]] = {}

    for policy in policies:
        obj = ontology.object_types.get(policy.object_type)
        if obj is None:
            known = ", ".join(sorted(ontology.object_types)) or "none"
            hint = suggest(policy.object_type, ontology.object_types) or f"known: {known}"
            problems.append(
                f"policy '{policy.name}' governs objectType '{policy.object_type}', which this "
                f"ontology does not declare — {hint}"
            )
            continue

        for name in policy.mask:
            prop = obj.properties.get(name)
            if prop is None:
                known = ", ".join(obj.properties) or "none"
                hint = suggest(name, obj.properties) or f"known: {known}"
                problems.append(
                    f"policy '{policy.name}' masks '{obj.api_name}.{name}', which is not a declared "
                    f"property — {hint}"
                )
                continue
            if name == obj.primary_key:
                problems.append(
                    f"policy '{policy.name}' masks '{obj.api_name}.{name}', which is the primary "
                    "key — every surface addresses a row by it, so masking it withholds the object "
                    "rather than a property. Stop declaring the object type instead"
                )
                continue
            joined = _link_uses(ontology, obj.api_name, name)
            if joined:
                problems.append(
                    f"policy '{policy.name}' masks '{obj.api_name}.{name}', which "
                    f"{', '.join(joined)} joins on — the value is the link's whole meaning"
                )
                continue
            touching = _actions_touching(ontology, obj.api_name, name)
            if touching:
                problems.append(
                    f"policy '{policy.name}' masks '{obj.api_name}.{name}', and "
                    f"{', '.join(touching)}. A rule that reads a withheld property is an oracle the "
                    "caller drives, and an effect that writes one changes data this deployment says "
                    "the caller may not see. Withhold the property or expose the action, not both"
                )
                continue
            masks.setdefault(obj.api_name, {}).setdefault(name, policy.name)

    if problems:
        lines = ["governance policies do not fit this ontology:"]
        lines += [f"  - {p}" for p in problems]
        raise PolicyError("\n".join(lines))

    # Declared order, not the order the policies happened to name them in: what a caller sees
    # withheld should read like the spec it is withheld from.
    ordered = {
        api_name: {
            name: props[name] for name in ontology.object_types[api_name].properties if name in props
        }
        for api_name, props in masks.items()
    }
    return PolicySet(policies=tuple(policies), masks=ordered)


def _link_uses(ontology: Ontology, object_type: str, property_name: str) -> list[str]:
    """Every link that joins on this property, named as a refusal should name it."""
    out = []
    for link in ontology.link_types.values():
        for end in (link.frm, link.to):
            if end.object_type == object_type and end.property == property_name:
                out.append(f"linkType '{link.api_name}'")
                break
    return sorted(out)


def _actions_touching(ontology: Ontology, object_type: str, property_name: str) -> list[str]:
    """Every action whose rules read or whose effects write this property.

    Reads `model.properties_in_play`, which the conflict detail already needed: "the declared
    properties this action reads in a rule or writes in an effect" is one definition with two
    readers rather than two definitions that can drift."""
    out = []
    for action in ontology.actions.values():
        target = ontology.object_types.get(action.target_object_type)
        if target is None or target.api_name != object_type:  # pragma: no cover - validator-enforced
            continue
        if property_name in properties_in_play(action, target):
            verb = "writes" if property_name in action.effect.set_values else "reads"
            out.append(f"action '{action.api_name}' {verb} it")
    return sorted(out)
