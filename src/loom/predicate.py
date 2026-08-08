"""A governance row predicate and a policy's guard — §5, restricted to what has to agree with what.

**Two grammars, and the difference between them is how many times an expression is answered.** A
`rows:` predicate is answered **twice** — compiled into SQL on the read plane and evaluated in
process on the write plane — so it is restricted to the subset below, whose whole rule is that Loom
rather than the engine decides what every operator means. A `when:` guard is answered **once**, in
this process, over the claims of the token in flight, before any table is named. No engine sees it,
so the subset does not bind it, and `contains` — refused in `rows:` for want of an IR node and a
second evaluator to keep agreeing with it — is legal there. The rule survived contact with a claim
by being restated rather than bent: *the subset is a property of expressions that must be answered
twice.*

They also compose in opposite directions. A guard is an **implication** — a policy whose guard is
false withholds nothing — and a predicate is a **conjunction** with every other policy's. That is
why a guard cannot be sugar for a longer predicate: the same text moved inside `rows:` would
withhold everything where the guard withholds nothing.

`principal.<claim>` may appear in either, and `fold` is what makes it cost nothing: a principal is
constant for the duration of a call, so by the time a predicate reaches a plane it compares against
a *literal* and names nobody. What a claim may be is declared in `mcp.auth.claims` (`auth.ClaimType`)
and checked here exactly as a property is — the same refusal for a typo, the same comparability
check against what it is compared with.


`governance.py` says a deployment may withhold rows; this module is what a `rows:` expression
*means*. It has to mean the same thing twice, because the two planes read differently and neither
can be made to read like the other:

- the **read path** compiles it into the query, so it filters before `ORDER BY`/`LIMIT`/`OFFSET`
  and `hasMore` and `offset` stay true. Post-filtering in the resolver was never available: a page
  of 50 that governance thins to 31 would report `hasMore: false` on a full table;
- the **write path** evaluates it in process over one row, because `ActionRuntime` reads through
  the `Catalog` port rather than the resolver (it needs the whole physical row to carry unmapped
  columns across a modify), and an agent that cannot see a row must not be able to act on it.

Two functions, therefore — `lower()` and `admits()` — and the only claim worth making about them
is that they agree. `test_predicate.py` asserts it differentially against real DuckDB over a table
full of nulls, because the disagreement they can have is exactly the one nulls cause.

**Null: three answers, one admission rule.** A predicate is true, false, or **undecided**, and a
row is admitted *only on true*. The two obvious ways out are both worse:

- *Emulate §5's two-valued logic in the lowering* — make every leaf definitely true or false on
  both planes — **fails open under negation**. Totalize `object.ltv > 100` to false when `ltv` is
  null and `!(object.ltv > 100)` becomes true: a predicate written to exclude admits, because a
  value was missing. For a governance filter that is the wrong direction to fail in, and it is not
  an edge case — "not expired", "not over limit" are how ranges get written.
- *Refuse any predicate that touches null* costs `object.deletedAt == null`, the most ordinary
  policy there is, and still does not close the question: a table can hold a null in a column the
  spec declares non-nullable — Loom already knows tables contradict specs, which is why
  `ambiguous_key` exists — so the runtime meets an undecidable leaf anyway and needs an answer for
  it regardless.

So: **`==` and `!=` never return undecided.** §5's "null is a value" is kept exactly — `null ==
null` is true, `null != 'gold'` is true — and it is carried into SQL by `ir.Compare`, whose `==`
is null-safe by definition of the node. That is the *one* operator where §5 and SQL genuinely
disagree, and §5 wins on both planes. **Everything §5 refuses to answer for a null** — the four
ordering operators — is undecided rather than an error. **`!`, `&&`, `||` propagate undecided by
Kleene's rules**, which is what SQL's `NOT`/`AND`/`OR` already do, so the two lowerings agree by
construction rather than by emulation, and negation stays fail-closed: `NOT undecided` is
undecided, and undecided never admits.

Undecided rather than `expression_error` — which is what M3 gave a validation rule that cannot be
evaluated — because the two sites differ in whether there is anybody to tell. A rule has a caller
who asked for the run and can fix the input. A policy predicate has none: per row there is no
channel at all, and per call, "this row exists but I could not decide about it" is exactly the
existence oracle §6.1 refuses. The only disposition available to a filter that cannot decide is to
not admit. So M3's rule is untouched where it applies, and what differs between a rule and a policy
is not the meaning of an operator but the disposition of *cannot decide*.

**The lowerable subset**: operands are `object.<prop>` references and literals, operators are the
six comparisons, composition is `&& || !`, and nothing else. Stated as a rule rather than a list —
*a predicate is lowerable when Loom, not the engine, decides what every operator means.* Loom emits
the comparison and binds the constant; nothing in the subset asks an engine to compute a value, so
there is nothing for an engine to compute differently. Everything else is refused at load naming
the node, never silently unenforced.

`NOT_LOWERABLE` carries the refusals key by key and, with `LOWERABLE`, covers `expr`'s whole
operator and function set under a test — `ENFORCED_KEYS`/`RESERVED_KEYS`' device, applied to a
grammar instead of a config. The set may only ever **grow**: widening it accepts predicates that
used to be refused and cannot change the meaning of one already written, which is the whole reason
the null semantics are settled now rather than iterated. A node already accepted may never change
meaning, and the differential test is what fails when one does.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ._shape import suggest
from .auth import BUILT_IN_CLAIMS, ClaimType
from .evaluate import EvalError, Scope, evaluate_node
from .expr import BINARY_OPS, FUNCTIONS, UNARY_OPS, Binary, Call, Expr, Literal, Ref, Unary
from .model import ObjectType
from .query.ir import And, ColumnRef, Compare, Const, Not, Operand, Or, Predicate
from .types import PropType

COMPARISONS = frozenset({"==", "!=", "<", "<=", ">", ">="})
NULL_SAFE = frozenset({"==", "!="})
"""The two §5 answers for a null, and therefore the two `ir.Compare` lifts out of SQL's `=`."""

