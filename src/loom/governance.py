"""Governance — what a deployment withholds, checked where a spec and a deployment are wired.

An ontology says what exists; a `loom.yaml` says what this deployment will show of it. This module
is the grammar between them, and the enforcement lives one rung below every surface that asks —
in `Resolver._projection` and in `_Run._project` for a masked property, in `Resolver._table` and
`_Run._admitted` for a withheld row — so `loom query`, a `get_` tool and an action's `before` all
withhold the same thing for the same reason.

**A policy may name the caller, and exactly half of one may.** `rows:` can be conditioned on who is
asking — by a `when:` guard, by a `principal.<claim>` inside the predicate, or both — and `mask:`
cannot, ever. That split is the whole shape of this grammar and it follows from the first of the two
rules below: *the schema is public; the data is not.*

- A **mask announces itself**, in the tool description, in the `filter` schema and in `masked` on
  every result. §7 says the tool set, its names and its argument namespaces are a function of the
  spec, and a policy may only *subtract* from what one advertises. A subtraction that varies per
  caller has three possible spellings and this codebase refuses all three: assemble the tool set per
  caller (the surface becomes a function of the caller), announce the worst case to everyone
  (narrowing the surface to fit, which §6 refuses to do even for an engine, whose limits are far
  less deliberate than a deployment's), or stop announcing. So `mask:` beside `when:` is refused at
  load, and "HR sees `ssn` and nobody else does" keeps M5's answer: two deployments.
- A **row predicate announces nothing** — a withheld row is simply absent, `get_` says
  `found: false`, and no description gains a sentence. That is exactly what makes it free to
  condition: nothing about the surface moves, and the only thing that differs between two callers is
  which rows come back.

**What M5 decided, and what survived it.** M5's policies were **deployment-scoped**: one `loom.yaml`
filtered one way for every caller, because nothing could attest a caller at all. The argument was
structural rather than a matter of sequencing — `loom query` and `loom run` have no transport, a
spawned stdio server carries no bearer token, and a grammar expressible *only* against an
authenticated caller would have made the direct half of M5's own claim ungovernable and left
governance existing only over HTTP. None of that is repealed. What changed is that `mcp.auth` gives
one surface a caller it has checked, so the grammar now has a conditional half **and a refusal for
every surface that cannot decide it**: `PolicyProgram.select(None)` refuses rather than applying the
unconditional policies alone, because policies subtract and never add — skipping the guarded ones
would show the unattested caller **more**, and `loom query` would be the way around the filter. One
file meaning one thing, with the surfaces that cannot read it declining loudly.

**A principal does not reach the resolver, and that is permanent rather than pending.** What varies
per call is a decided `PolicySet`, selected *above* the resolver by `PolicyProgram.select` — which
works because a principal is constant for the duration of a call, so everything it conditions folds
to a literal before the call begins, including a predicate that names the caller. Every enforcement
site below is untouched, reading a set that is already decided. The M5 sentence that was expected to
give way — `PolicySet.masks`' *resolved once at bind* — **did not**, because masks turned out not to
be conditionable at all; what gives way is only the sentence about `filters`.

**Where a missing claim goes, and why it is not the same answer as an unattestable surface.** Both
are cases of *cannot decide*, and they end differently under one rule: **decidable at pairing time
with somebody to tell → refuse; decidable only per call, with only the caller to tell → withhold
silently.** A surface that cannot attest is known at bind, and an operator is there reading stderr,
so it refuses. A token missing a claim a guard names is known only when the call arrives, and the
only party in the exchange is the caller — telling them a policy did or did not apply to them is the
existence oracle this module refuses everywhere else. So the guard is undecided, and an undecided
guard **applies** the policy, which subtracts more. It is the same direction `admits` fails in for a
row, arrived at by the same rule.

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

**It is checked where a spec and a deployment are paired** — `bind_reads` and `bind_writes`, beside
`check_capabilities` — for the reason M4's capability slice wrote down when it borrowed this
milestone's principle a milestone early: that is the one place the two meet, so `loom query` refuses
exactly what `loom serve` refuses. Not in `loom validate`, which validates an ontology and does not
require a `loom.yaml` at all — a spec that is valid stays valid whatever a deployment withholds of
it.

That sentence needed **narrowing** when policies learned to name a caller, and the narrowing is
worth stating because three slices have cited it. It is a claim about **pairings**, and every
refusal in this module is still surface-blind: the four mask refusals, the predicate subset, the
guard grammar and an undeclared claim all fire identically wherever they are bound. What differs
between `loom query` and `loom serve` is not a check but an **ability** — `build_resolver` is
`bind_reads(...).for_(None)`, and it is the `for_(None)` that a conditional program refuses. Nothing
gained a `surface=` argument, which would have been wrong as well as ugly: `McpConfig.attests` is
true for an attesting config that `loom query` still cannot attest anybody with.

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

A key that will never land must **leave** this grammar rather than sit in it, because *reserved
forever* is a third kind no partition test can see — as invisible as the accepted-and-ignored kind
the partition was written to catch.

**And `RESERVED_KEYS` has now left too, with the last of its entries.** `when:` was the whole of it
and `when:` is enforced, so what remained was a `Reserved` dataclass nothing constructs and a
partition with one live side: the `loom.managed` shape, one level up. What the partition was
standing in for is asserted directly instead — every key this grammar accepts is read into a field
of `Policy` — and a key nobody declared is refused by `check_keys` as it always was. A future
reservation re-adds eight lines and its own test.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ._shape import check_keys, suggest
from .auth import ClaimType, Principal, readable_claims
from .errors import Diagnostics, SourceLoc
from .expr import Binary, Expr, ExprError
from .expr import parse as parse_expr
from .model import Ontology, properties_in_play
from .predicate import check as check_predicate
from .predicate import check_guard, fold, guard_truth

ENFORCED_KEYS = frozenset({"name", "objectType", "mask", "rows", "when"})
"""The keys a policy may carry, all of which change what a caller gets.

