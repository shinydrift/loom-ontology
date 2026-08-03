"""Governance — what a deployment withholds, checked where a spec and a deployment are wired.

An ontology says what exists; a `loom.yaml` says what this deployment will show of it. This module
is the grammar between them, and the enforcement lives one rung below every surface that asks —
in `Resolver._projection` and in `_Run._project` for a masked property, in `Resolver._table` and
`_Run._admitted` for a withheld row — so `loom query`, a `get_` tool and an action's `before` all
withhold the same thing for the same reason.

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
remains a string an operator declared about a deployment, reaching the edit log and nothing else.

**A principal now has a source, and `when:` is still refused — the two are less connected than this
docstring first assumed.** M6's first slice landed `mcp.auth`: a bearer token verified against an
issuer's key set, over the one transport that can carry one, recorded in the edit log beside
`mcp.actor`. What that changed here is *nothing yet*, and the reason is the paragraph above rather
than a missing slice: `loom query`, `loom run` and a stdio server still cannot attest anybody, so a
`when:` policy would filter one surface and be skipped on three. That is why the slice that turns it
on also **refuses** to build any surface that cannot attest against a config carrying it — one file
meaning one thing, with two surfaces declining it, rather than one file meaning two things depending
on who reads it. The invariant that forces the refusal is this module's own: *policies subtract,
never add*, so skipping a conditional policy would leave the unattested caller seeing **more**, and
`loom query` would be the way around the filter.

Until that slice, `when:` is refused rather than accepted-and-ignored, which is `_check_governance`'s
own posture one level down: a config that is silently ignored reads, to whoever wrote it, exactly
like one that was obeyed.

**When it lands, a principal still will not reach the resolver, and that is now permanent rather
than pending.** What varies per call is a decided `PolicySet`, selected *above* the resolver — which
works because a principal is constant for the duration of a call, so everything it conditions folds
before the call begins, including a predicate that names the caller. Every enforcement site below
stays as it is, reading a set that is already decided. The M5 sentence that gives way is the one on
`PolicySet.masks` — *resolved once at bind* becomes *resolved once per caller* — and it gives way
exactly where it predicted it would.

**Two rules decide almost everything else.**

*The schema is public; the data is not.* A mask **announces itself** — the property list is already
in the spec, in the tool description and in the JSON Schema, so saying "withheld" tells a caller
nothing the surface did not already say. A row predicate **does not**, because the rows *are* the
data and "you may not see this one" is an existence oracle over it: a filtered row is simply
absent, `get_` says `found: false`, and no tool description gains a sentence. The same principle
decides a question that looks unrelated: filtering on a masked property is a refusal rather than an
empty result, because an empty result is an oracle (a substring filter on a withheld column
binary-searches its value) and a refusal only repeats what the mask already said.

It also decides what a row predicate does when it cannot be evaluated over a row, which was the
hardest question of the slice that landed the predicates. Nothing can be *reported*: per row there
is no channel, and per call, "this row exists but I could not decide about it" is the oracle again.
So an undecided predicate does not admit, and `predicate.py` carries the rest of that argument —
including why it makes the read path's SQL and the write path's in-process evaluation agree instead
of drift.

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

**What a policy is not, and where `audit:` went.** That key was reserved in this grammar for two
slices, and it has *left* rather than landed, because neither half of what it named is a policy in
the sense the rest of this module means.

*No log, no write* subtracts an ability, which is the right shape — but it names no object type.
Unloggability is a fact about a **catalog**: the log is one table per catalog, reached through a
port a catalog either implements or does not, so a per-type spelling would let a config say
"Customer edits must be logged, Order edits need not" about a single fact concerning a single
catalog. It is also a switch an operator reads, which is precisely the argument that kept
`mcp.writes` out of this list — folding a switch into a policy list turns a line somebody reads into
a set they have to evaluate. So it landed beside `policies:` as `governance.edit_log`, and it is the
first governance clause that binds **one plane only**: `build_runtime` and not `build_resolver`,
because it subtracts from the write surface and the read plane produces no records. `log.require_edit_log`
carries what it can and cannot promise.

*Retention* fails the subtraction test outright. It withholds nothing from any caller; it deletes
from the lake, on a table no policy can name, by an actor Loom does not have — nothing here runs on
a schedule. It is also the one thing that would cost the edit log the property its write-then-log
ordering was chosen for: expire a record and a reader holding a stamped snapshot with no matching
row can no longer tell an expired edit from a lost one. So there is no key for it and no key coming;
it is a command Loom has not built (spec-v0 "Open edges"), and a config key that is only a default
for a command nobody runs is exactly the `loom.managed` shape this codebase has already paid for.

A key that will never land must **leave** this grammar rather than sit in it. `ENFORCED_KEYS` and
`RESERVED_KEYS` partition `POLICY_KEYS` under a test, and *reserved forever* is a third kind that
test cannot see — as invisible as the accepted-and-ignored kind it was written to catch. So
`RESERVED_KEYS` now means one thing only, a named future slice turns this on, and `when:` is the
whole of it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ._shape import check_keys, suggest
from .errors import Diagnostics, SourceLoc
from .expr import Binary, Expr, ExprError
from .expr import parse as parse_expr
from .model import Ontology, properties_in_play
from .predicate import check as check_predicate

ENFORCED_KEYS = frozenset({"name", "objectType", "mask", "rows"})
"""The keys a policy may carry today, all of which change what a caller gets."""


@dataclass(frozen=True)
class Reserved:
    """A key the grammar names, refuses, and will one day honour.

    Named rather than omitted so that `governance:` can be reviewed for what it will hold, and
    refused rather than ignored for the reason the block itself is refused when Loom cannot enforce
    it. `why` is what the message says; `hint` is the way out that exists today."""

    why: str
    hint: str


MOVED_KEYS: Mapping[str, str] = {
    "audit": "'no log, no write' is a switch on a whole deployment rather than a policy — it names "
    "no objectType, because a catalog either can hold an edit log or cannot. Set "
    "'governance.edit_log: required' beside 'policies:'. A retention window is not there and is "
    "not coming: removing a record would leave a reader unable to tell an expired edit from a lost "
    "one, so no Loom command deletes from '_loom_meta.edits'",
}
"""Keys this grammar used to name, and where they went instead.