ORDERINGS = COMPARISONS - NULL_SAFE
CONNECTIVES = frozenset({"&&", "||", "!"})
LOWERABLE = COMPARISONS | CONNECTIVES

_ENGINE_WOULD_COMPUTE_IT = (
    "a governance predicate is a boolean combination of comparisons between the row's own "
    "properties and literals, because that is the whole of what Loom — rather than whichever "
    "engine is executing — decides the meaning of"
)

NOT_LOWERABLE: Mapping[str, str] = {
    "+": f"{_ENGINE_WOULD_COMPUTE_IT}. Arithmetic and string '+' are the engine's: it decides "
    "what integer division does and silently mixes a decimal with a float where §5 refuses to",
    "-": f"{_ENGINE_WOULD_COMPUTE_IT}. Arithmetic is the engine's, and engines disagree about it",
    "*": f"{_ENGINE_WOULD_COMPUTE_IT}. Arithmetic is the engine's, and engines disagree about it",
    "/": f"{_ENGINE_WOULD_COMPUTE_IT}. Arithmetic is the engine's, and engines disagree about it",
    "lower()": f"{_ENGINE_WOULD_COMPUTE_IT}. Case folding is the engine's, not Loom's",
    "upper()": f"{_ENGINE_WOULD_COMPUTE_IT}. Case folding is the engine's, not Loom's",
    "len()": f"{_ENGINE_WOULD_COMPUTE_IT}. Length in characters or in bytes is the engine's answer",
    "coalesce()": f"{_ENGINE_WOULD_COMPUTE_IT}. coalesce() is the tempting one and the one that "
    "most has to go: it is the null tool, and what null means here is precisely what Loom owns "
    "rather than borrows per row from whoever is executing",
    "contains": "'contains' is a policy's 'when:' guard operator and cannot filter rows. A guard is "
    "answered once per call, in process, over a list only Loom holds — nothing asks an engine "
    "anything, which is why the subset rule does not reach it. Inside 'rows:' the same operator "
    "would have to lower a list into SQL, which is a node this IR does not have and a second "
    "evaluator to keep it agreeing with. Put the membership test in 'when:'",
    "now()": f"{_ENGINE_WOULD_COMPUTE_IT}. now() is the one refusal that is not about engines — it"
    "never reaches one, it would bind as a parameter — but it puts a clock inside a filter, and "
    "*which instant, the read's or the run's* deserves an answer written down rather than one that "
    "arrives as a side effect. Compare against a literal, or wait for the slice that stamps one "
    "instant per call",
}
"""Every other node the grammar has, with the reason a policy may not use it.

Refused at load, naming the node, rather than accepted and unenforced — a `rows:` Loom half-obeys
reads, to whoever wrote it, exactly like one it obeyed. Widening any of these later is safe by
construction: it accepts a policy that used to be refused."""