**There is no `RESERVED_KEYS` any more, and its deletion is the point rather than tidying.** It held
one entry, `when`, which this milestone enforces; a `Reserved` dataclass nothing constructs and a
partition test with one side empty is the `loom.managed` shape this codebase has already paid for —
structure whose second case does not exist. What the partition bought is bought instead by
`test_governance.py`'s check that every key here is read into a `Policy` field, which catches the
third kind directly (accepted, unenforced, silent) rather than by bookkeeping. `check_keys` already
refuses a key nobody named. A future reservation re-adds eight lines and its own test."""

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

POLICY_KEYS = ENFORCED_KEYS

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
    not here.

    `when` is that same language over the caller instead of the row, and it composes with `rows` as
    an **implication**: a policy whose guard is false withholds nothing. That is what stops it being
    sugar for a longer `rows:` — the same text moved inside the predicate would withhold
    *everything* when the guard is false, which is the opposite disposition. A guard carries no
    `mask`; see this module's docstring for why announcement stays per deployment."""

    name: str
    object_type: str
    mask: tuple[str, ...] = ()
    rows: Expr | None = None
    when: Expr | None = None

    @property
    def conditional(self) -> bool:
        """Whether this policy's effect depends on who is calling.

        A guard or a `principal.` reference inside the predicate — both fold at selection, and
        either one means this policy cannot be decided without a caller."""
        return self.when is not None or (self.rows is not None and _names_a_principal(self.rows))