Strictly unnecessary by this codebase's own rule — a config that was refused is a config nobody
deployed, so nothing written against `audit:` has to change. It is here because the key was
*advertised*: §6.1 named it for two slices, and the refusal it produced told operators it was
coming. The honest end of a reservation names its destination, rather than falling through to
`unexpected key 'audit'` with nothing behind it."""

RESERVED_KEYS: Mapping[str, Reserved] = {
    "when": Reserved(
        why="a principal-conditioned policy needs a caller every surface of this deployment can "
        "attest, and only an HTTP transport with 'mcp.auth' can — 'loom query', 'loom run' and a "
        "stdio server can never attest anybody, so a policy conditioned on a caller would filter "
        "one surface and be skipped on three",
        hint="policies are deployment-scoped: run one deployment per audience, with the policies "
        "that audience gets. 'mcp.auth' now attests a caller and records it in the edit log; what "
        "is not built yet is conditioning a policy on one",
    ),
}
"""Every other key, with the reason it is refused.

Together with `ENFORCED_KEYS` this covers `POLICY_KEYS` exactly, under a test — the same device
`negotiate.NEGOTIATED` uses, so a fifth key has to be declared as one kind or the other instead of
arriving as a third: silently accepted. That third kind is how `loom.managed` got written by `apply`
and read by nothing for two milestones.

A reservation has exactly two honest ends, and both have now happened. `rows` was here and is
**enforced**, which is what the reservation was for: nothing written against the refusal had to
change, because a config that was refused is a config nobody deployed. `audit` was here and
**left** — see `MOVED_KEYS` and this module's docstring — because a key that will never land is a
third kind of its own, and the partition test below cannot see it. What is left in here therefore
means one thing: a named future slice turns it on."""

POLICY_KEYS = ENFORCED_KEYS | set(RESERVED_KEYS)

EDIT_LOG_OPTIONAL = "optional"
EDIT_LOG_REQUIRED = "required"
EDIT_LOG_POSTURES = (EDIT_LOG_OPTIONAL, EDIT_LOG_REQUIRED)
"""What `governance.edit_log` may say, and `optional` is what it says when nobody says anything.