class _Undecided:
    """Neither true nor false. SQL calls it `NULL`; §5 calls it an `EvalError`; a governance
    predicate calls it *not admitted*."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<undecided>"

    def __bool__(self) -> bool:
        # Never truthy by accident: the admission rule is `is True`, and anything reaching for the
        # shortcut should fail loudly rather than admit a row nobody decided about.
        raise TypeError("an undecided predicate has no truth value — a row is admitted only on true")


UNDECIDED = _Undecided()

Truth = bool | _Undecided


# ---- what a policy may say -----------------------------------------------------


@dataclass(frozen=True)
class _Ctx:
    """What is in scope for one expression, and the problems found in it.

    `obj` is the object type whose rows are being filtered, or **None in a guard** — a `when:` is
    answered once per call, before any row is read, so there is no row for `object.` to name. That
    one field is the whole difference between the two grammars this module checks, which is why they
    are one walk with a context rather than two walks that can drift.

    `claims` is every claim a policy may name: `mcp.auth.claims` plus the two `auth.BUILT_IN_CLAIMS`
    the verifier requires of every token."""

    objects: Mapping[str, ObjectType]
    claims: Mapping[str, ClaimType]
    obj: ObjectType | None
    problems: list[str]

    @property
    def in_a_guard(self) -> bool:
        return self.obj is None


class _ArrayOf:
    """A list-valued claim, as an operand type. Comparable to nothing; `contains` is its only verb."""

    def __init__(self, element: PropType) -> None:
        self.element = element


def check(
    expr: Expr,
    obj: ObjectType,
    objects: Mapping[str, ObjectType],
    claims: Mapping[str, ClaimType] | None = None,
) -> list[str]:
    """Every reason this expression cannot govern this object type, or an empty list.

    Every problem rather than the first, because `bind_policies` collects these alongside the mask
    refusals and an operator reconciling a policy file with a spec should learn the whole of what
    disagrees in one reading.

    `claims` is what `principal.<claim>` may name. Empty (the default) is a deployment that attests
    nobody, and a predicate naming a principal there is refused for that reason rather than for a
    typo — see `_claim_type`.

    Deliberately not `validator._ExprChecker`, which infers a type for the *whole* language and is
    written to be optimistic — it "returns None when the type is unknown, never a guess", so that a
    spec author is not told about a type Loom could not work out. A governance predicate needs the
    opposite posture: every node accounted for, nothing inferred generously, and the comparability
    of two operands actually checked rather than assumed. That is a stricter walk over a smaller
    grammar, not a second call into the same one. `objects` is only for the one thing it does
    borrow: an `objectRef` property travels as the referenced object's primary key, so it compares
    as that key's type — the resolution `_ExprChecker._resolve` already makes for a rule."""
    ctx = _Ctx(objects=objects, claims=_claims(claims), obj=obj, problems=[])
    _walk(expr.root, ctx)
    # Deduplicated, order preserved: `lower(a) == lower(b)` offends twice for one reason, and a
    # refusal that says the same sentence twice reads like two different problems.
    problems = list(dict.fromkeys(ctx.problems))
    if not problems and not _reads_a_property(expr.root):
        problems.append(
            f"'{expr.raw}' names no property of '{obj.api_name}', so it is the same answer for "
            "every row — a predicate that admits everything reads like protection and is none, and "
            "one that admits nothing withholds the object type. A condition on the caller alone is "
            "what 'when:' is for. Otherwise stop declaring the object type"
        )
    return problems


