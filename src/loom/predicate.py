"""A governance row predicate — §5, restricted to what two evaluators can be made to agree on.

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
from typing import Any

from ._shape import suggest
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
    "now()": f"{_ENGINE_WOULD_COMPUTE_IT}. now() is the one refusal that is not about engines — it "
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


def check(expr: Expr, obj: ObjectType, objects: Mapping[str, ObjectType]) -> list[str]:
    """Every reason this expression cannot govern this object type, or an empty list.

    Every problem rather than the first, because `bind_policies` collects these alongside the mask
    refusals and an operator reconciling a policy file with a spec should learn the whole of what
    disagrees in one reading.

    Deliberately not `validator._ExprChecker`, which infers a type for the *whole* language and is
    written to be optimistic — it "returns None when the type is unknown, never a guess", so that a
    spec author is not told about a type Loom could not work out. A governance predicate needs the
    opposite posture: every node accounted for, nothing inferred generously, and the comparability
    of two operands actually checked rather than assumed. That is a stricter walk over a smaller
    grammar, not a second call into the same one. `objects` is only for the one thing it does
    borrow: an `objectRef` property travels as the referenced object's primary key, so it compares
    as that key's type — the resolution `_ExprChecker._resolve` already makes for a rule."""
    problems: list[str] = []
    _walk(expr.root, obj, objects, problems)
    # Deduplicated, order preserved: `lower(a) == lower(b)` offends twice for one reason, and a
    # refusal that says the same sentence twice reads like two different problems.
    problems = list(dict.fromkeys(problems))
    if not problems and not _reads_a_property(expr.root):
        problems.append(
            f"'{expr.raw}' names no property of '{obj.api_name}', so it is the same answer for "
            "every row — a predicate that admits everything reads like protection and is none, and "
            "one that admits nothing withholds the object type. Stop declaring it instead"
        )
    return problems


def _walk(node: Any, obj: ObjectType, objects: Mapping[str, ObjectType], problems: list[str]) -> None:
    """One pass that checks the subset and the types together, because they are one question:
    whether this node is something both planes can be made to answer identically.

    Every node it recurses into stands where a condition belongs — a comparison's own operands are
    `_compare`'s business — which is what lets it name a bare `object.tier` as *not a condition*
    rather than inferring a type for it and complaining about the type."""
    if isinstance(node, Unary) and node.op == "!":
        _walk(node.operand, obj, objects, problems)
        return

    if isinstance(node, Binary):
        if node.op in CONNECTIVES:
            _walk(node.left, obj, objects, problems)
            _walk(node.right, obj, objects, problems)
            return
        if node.op in COMPARISONS:
            _compare(node, obj, objects, problems)
            return

    if _refuse(node, problems):
        return

    # A bare reference or literal where a condition belongs.
    problems.append(
        f"'{_render(node)}' is not a condition — a row predicate is a comparison, or several "
        "joined by '&&', '||' and '!'"
    )


def _refuse(node: Any, problems: list[str]) -> bool:
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
    problems.append(f"'{_render(node)}' cannot be used in a row predicate: {why}")
    return True


def _compare(
    node: Binary, obj: ObjectType, objects: Mapping[str, ObjectType], problems: list[str]
) -> None:
    left = _operand_type(node.left, obj, objects, problems)
    right = _operand_type(node.right, obj, objects, problems)
    if left is _BAD or right is _BAD:
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
        problems.append(
            f"a row predicate compares '{left.kind}' with '{right.kind}', which are not comparable "
            "types — the same rule a validation rule's operands follow"
        )


class _Bad:
    """An operand already reported on. Distinct from `None`, which is the type of `null`."""


_BAD = _Bad()


def _operand_type(
    node: Any, obj: ObjectType, objects: Mapping[str, ObjectType], problems: list[str]
) -> PropType | None | _Bad:
    if isinstance(node, Literal):
        return PropType.of_literal(node.value)
    if isinstance(node, Ref):
        if len(node.path) == 2 and node.path[0] == "object":
            prop = obj.properties.get(node.path[1])
            if prop is None:
                known = ", ".join(obj.properties) or "none"
                hint = suggest(node.path[1], obj.properties) or f"known: {known}"
                problems.append(
                    f"'object.{node.path[1]}' is not a property of '{obj.api_name}' — {hint}"
                )
                return _BAD
            return _resolved(prop.type, objects)
        # A bare name is a *parameter* reference in §5 and a policy has no parameters. One language
        # keeps one meaning for each reference form rather than growing a second one here.
        problems.append(
            f"'{'.'.join(node.path)}' is not something a policy can reference — a bare name is a "
            f"parameter and a policy has none. Write 'object.{node.path[0]}' for a property of "
            f"'{obj.api_name}'"
        )
        return _BAD
    if not _refuse(node, problems):  # pragma: no cover - the parser emits no other operand shapes
        problems.append(f"'{_render(node)}' is neither a property nor a literal")
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
    if isinstance(node, Ref):
        return len(node.path) == 2 and node.path[0] == "object"
    if isinstance(node, Unary):
        return _reads_a_property(node.operand)
    if isinstance(node, Binary):
        return _reads_a_property(node.left) or _reads_a_property(node.right)
    return False


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


def _truth(node: Any, row: Mapping[str, Any]) -> Truth:
    if isinstance(node, Unary) and node.op == "!":
        inner = _truth(node.operand, row)
        return UNDECIDED if inner is UNDECIDED else not inner

    if isinstance(node, Binary) and node.op in ("&&", "||"):
        # Kleene, and deliberately **not** §5's short-circuit. §5 stops at the left operand so that
        # `object.ltv != null && object.ltv > 100` is writable at all — the right side would raise.
        # Here nothing raises: an unevaluable leaf is undecided, so the only thing short-circuiting
        # could still do is make the answer depend on the order the author wrote the operands in,
        # which is precisely what SQL does not do. `false && undecided` is false and
        # `true || undecided` is true on both planes because both sides are consulted.
        decides = node.op == "||"
        left = _truth(node.left, row)
        if left is decides:
            return decides
        right = _truth(node.right, row)
        if right is decides:
            return decides
        if left is UNDECIDED or right is UNDECIDED:
            return UNDECIDED
        return not decides

    # A leaf: §5's own evaluator over §5's own value domain, and its refusal to answer *is* this
    # language's undecided. Nothing about `==`, `<` or null means anything different here.
    try:
        value = evaluate_node(node, Scope(parameters={}, object_row=row))
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