A sibling of `policies:` rather than an entry in it, for `mcp.writes`' reason — see this module's
docstring. Enforced by `action.log.require_edit_log`, from `build_runtime`."""


class PolicyError(RuntimeError):
    """A policy names something the ontology does not have, or withholds something it cannot."""


@dataclass(frozen=True)
class Policy:
    """One entry under `governance.policies`, as written.

    `object_type` is spelled `objectType:` in YAML, which is Loom's own vocabulary for the same
    thing (§7's `traverse` takes an `objectType`) and dodges a trap the obvious spelling walks into:
    under YAML 1.1 the bare key `on:` resolves to the boolean `True`, so a policy written that way
    would arrive with a key no grammar could name.

    `rows` is a parsed `Expr` rather than the string it was written as, because there is one
    expression language and this is it: `rows: "object.tier == 'gold'"` and an action's
    `rule: "newTier != object.tier"` are the same grammar, parsed by the same function. What it may
    *contain* is narrower — see `predicate.py` — and that is checked against the ontology at bind,
    not here."""

    name: str
    object_type: str
    mask: tuple[str, ...] = ()
    rows: Expr | None = None


@dataclass(frozen=True)
class PolicySet:
    """Policies bound to an ontology — resolved, checked, and ready to withhold.

    Empty is the whole of "this deployment governs nothing", and it is the default everywhere, so
    every construction of a `Resolver` or an `ActionRuntime` that predates this milestone still
    means what it meant.

    `masks` is the resolution rather than a cache: object type -> property -> the policy that
    withheld it. Resolved once at bind rather than per read, because the answer cannot change
    between two calls of a process — which is a consequence of policies being deployment-scoped, and
    the thing that stops being true when a principal arrives per call.

    That condition is now half met and the sentence is still true, which is worth stating because it
    is the seam the next slice cuts on. `mcp.auth` attests a principal per call, but no policy is
    conditioned on one, so the resolution still cannot change between two calls. When `when:` lands,
    what changes is *when* this is resolved and never *where* it is read: a `PolicySet` stays a
    decided, frozen set that the resolver holds, and the selection moves one rung above it. A
    program with no conditional policies will return the same object for every caller, so this class
    keeps meaning exactly what it means today for every config that exists.

    `filters` is the same statement for rows: object type -> the policies that filter it, in
    declared order, each with the expression it filters by. Kept as a list rather than pre-combined
    so a refusal, a banner or a test can still name *which* policy is doing the withholding —
    `predicate_for` is the combination, and it is a conjunction because policies subtract and never
    add."""

    policies: tuple[Policy, ...] = ()
    masks: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    filters: Mapping[str, tuple[tuple[str, Expr], ...]] = field(default_factory=dict)

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
        # Both statements again for rows, because a filter that is declared and not resolved is the
        # same failure as a mask that is: a config that reads like protection and enforces none.
        unfiltered = [
            p.name
            for p in self.policies
            if p.rows is not None
            and p.name not in {name for entries in self.filters.values() for name, _ in entries}
        ]
        if unfiltered:
            raise ValueError(
                f"policy {', '.join(unfiltered)} declares a row filter that is not in the "
                "resolution — a PolicySet must be built by bind_policies()"
            )
        unattributed = [
            f"{object_type} (by '{name}')"
            for object_type, entries in self.filters.items()
            for name, _ in entries
            if name not in declared
        ]
        if unattributed:
            raise ValueError(
                f"rows of {', '.join(unattributed)} are filtered by a policy this set does not "
                "declare — every filter is attributable to a policy somebody wrote"
            )

    def __bool__(self) -> bool:
        return bool(self.policies)

    def masked(self, object_type: str) -> tuple[str, ...]:
        """The properties withheld from every read of this object type, in declared order."""
        return tuple(self.masks.get(object_type, {}))

    def masked_by(self, object_type: str, property_name: str) -> str | None:
        """Which policy withholds this property, or None if nothing does."""
        return self.masks.get(object_type, {}).get(property_name)

    def filtered_by(self, object_type: str) -> tuple[str, ...]:
        """Which policies filter the rows of this object type, in declared order.

        For an operator reading a startup banner, never for a caller reading a result: a mask
        announces itself because the property names are already in the spec, and a row predicate
        does not because the rows are the data. See `governance.py`'s two rules."""
        return tuple(name for name, _ in self.filters.get(object_type, ()))

    def predicate_for(self, object_type: str) -> Expr | None:
        """Every policy's row filter for this type, as one expression, or None if none filters it.

        **A conjunction, and that is forced rather than chosen.** Policies subtract and never add,
        so two policies filtering one type can only mean *both*; an `||` would let a second policy
        widen what the first permitted, which is the one thing no policy may do. It also makes
        composition order-free, exactly as masks union — `test_governance.py` asserts the
        monotonicity rather than trusting it.

        Combined here rather than at bind so `filters` keeps naming the policy behind each half."""
        entries = self.filters.get(object_type, ())
        if not entries:
            return None
        if len(entries) == 1:
            return entries[0][1]
        root: object = entries[0][1].root
        for _, expr in entries[1:]:
            root = Binary("&&", root, expr.root)
        return Expr(root=root, raw=" && ".join(f"({expr.raw})" for _, expr in entries))


def parse_edit_log(raw: object, loc: SourceLoc, diag: Diagnostics) -> str:
    """`governance.edit_log`, which is a posture about a deployment and not a promise about a run.

    **The name is the decision.** What this key was called while it was reserved is "no log, no
    write", and that sentence cannot be delivered by anything built on Iceberg: there is no
    transaction spanning a row's table and `_loom_meta.edits`, so no ordering of the two makes
    *every applied run is logged* true. `edit_log: required` says instead the one thing that is
    true — **a deployment that cannot log does not run** — and says it about a deployment rather
    than about a run. A boolean spelled `no_log_no_write: true` would have read as the per-run
    guarantee, which is the failure this whole block refuses everywhere else: a config that promises
    more than it enforces reads, to whoever wrote it, exactly like one that was obeyed.

    `edit_log` and not `audit` is Loom's own vocabulary for the thing being demanded — `EditLogWriter`,
    `EditLog`, `_loom_meta.edits` — the same reason a policy's subject is `objectType:` rather than
    the obvious `on:`.

    **Default `optional`**, for the reason `mcp.writes` is off by default: an upgrade and a catalog
    that implements no edit-log port are two things that happen for unrelated reasons, and a
    deployment that never asked for this posture is not asking to stop working."""
    if raw is None:
        return EDIT_LOG_OPTIONAL
    if not isinstance(raw, str) or raw.strip() not in EDIT_LOG_POSTURES:
        allowed = ", ".join(EDIT_LOG_POSTURES)
        diag.error(
            f"'governance.edit_log' must be one of {allowed}, got {raw!r}",
            loc,
            suggest(raw, EDIT_LOG_POSTURES) if isinstance(raw, str) else None,
        )
        return EDIT_LOG_OPTIONAL
    return raw.strip()


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
        # `MOVED_KEYS` is passed to `check_keys` so a key that left this grammar is reported once,
        # by the branch below that knows where it went, rather than twice — once as unknown and
        # once as moved.
        check_keys(entry, set(POLICY_KEYS) | set(MOVED_KEYS), loc, diag, ctx)

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
        for key, moved in MOVED_KEYS.items():
            if key in entry:
                diag.error(f"{ctx} sets '{key}', which is no longer a policy key", loc, moved)

        object_type = entry.get("objectType")
        if not isinstance(object_type, str) or not object_type.strip():
            diag.error(f"{ctx} needs an 'objectType' naming the type it governs", loc)
            continue

        mask, rows = entry.get("mask"), entry.get("rows")
        if mask is None and rows is None:
            # Not a warning. A policy that withholds nothing is the shape this module exists to
            # prevent: it reads, to whoever wrote it and to whoever reviews the deployment, exactly
            # like one that is protecting something.
            diag.error(
                f"{ctx} withholds nothing", loc,
                "give it a 'mask' of property names, a 'rows' predicate, or both — a policy with no "
                "effect reads like protection",
            )
            continue

        columns: tuple[str, ...] = ()
        if mask is not None:
            if isinstance(mask, (str, bytes)) or not isinstance(mask, Sequence):
                diag.error(f"{ctx}: 'mask' must be a list of property names, got {mask!r}", loc)
                continue
            if not mask:
                diag.error(f"{ctx}: 'mask' is empty, so the policy withholds nothing", loc)
                continue
            if not all(isinstance(m, str) and m.strip() for m in mask):
                diag.error(f"{ctx}: 'mask' entries must be non-empty property names, got {list(mask)!r}", loc)
                continue
            columns = tuple(dict.fromkeys(m.strip() for m in mask))

        predicate: Expr | None = None
        if rows is not None:
            # Parsed here and checked against the ontology at bind, the same two phases the whole
            # module splits along: this runs where the config is read and there is no spec in hand,
            # so it can say "that is not an expression" and nothing about what it may say.
            if not isinstance(rows, str) or not rows.strip():
                diag.error(f"{ctx}: 'rows' must be an expression, got {rows!r}", loc)
                continue
            try:
                predicate = parse_expr(rows)
            except ExprError as e:
                diag.error(f"{ctx}: 'rows' is not a valid expression: {e}", loc)
                continue

        out.append(
            Policy(name=name, object_type=object_type.strip(), mask=columns, rows=predicate)
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
    filters: dict[str, list[tuple[str, Expr]]] = {}

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

        if policy.rows is not None:
            # A row predicate has no equivalent of the four refusals above, and that is a
            # consequence of what it does rather than an oversight. A mask cannot withhold a
            # primary key, a link's join property or a property an action touches, because each of
            # those is a surface still trying to *use* the value. A predicate uses the value and
            # shows nobody: it may filter on the key, on a join property, on a property an action
            # reads — and on one the same policy masks, which is Loom filtering rather than the
            # caller. What it may not do is be a predicate the two planes could answer differently,
            # which is the whole of what `predicate.check` refuses.
            refusals = check_predicate(policy.rows, obj, ontology.object_types)
            problems += [
                f"policy '{policy.name}' filters rows of '{obj.api_name}' by "
                f"'{policy.rows.raw}': {refusal}"
                for refusal in refusals
            ]
            if not refusals:
                filters.setdefault(obj.api_name, []).append((policy.name, policy.rows))

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
    return PolicySet(
        policies=tuple(policies),
        masks=ordered,
        filters={name: tuple(entries) for name, entries in filters.items()},
    )


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