def check_guard(expr: Expr, claims: Mapping[str, ClaimType] | None = None) -> list[str]:
    """Every reason this expression cannot guard a policy, or an empty list.

    **A guard is not a row predicate, and the two differ in one thing that decides everything else:
    a guard is answered once per call and a predicate is answered twice per row.** A `rows:`
    expression has to mean the same thing to SQL and to an in-process evaluator, which is the whole
    of what the lowerable subset is about — *Loom, not the engine, decides what every operator
    means*. A guard reaches no engine: it is evaluated in this process, over the claims of the token
    in flight, before any table is named. So the subset does not bind it, and `contains` — refused in
    `rows:` for want of an IR node and a second evaluator — is legal here.

    What a guard may **not** do is name a row. `object.` is refused rather than deferred: a guard is
    decided before a read happens, and a condition on a row is what `rows:` is. The two compose as
    an *implication* — a policy whose guard is false withholds nothing — which is also why a guard
    cannot be folded into the predicate beside it: the same text inside `rows:` would withhold
    everything instead."""
    ctx = _Ctx(objects={}, claims=_claims(claims), obj=None, problems=[])
    _walk(expr.root, ctx)
    problems = list(dict.fromkeys(ctx.problems))
    if not problems and not _names_a_claim(expr.root):
        problems.append(
            f"'{expr.raw}' names no claim of the caller, so it is the same answer for every caller "
            "— a guard that is always true is a policy with no guard, and one that is always false "
            "is a policy that never applies. Drop 'when:', or name a claim"
        )
    return problems


def _claims(claims: Mapping[str, ClaimType] | None) -> Mapping[str, ClaimType]:
    """Every claim a policy may name. The built-ins are not declared and cannot be redeclared."""
    return {**(claims or {}), **BUILT_IN_CLAIMS}


def _walk(node: Any, ctx: _Ctx) -> None:
    """One pass that checks the subset and the types together, because they are one question:
    whether this node is something both planes can be made to answer identically.

    Every node it recurses into stands where a condition belongs — a comparison's own operands are
    `_compare`'s business — which is what lets it name a bare `object.tier` as *not a condition*
    rather than inferring a type for it and complaining about the type."""
    if isinstance(node, Unary) and node.op == "!":
        _walk(node.operand, ctx)
        return

    if isinstance(node, Binary):
        if node.op in CONNECTIVES:
            _walk(node.left, ctx)
            _walk(node.right, ctx)
            return
        if node.op in COMPARISONS:
            _compare(node, ctx)
            return
        if node.op == "contains" and ctx.in_a_guard:
            _contains(node, ctx)
            return

    if _refuse(node, ctx):
        return

    # A bare reference or literal where a condition belongs.
    where = "a policy guard" if ctx.in_a_guard else "a row predicate"
    problems = ctx.problems
    problems.append(
        f"'{_render(node)}' is not a condition — {where} is a comparison, or several "
        "joined by '&&', '||' and '!'"
    )


def _refuse(node: Any, ctx: _Ctx) -> bool:
    """Name a node the subset does not carry, wherever it turns up.

    One function because the refusal has to be the same sentence in both positions — a `lower()`
    standing where a condition belongs and a `lower()` standing where an operand belongs are the
    same thing the deployment cannot compile, and an author who moved it has not fixed it.

    It names the sub-expression rather than the operator, because `'object.ltv + 1' cannot be used`
    is something an author can find in the file and `'+' cannot be used` is something they have to
    go looking for."""
    if isinstance(node, Call):
        why = NOT_LOWERABLE.get(
            f"{node.name}()", f"'{node.name}()' is not a function of this language"
        )
    elif isinstance(node, (Unary, Binary)) and node.op in NOT_LOWERABLE:
        why = NOT_LOWERABLE[node.op]
    else:
        return False
    where = "a policy guard" if ctx.in_a_guard else "a row predicate"
    ctx.problems.append(f"'{_render(node)}' cannot be used in {where}: {why}")
    return True


def _compare(node: Binary, ctx: _Ctx) -> None:
    problems = ctx.problems
    left = _operand_type(node.left, ctx)
    right = _operand_type(node.right, ctx)
    if left is _BAD or right is _BAD:
        return
    for side in (left, right):
        if isinstance(side, _ArrayOf):
            problems.append(
                f"'{_render(node)}' compares against a list claim, which no operator here compares "
                "— membership is 'contains', and 'contains' belongs in a policy's 'when:' guard"
            )
            return
    if node.op in ORDERINGS and (left is None or right is None):
        # `object.ltv > null` is undecided for every row, so it withholds the whole object type
        # while reading like a filter. §5 already refuses to order a null; this is that refusal
        # moved to load time, where it can name the expression.
        problems.append(
            f"'{_render(node)}' orders against null, which is undecided for every row — null is a "
            "value you can test with '==' or '!=', not one you can order"
        )
        return
    if left is not None and right is not None and not left.comparable_to(right):
        what = "a policy guard" if ctx.in_a_guard else "a row predicate"
        problems.append(
            f"{what} compares '{left.kind}' with '{right.kind}', which are not comparable "
            "types — the same rule a validation rule's operands follow"
        )
        return
    _enum_membership(node, left, right, ctx)