@dataclass(frozen=True)
class PolicySet:
    """Policies bound to an ontology — resolved, checked, and ready to withhold.

    Empty is the whole of "this deployment governs nothing", and it is the default everywhere, so
    every construction of a `Resolver` or an `ActionRuntime` that predates this milestone still
    means what it meant.

    `masks` is the resolution rather than a cache: object type -> property -> the policy that
    withheld it. **Resolved once at bind, for every caller, and that is now a rule rather than a
    consequence.** M5 wrote it as a consequence of policies being deployment-scoped, and named the
    condition under which it lapses ("the thing that stops being true when a principal arrives per
    call"). It did not lapse: a principal arrives per call and a mask still cannot be conditioned on
    one, because a mask *announces itself* — into the tool description, the `filter` schema and the
    `masked` field — and an announcement that varies per caller makes the tool set a function of the
    caller rather than of the spec (§7). `PolicyProgram.select` asserts it: every selection returns
    the identical `masks` object.

    What does vary is `filters`, and only because a row predicate announces nothing. Object type ->
    the policies that filter it, in declared order, each with the expression it filters by. Kept as
    a list rather than pre-combined so a refusal, a banner or a test can still name *which* policy is
    doing the withholding — `predicate_for` is the combination, and it is a conjunction because
    policies subtract and never add.

    `decided` is what separates a set that governs a caller from one that only describes the
    deployment. `PolicyProgram.announcements()` builds an undecided set for the tool builder, whose
    filters are the expressions *as written* — with `principal.` still in them — so a banner can name
    which policies filter what. Reading a row with it is refused rather than allowed to fail open:
    see `Resolver._table`."""

    policies: tuple[Policy, ...] = ()
    masks: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    filters: Mapping[str, tuple[tuple[str, Expr], ...]] = field(default_factory=dict)
    decided: bool = True

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
        # A guarded policy may legitimately be absent — that is what a guard *is* — so the statement
        # narrows to the policies whose applying was never in question. Every unguarded `rows:` is
        # still in the resolution or this set was not built by `bind_policies`.
        unfiltered = [
            p.name
            for p in self.policies
            if p.rows is not None
            and p.when is None
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


@dataclass(frozen=True)
class PolicyProgram:
    """Policies bound to an ontology, and not yet decided for a caller.

    **This is the seam the whole milestone turns on, and it splits `bind_policies` by *time* rather
    than by responsibility.** Bind time keeps every static spec × config refusal — all four mask
    refusals, the predicate subset, the guard grammar, undeclared properties and undeclared claims —
    firing whether or not a caller ever arrives. Per call adds only *selection*: which already-bound
    policies apply to this principal, and what their predicates say once the caller is folded into
    them. Every enforcement site below is untouched, because what reaches them is still a
    `PolicySet` that is already decided.

    **A principal reaches here and stops.** It is read for two things — a guard's answer and a fold's
    literals — and neither survives the call: what leaves is a set of expressions naming nobody. That
    is what keeps *the resolver receives no identity* true by construction rather than by scope.

    `claims` is `mcp.auth.claims` plus the built-ins, carried because selection needs the declared
    types to decide what of a token is readable at all (`auth.readable_claims`)."""

    policies: tuple[Policy, ...] = ()
    masks: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    filters: Mapping[str, tuple[Policy, ...]] = field(default_factory=dict)
    claims: Mapping[str, ClaimType] = field(default_factory=dict)
    _decided: PolicySet | None = field(default=None, compare=False, repr=False)
    """The one answer, built once, for a program no caller can change — see `select`.

    None when the program *is* conditional, which is not an optimisation: a set built from
    unfolded expressions naming a principal is exactly the thing that must not be reachable, so it
    is not built."""

    def __post_init__(self) -> None:
        if not self.conditional:
            object.__setattr__(self, "_decided", self._as_written())

    @property
    def conditional(self) -> bool:
        """Whether any policy here needs a caller before it can be decided."""
        return any(p.conditional for p in self.policies)

    def select(self, principal: Principal | None) -> PolicySet:
        """The policies that apply to this caller, decided, with the caller folded out.

        **`select(None)` on a conditional program is the refusal decision 2 asks for, and it names
        no surface.** `loom query`, `loom run` and a stdio server reach it at build, before anything
        is read, because they are asking for a decided set while naming nobody; an HTTP server with
        `mcp.auth` never reaches it, because it selects per call with a principal in hand. That is
        one function rather than a check three call sites re-derive from `McpConfig.attests` — which
        would also get `loom query` wrong, since an attesting *config* read by a command with no
        transport still attests nobody.

        The alternative — treat an unattested caller as principal-less and apply only the
        unconditional policies — is disqualified by this module's own invariant: policies subtract,
        never add, so skipping the guarded ones gives that caller **less** subtraction. `loom query`
        would become the way to read what the governed MCP surface withholds.

        **A guard that cannot be decided applies the policy.** Undecided is not false: a token that
        carries no `dept` has not said it is outside HR. Applying is the withholding direction, and
        it is the same direction `admits` fails in for a row. The rule under both, and under the
        refusal above: *decidable at pairing time with somebody to tell → refuse; decidable only per
        call, with only the caller to tell → withhold silently*, because "a policy did or did not
        apply to you" is the existence oracle §6.1 refuses.

        A program with nothing conditional returns the **same object** every time, so every
        deployment that predates this milestone is provably unchanged rather than argued to be."""
        if self._decided is not None:
            return self._decided
        if principal is None:
            named = ", ".join(p.name for p in self.policies if p.conditional)
            raise PolicyError(
                f"governance policy {named} names the caller, and nobody is attested here — this "
                "surface can never name a caller, so it cannot decide which policies apply. "
                "'mcp.auth' over 'transport: http' attests one; 'loom query', 'loom run' and a "
                "spawned stdio server cannot. Refusing rather than applying the policies that are "
                "left, which would show this caller more than the served surface shows"
            )
        values = readable_claims(principal, self.claims)
        filters: dict[str, tuple[tuple[str, Expr], ...]] = {}
        for object_type, policies in self.filters.items():
            applied = tuple(
                (p.name, fold(p.rows, values))
                for p in policies
                if p.when is None or guard_truth(p.when, values) is not False
            )
            if applied:
                filters[object_type] = applied
        return PolicySet(policies=self.policies, masks=self.masks, filters=filters)

    def announcements(self) -> PolicySet:
        """What this deployment says about itself, for the tool set and the banner — never for a read.

        Masks, which are the same for every caller by construction, plus the row filters **as
        written** so a startup banner can still name which policy filters what. It is `decided=False`
        because those expressions may still name a principal, and a read performed with it would be
        a read nobody selected: `Resolver._table` refuses it rather than letting it fail open."""
        return self._as_written(decided=False)

    def _as_written(self, decided: bool = True) -> PolicySet:
        return PolicySet(
            policies=self.policies,
            masks=self.masks,
            filters={
                object_type: tuple((p.name, p.rows) for p in policies if p.rows is not None)
                for object_type, policies in self.filters.items()
            },
            decided=decided,
        )


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

        for key, moved in MOVED_KEYS.items():
            if key in entry:
                diag.error(f"{ctx} sets '{key}', which is no longer a policy key", loc, moved)

        object_type = entry.get("objectType")
        if not isinstance(object_type, str) or not object_type.strip():
            diag.error(f"{ctx} needs an 'objectType' naming the type it governs", loc)
            continue

        mask, rows, when = entry.get("mask"), entry.get("rows"), entry.get("when")
        if mask is None and rows is None:
            # Not a warning. A policy that withholds nothing is the shape this module exists to
            # prevent: it reads, to whoever wrote it and to whoever reviews the deployment, exactly
            # like one that is protecting something.
            diag.error(
                f"{ctx} withholds nothing", loc,
                "give it a 'mask' of property names, a 'rows' predicate, or both — a policy with no "
                "effect reads like protection"
                + ("; 'when:' says who a policy applies to, not what it withholds" if when else ""),
            )
            continue
        if mask is not None and when is not None:
            # **The refusal this slice is built around.** A mask announces itself — in every tool
            # description, in the `filter` schema, in `masked` on every result — and §7 says the
            # tool set and its argument namespaces are a function of the spec. A mask that varies
            # per caller has three possible spellings and this module refuses all three: assemble
            # the tool set per caller (the surface becomes a function of the caller), announce the
            # worst case to everyone (narrowing the surface to fit, which §6 refuses to do even for
            # an engine), or stop announcing (the rule a mask exists under). Rows carry no
            # announcement, which is exactly why they may be conditioned at no cost to the surface.
            diag.error(
                f"{ctx} masks a property and carries 'when:', which cannot vary per caller", loc,
                "a mask announces itself in the tool description, the filter schema and every "
                "result, so conditioning it would make the tool set a function of the caller "
                "rather than of the spec. Condition 'rows:' instead, or serve the two audiences "
                "from two deployments",
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

        guard: Expr | None = None
        if when is not None:
            if not isinstance(when, str) or not when.strip():
                diag.error(f"{ctx}: 'when' must be an expression, got {when!r}", loc)
                continue
            try:
                guard = parse_expr(when)
            except ExprError as e:
                diag.error(f"{ctx}: 'when' is not a valid expression: {e}", loc)
                continue
            if rows is None:
                diag.error(
                    f"{ctx} carries 'when:' and withholds nothing", loc,
                    "a guard says which callers a policy applies to; without a 'rows:' predicate "
                    "there is no policy for it to guard",
                )
                continue

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
            Policy(
                name=name,
                object_type=object_type.strip(),
                mask=columns,
                rows=predicate,
                when=guard,
            )
        )
    return tuple(out)


def bind_policies(
    ontology: Ontology,
    policies: Sequence[Policy],
    claims: Mapping[str, ClaimType] | None = None,
) -> PolicyProgram:
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

    **All four are checked as though every policy always applies, and conditionality changes
    nothing here** — which costs nothing, because a mask may not carry `when:` at all. They are mask
    refusals about a *spec*, and a spec does not vary per caller; checking them per caller would be a
    deployment that starts and then fails for one caller, which is the shape "refuse rather than
    degrade" exists to prevent.

    `claims` is `mcp.auth.claims`, which is what a `when:` guard and a `principal.` reference in a
    predicate are checked against. A policy naming a claim where none is declared is refused here
    for the same reason a mask naming an undeclared property is: it protects a misspelling."""
    problems: list[str] = []
    masks: dict[str, dict[str, str]] = {}
    filters: dict[str, list[Policy]] = {}
    declared_claims = dict(claims or {})

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
            refusals = check_predicate(policy.rows, obj, ontology.object_types, declared_claims)
            problems += [
                f"policy '{policy.name}' filters rows of '{obj.api_name}' by "
                f"'{policy.rows.raw}': {refusal}"
                for refusal in refusals
            ]
            guard_refusals = (
                check_guard(policy.when, declared_claims) if policy.when is not None else []
            )
            problems += [
                f"policy '{policy.name}' applies when '{policy.when.raw}': {refusal}"
                for refusal in guard_refusals
            ]
            if not refusals and not guard_refusals:
                filters.setdefault(obj.api_name, []).append(policy)
        elif policy.when is not None:  # pragma: no cover - parse_policies refuses a guard with no rows
            problems.append(
                f"policy '{policy.name}' carries a guard and no 'rows:' predicate to guard"
            )

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
    return PolicyProgram(
        policies=tuple(policies),
        masks=ordered,
        filters={name: tuple(entries) for name, entries in filters.items()},
        claims=declared_claims,
    )


def _names_a_principal(expr: Expr) -> bool:
    """Whether an expression reads the caller. One definition, read by `Policy.conditional`."""
    return any(len(ref.path) == 2 and ref.path[0] == "principal" for ref in expr.refs())


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