def _enum_membership(node: Binary, left: Any, right: Any, ctx: _Ctx) -> None:
    """Refuse an enum compared against a value the enum does not have.

    `object.tier != 'closed'` where `tier` is `[bronze, silver, gold]` type-checks — an enum compares
    as its string storage, which is the rule that lets `object.tier == 'gold'` work at all — and then
    means the same thing for every row. That is precisely the offence `check()` already refuses one
    door down for a predicate naming no property: *a predicate that admits everything reads like
    protection and is none, and one that admits nothing withholds the object type.* The two differ
    only in how the constant gets in, so they refuse for the same reason, in the same words.

    It is worth a rule of its own because of which direction the accident falls in. `!=` against a
    non-member is always true, so the policy silently withholds **nothing** — a deployment that
    believes it filters rows and serves all of them, with no refusal and no warning anywhere, and
    the value in the file looking exactly like a value the column holds. Loom finding out at load is
    the only place anybody finds out at all.

    Only the declared side is checked, and only against a string literal. A claim is never an enum —
    `auth.ClaimType`'s vocabulary is `string`, `string[]` and `boolean` — so in a guard this has
    nothing to say, and it is written to be silent there rather than special-cased out.

    **Equality only.** `object.tier > 'closed'` orders an enum against a string and is a different
    answer per row whether or not the string is a member, so there is nothing constant to refuse —
    only `==` and `!=` collapse to the same answer for every row."""
    if node.op not in NULL_SAFE:
        return
    for declared, other in ((left, node.right), (right, node.left)):
        if not isinstance(declared, PropType) or declared.kind != "enum" or not declared.values:
            continue
        if not isinstance(other, Literal) or not isinstance(other.value, str):
            continue
        if other.value in declared.values:
            continue
        always = "true" if node.op == "!=" else "false"
        withholds = "withholds nothing at all" if node.op == "!=" else "withholds every row"
        hint = suggest(other.value, declared.values) or f"declared: {', '.join(declared.values)}"
        ctx.problems.append(
            f"'{_render(node)}' compares an enum against a value it cannot hold — {hint}. The "
            f"comparison is {always} for every row, so the policy {withholds} while reading like "
            "one that filters"
        )
        return


def _contains(node: Binary, ctx: _Ctx) -> None:
    """`principal.groups contains 'auditors'` — the motivating case, and the only shape it has.

    A list on the left and a string on the right, both checked, because the list can only ever be a
    claim: no *property* type is a list, so there is nothing else in the language this operator could
    stand against until complex types land."""
    problems = ctx.problems
    left = _operand_type(node.left, ctx)
    right = _operand_type(node.right, ctx)
    if left is _BAD or right is _BAD:
        return
    if not isinstance(left, _ArrayOf):
        problems.append(
            f"'{_render(node)}' asks what a non-list contains — 'contains' tests membership in a "
            "list claim, so its left operand is a claim declared as a list (e.g. 'groups: string[]')"
        )
        return
    if isinstance(right, _ArrayOf) or right is None or not right.comparable_to(left.element):
        got = "a list" if isinstance(right, _ArrayOf) else ("null" if right is None else right.kind)
        problems.append(
            f"'{_render(node)}' looks for {got} in a list of '{left.element.kind}' — 'contains' "
            "tests one value against the elements of a list claim"
        )


class _Bad:
    """An operand already reported on. Distinct from `None`, which is the type of `null`."""


_BAD = _Bad()


def _operand_type(node: Any, ctx: _Ctx) -> PropType | None | _Bad | _ArrayOf:
    problems = ctx.problems
    if isinstance(node, Literal):
        return PropType.of_literal(node.value)
    if isinstance(node, Ref):
        if len(node.path) == 2 and node.path[0] == "object":
            if ctx.obj is None:
                problems.append(
                    f"'{_render(node)}' names a row, and a guard is answered once per caller before "
                    "any row is read — a condition on a row is what 'rows:' is for"
                )
                return _BAD
            prop = ctx.obj.properties.get(node.path[1])
            if prop is None:
                known = ", ".join(ctx.obj.properties) or "none"
                hint = suggest(node.path[1], ctx.obj.properties) or f"known: {known}"
                problems.append(
                    f"'object.{node.path[1]}' is not a property of '{ctx.obj.api_name}' — {hint}"
                )
                return _BAD
            return _resolved(prop.type, ctx.objects)
        if len(node.path) == 2 and node.path[0] == "principal":
            return _claim_type(node.path[1], ctx)
        # A bare name is a *parameter* reference in §5 and a policy has no parameters. One language
        # keeps one meaning for each reference form rather than growing a second one here.
        forms = "'principal.<claim>' for a claim of the caller"
        if ctx.obj is not None:
            forms = f"'object.{node.path[0]}' for a property of '{ctx.obj.api_name}', or {forms}"
        problems.append(
            f"'{'.'.join(node.path)}' is not something a policy can reference — a bare name is a "
            f"parameter and a policy has none. Write {forms}"
        )
        return _BAD
    if not _refuse(node, ctx):  # pragma: no cover - the parser emits no other operand shapes
        problems.append(f"'{_render(node)}' is neither a property nor a literal")
    return _BAD


def _claim_type(name: str, ctx: _Ctx) -> PropType | _Bad | _ArrayOf:
    """The declared type of `principal.<name>`, or a refusal naming what is declared.

    **A claim is declared or it is refused**, and this is the check that buys the declaration. The
    alternative — accept any name and find out per call — is a typo that fails *closed*: the guard
    is undecided, the policy applies to everybody, and a deployment quietly withholds more than it
    was asked to with nothing anywhere saying why. Every other reference form in this language is
    checked against a declaration, and this one is checked against `mcp.auth.claims`."""
    declared = ctx.claims.get(name)
    if declared is not None:
        return _ArrayOf(declared.element_type()) if declared.array else declared.element_type()
    if len(ctx.claims) == len(BUILT_IN_CLAIMS):
        # Nothing was declared at all, which is a different sentence: this deployment either attests
        # nobody or has not said what its tokens carry, and neither is a misspelling.
        ctx.problems.append(
            f"'principal.{name}' names a claim this deployment does not declare, and "
            "'mcp.auth.claims' declares none — a policy may name a caller only where the deployment "
            "says what its tokens carry (and only 'transport: http' can carry a token at all)"
        )
        return _BAD
    known = ", ".join(sorted(ctx.claims))
    hint = suggest(name, ctx.claims) or f"declared: {known}"
    ctx.problems.append(f"'principal.{name}' is not a declared claim — {hint}")
    return _BAD


def _resolved(prop_type: PropType, objects: Mapping[str, ObjectType]) -> PropType:
    """An `objectRef` compares as the referenced object's primary key, because that is what it
    travels as — the same resolution `_ExprChecker._resolve` makes for a rule and `coerce_value`
    makes for a value. Without it `object.owner == 'c1'` would be refused as a comparison of
    'objectRef' with 'string', which is a type nothing on the wire ever has."""
    if prop_type.kind == "objectRef":
        referenced = objects.get(prop_type.object_type or "")
        if referenced is not None:
            return referenced.pk_property.type
    return prop_type


def _render(node: Any) -> str:
    """A node written back out the way its author wrote it, for a message that names the offence.

    A refusal that says "in 'object.ltv > null'" is one an author can find in the file; one that
    says "in policy 'hide-x'" makes them read the whole expression to guess which half is meant."""
    if isinstance(node, Ref):
        return ".".join(node.path)
    if isinstance(node, Literal):
        return "null" if node.value is None else repr(node.value)
    if isinstance(node, Call):
        return f"{node.name}({', '.join(_render(a) for a in node.args)})"
    if isinstance(node, Unary):
        return f"{node.op}{_render(node.operand)}"
    if isinstance(node, Binary):
        return f"{_render(node.left)} {node.op} {_render(node.right)}"
    return repr(node)  # pragma: no cover - the parser emits no others


def _reads_a_property(node: Any) -> bool:
    return _references(node, "object")


def _names_a_claim(node: Any) -> bool:
    return _references(node, "principal")


def _references(node: Any, namespace: str) -> bool:
    if isinstance(node, Ref):
        return len(node.path) == 2 and node.path[0] == namespace
    if isinstance(node, Unary):
        return _references(node.operand, namespace)
    if isinstance(node, Binary):
        return _references(node.left, namespace) or _references(node.right, namespace)
    return False


# ---- the caller, folded out ------------------------------------------------------

UNDECIDABLE = Binary("<", Literal(None), Literal(None))
"""A leaf that is undecided on both planes, and the whole of what a missing claim leaves behind.

`null < null`: SQL answers `NULL`, §5 refuses to order a null, so `lower()` emits `? < ?` with two
null parameters and `truth()` catches an `EvalError`. Both planes call it undecided by the rules they
already had, and Kleene propagation does the rest — an undecided leaf under `||` beside a true one is
still true, exactly as it would be for a null column.

**A generated node rather than a sentinel, and the alternative is worse than it looks.** A
`DENY_ALL` marker would have to be understood at both enforcement sites, which is the one thing this
milestone promised not to touch; it would also over-subtract, since "this policy admits nothing"
is a stronger statement than "this leaf could not be decided" and the two differ under `||`.
Substituting `null` for the missing claim was the obvious try and is **wrong in the dangerous
direction**: `==` is null-safe here, so `object.ownerId == principal.sub` would come back *true* for
every row whose owner is null — a missing claim admitting rows it was written to withhold."""


def fold(expr: Expr, claims: Mapping[str, Any]) -> Expr:
    """The predicate with the caller substituted in — a `rows:` expression made ordinary.

    This is where "a principal never reaches the resolver" is paid for rather than asserted. A
    principal is constant for the duration of a call, so `object.ownerId == principal.sub` is a
    comparison against a *literal* by the time anything reads a row, and what the resolver receives
    is a predicate indistinguishable from one an operator wrote by hand. Nothing below this line
    knows a caller exists.

    `claims` holds only values this deployment declared and this token typed as declared
    (`auth.readable_claims`), so anything absent from it is undecidable rather than wrong — see
    `UNDECIDABLE`. The `raw` string is deliberately the one the author wrote: a message naming this
    predicate should name what is in the file, not a rendering with somebody's subject in it."""
    return Expr(root=_fold(expr.root, claims), raw=expr.raw)


def _fold(node: Any, claims: Mapping[str, Any]) -> Any:
    if isinstance(node, Unary) and node.op == "!":
        return Unary("!", _fold(node.operand, claims))
    if isinstance(node, Binary) and node.op in CONNECTIVES:
        return Binary(node.op, _fold(node.left, claims), _fold(node.right, claims))
    if isinstance(node, Binary) and node.op in COMPARISONS:
        left, right = _substituted(node.left, claims), _substituted(node.right, claims)
        if left is None or right is None:
            return UNDECIDABLE
        return Binary(node.op, left, right)
    return node  # pragma: no cover - check() refused every other node at bind


def _substituted(node: Any, claims: Mapping[str, Any]) -> Any | None:
    """One operand with the caller folded in, or None when this caller cannot decide it."""
    if isinstance(node, Ref) and len(node.path) == 2 and node.path[0] == "principal":
        name = node.path[1]
        return Literal(claims[name]) if name in claims else None
    return node


def guard_truth(expr: Expr, claims: Mapping[str, Any]) -> Truth:
    """What a policy's `when:` says about this caller: true, false, or undecided.

    Three answers because a guard has the same three, for the same reason: a claim this token does
    not carry is not a claim that is false. `governance.PolicyProgram.select` applies the policy on
    **true or undecided**, which is the fail-closed direction — see there for why the answer differs
    from decision 2's *refuse*, and why both are the same rule read twice.

    Kleene rather than §5's short-circuit, for `truth()`'s reason: nothing raises out of here, so
    short-circuiting could only make the answer depend on the order the author wrote the operands
    in."""
    return _truth(expr.root, {}, claims)


# ---- the read plane ------------------------------------------------------------


def lower(expr: Expr, obj: ObjectType, alias: str) -> Predicate:
    """The predicate as `ir` nodes, against one table alias.

    Only ever called after `check()` has passed at bind time, so an unexpected node here is a bug
    in this module rather than a bad policy, and says so."""
    return _lower(expr.root, obj, alias)


def _lower(node: Any, obj: ObjectType, alias: str) -> Predicate:
    if isinstance(node, Unary) and node.op == "!":
        return Not(_lower(node.operand, obj, alias))
    if isinstance(node, Binary):
        if node.op == "&&":
            return And(_lower(node.left, obj, alias), _lower(node.right, obj, alias))
        if node.op == "||":
            return Or(_lower(node.left, obj, alias), _lower(node.right, obj, alias))
        if node.op in COMPARISONS:
            return Compare(
                op=node.op,
                left=_operand(node.left, obj, alias),
                right=_operand(node.right, obj, alias),
            )
    raise AssertionError(  # pragma: no cover - check() refused every other node at bind
        f"{node!r} is not lowerable and check() should have refused it"
    )


def _operand(node: Any, obj: ObjectType, alias: str) -> Operand:
    if isinstance(node, Literal):
        return Const(node.value)
    if isinstance(node, Ref):
        return ColumnRef(alias=alias, column=obj.properties[node.path[1]].column)
    raise AssertionError(  # pragma: no cover - check() refused every other operand at bind
        f"{node!r} is not an operand and check() should have refused it"
    )


# ---- the write plane -----------------------------------------------------------


def truth(expr: Expr, row: Mapping[str, Any]) -> Truth:
    """What this predicate says about this row: true, false, or undecided.

    Public because the three answers *are* the design — a function that could only report two of
    them would be the thing this module argues against — and because the differential test needs to
    name the middle one.

    `row` is keyed by **property** name and holds every declared property, masked ones included:
    the policy *is* the deployment, so withholding from it makes no sense, and a predicate may
    legitimately filter on a property it also masks."""
    return _truth(expr.root, row)


def admits(expr: Expr, row: Mapping[str, Any]) -> bool:
    """Whether this row is one the deployment shows.

    Admitted only on true — an undecided predicate is a row nobody decided about, and a governance
    filter that cannot decide must not admit."""
    return truth(expr, row) is True


def _truth(node: Any, row: Mapping[str, Any], claims: Mapping[str, Any] | None = None) -> Truth:
    if isinstance(node, Unary) and node.op == "!":
        inner = _truth(node.operand, row, claims)
        return UNDECIDED if inner is UNDECIDED else not inner

    if isinstance(node, Binary) and node.op in ("&&", "||"):
        # Kleene, and deliberately **not** §5's short-circuit. §5 stops at the left operand so that
        # `object.ltv != null && object.ltv > 100` is writable at all — the right side would raise.
        # Here nothing raises: an unevaluable leaf is undecided, so the only thing short-circuiting
        # could still do is make the answer depend on the order the author wrote the operands in,
        # which is precisely what SQL does not do. `false && undecided` is false and
        # `true || undecided` is true on both planes because both sides are consulted.
        decides = node.op == "||"
        left = _truth(node.left, row, claims)
        if left is decides:
            return decides
        right = _truth(node.right, row, claims)
        if right is decides:
            return decides
        if left is UNDECIDED or right is UNDECIDED:
            return UNDECIDED
        return not decides

    # A leaf: §5's own evaluator over §5's own value domain, and its refusal to answer *is* this
    # language's undecided. Nothing about `==`, `<` or null means anything different here.
    try:
        value = evaluate_node(node, Scope(parameters={}, object_row=row, principal_claims=claims))
    except EvalError:
        return UNDECIDED
    if not isinstance(value, bool):  # pragma: no cover - check() refuses a non-boolean leaf
        raise AssertionError(f"{node!r} did not evaluate to a boolean")
    return value


# ---- the whole grammar, accounted for -------------------------------------------

GRAMMAR = BINARY_OPS | UNARY_OPS | {f"{name}()" for name in FUNCTIONS}
"""Every operator and function §5 has. `test_predicate.py` asserts `LOWERABLE` and `NOT_LOWERABLE`
partition it exactly, so a node added to the language has to be declared as one or the other rather
than arriving as the third kind Loom has been bitten by: accepted, unenforced, and silent."""
