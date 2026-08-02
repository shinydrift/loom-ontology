"""The expression mini-language, evaluated — §5, run rather than parsed or type-checked.

`expr.py` produces the AST and the validator infers its type offline. This is the third consumer of
those same nodes, and the first that needs actual values: `newTier != object.tier` cannot be
answered without a row.

**It sat under `action/` while it had one consumer, and it moved out when it grew a second.** M5's
row predicates are evaluated over a row on the write plane by the same rules — a governance
predicate is not an action, and a module every plane depends on cannot live inside the package of
whichever plane needed it first. `predicate.py` reads the leaves of a policy through
`evaluate_node`, which is what keeps "what does `==` mean between a `Decimal` and an `int`" one
definition rather than two.

**The value domain is the read path's value domain.** `Decimal` for decimal (never a float — that
is the entire reason a spec writes `decimal(12,2)`), tz-aware `datetime` for timestamp, `date`,
and plain `str` / `int` / `float` / `bool` / `None` elsewhere. It is the domain `model.coerce_value`
produces and `mcp.registry.json_safe` renders, so a value read out of a row, compared in a rule and
written back is the same value throughout.

**Null is a value, not an unknown.** `null != 'gold'` is true; `null == null` is true. This is
deliberately *not* SQL's three-valued logic. The language never reaches SQL — it is evaluated in
Python, in process, over one already-fetched row — and three-valued logic would let a precondition
come back "unknown", at which point the runtime has to decide what an unknown precondition means.
The only safe answer there is to refuse, which would make `null` a landmine in every rule an author
writes about a nullable property. A precondition is meant to be a decision.

But null is a value you can **test and coalesce**, not one you can **order or compute with**:
`< <= > >=`, arithmetic, `!` and the boolean operators all raise on null rather than inventing an
answer. `&&` and `||` short-circuit, which is what makes that strictness livable — the idiom is
`object.ltv != null && object.ltv > 100`, and `coalesce` is in the function allow-list for exactly
this reason.

Everything it raises is an `EvalError`, which the runtime turns into an `EXPRESSION_ERROR` failure.
Nothing here decides policy; it only decides values.

**One qualification the second reader added, and it is a qualification rather than an exception.**
A governance row predicate is this language over this value domain, and it treats an `EvalError`
the way it treats a false: the row is not admitted. Nothing about `==`, `<` or `null` means
anything different there — what differs is the *disposition of "cannot decide"*, and it differs
because the two sites differ in whether there is anybody to tell. A validation rule has a caller
who asked for the run and can fix the input, so `expression_error` is a service to them. A policy
predicate has no such caller: per row there is no channel at all, and per call, "this row exists
but I could not decide about it" is exactly the existence oracle §6.1 refuses. See `predicate.py`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from .expr import Binary, Call, Expr, Literal, Ref, Unary

_NUMERIC = (int, float, Decimal)
_ORDERED = {"<", "<=", ">", ">="}
_ARITHMETIC = {"+", "-", "*", "/"}


class EvalError(RuntimeError):
    """An expression that cannot be given a value: null where a value is required, a type the
    operator has no meaning for, division by zero."""


@dataclass(frozen=True)
class Scope:
    """What names resolve to during one action run.

    `object_row` is the target object's *current* property values, keyed by property name — None
    for `create`, which has no prior object, exactly as the validator refuses `object.*` there. A
    property the row holds no value for is present and null; a property missing from the mapping
    entirely is a bug above this line, and says so rather than evaluating to null."""

    parameters: Mapping[str, Any]
    object_row: Mapping[str, Any] | None = None


def evaluate(expr: Expr, scope: Scope) -> Any:
    """Evaluate a parsed expression to a Python value. Raises EvalError."""
    return evaluate_node(expr.root, scope)


def evaluate_node(node: Any, scope: Scope) -> Any:
    """The same thing, for a caller holding a sub-tree rather than a whole `Expr`.

    `predicate.py` walks the connectives itself — a row predicate's `&&` obeys SQL's rules, not
    §5's short-circuit — and hands each leaf back here. Splitting the entry point rather than
    rebuilding an `Expr` around every leaf keeps one evaluator for the values and one raw string
    per expression, which is what error messages are written against."""
    return _eval(node, scope)


def _eval(node: Any, scope: Scope) -> Any:
    if isinstance(node, Literal):
        return node.value
    if isinstance(node, Ref):
        return _ref(node, scope)
    if isinstance(node, Call):
        return _call(node, scope)
    if isinstance(node, Unary):
        return _unary(node, scope)
    if isinstance(node, Binary):
        return _binary(node, scope)
    raise EvalError(f"cannot evaluate {node!r}")  # pragma: no cover - the parser emits no others


# ---- references ----------------------------------------------------------------


def _ref(ref: Ref, scope: Scope) -> Any:
    if len(ref.path) == 1:
        name = ref.path[0]
        if name in scope.parameters:
            return scope.parameters[name]
        # The validator rejects a free variable at load, so reaching here means the ontology and
        # the binding disagree — worth saying loudly rather than resolving to null.
        raise EvalError(f"no parameter '{name}' is bound")
    if len(ref.path) == 2 and ref.path[0] == "object":
        if scope.object_row is None:
            raise EvalError(f"'object.{ref.path[1]}' has no value here — there is no current object")
        if ref.path[1] not in scope.object_row:
            raise EvalError(f"'object.{ref.path[1]}' was not read from the row")
        return scope.object_row[ref.path[1]]
    raise EvalError(f"unsupported reference '{'.'.join(ref.path)}'")  # pragma: no cover


# ---- operators -----------------------------------------------------------------


def _unary(node: Unary, scope: Scope) -> Any:
    value = _eval(node.operand, scope)
    if node.op == "!":
        if not isinstance(value, bool):
            raise EvalError(f"'!' needs a boolean, got {_name(value)}")
        return not value
    if value is None or isinstance(value, bool) or not isinstance(value, _NUMERIC):
        raise EvalError(f"unary '-' needs a number, got {_name(value)}")
    return -value


def _binary(node: Binary, scope: Scope) -> Any:
    # Short-circuit before touching the right side: `object.ltv != null && object.ltv > 100` is the
    # idiom that makes strict null handling usable, and it only works if the right side is never
    # evaluated when the left already decided.
    if node.op in ("&&", "||"):
        left = _boolean(_eval(node.left, scope), node.op)
        if (node.op == "&&") != left:  # && with false, || with true
            return left
        return _boolean(_eval(node.right, scope), node.op)

    left, right = _eval(node.left, scope), _eval(node.right, scope)

    if node.op in ("==", "!="):
        equal = _equal(left, right)
        return equal if node.op == "==" else not equal
    if node.op in _ORDERED:
        return _ordered(node.op, left, right)
    if node.op in _ARITHMETIC:
        return _arithmetic(node.op, left, right)
    raise EvalError(f"unsupported operator '{node.op}'")  # pragma: no cover


def _boolean(value: Any, op: str) -> bool:
    if not isinstance(value, bool):
        raise EvalError(f"'{op}' needs booleans, got {_name(value)}")
    return value


def _equal(left: Any, right: Any) -> bool:
    """Null is a value here, and only here. Two nulls are equal; a null and anything else are not.

    Numbers compare across their Python types, so a `Decimal('1.00')` read out of a row equals the
    literal `1` an author wrote — the alternative is a rule that is false for a reason the YAML
    gives no hint of. Dates and timestamps compare on one axis for the same reason: `datetime` is a
    `date` subclass, so a plain `==` between them is silently False forever. Enum values are strings
    and compare as strings, which is what `PropType.comparable_to` already says offline."""
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        # True == 1 is a Python accident, not a Loom semantic.
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, _NUMERIC) and isinstance(right, _NUMERIC):
        return left == right
    if isinstance(left, (date, datetime)) and isinstance(right, (date, datetime)):
        return _instant(left) == _instant(right)
    return bool(left == right)


def _ordered(op: str, left: Any, right: Any) -> bool:
    _require_value(op, left, right)
    if isinstance(left, bool) or isinstance(right, bool):
        raise EvalError(f"'{op}' has no meaning for a boolean")
    if isinstance(left, _NUMERIC) and isinstance(right, _NUMERIC):
        a, b = left, right
    elif isinstance(left, (date, datetime)) and isinstance(right, (date, datetime)):
        a, b = _instant(left), _instant(right)
    elif isinstance(left, str) and isinstance(right, str):
        a, b = left, right
    else:
        raise EvalError(f"'{op}' cannot compare {_name(left)} with {_name(right)}")
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


def _arithmetic(op: str, left: Any, right: Any) -> Any:
    _require_value(op, left, right)
    if op == "+" and (isinstance(left, str) or isinstance(right, str)):
        if not (isinstance(left, str) and isinstance(right, str)):
            raise EvalError(f"'+' concatenates two strings; got {_name(left)} and {_name(right)}")
        return left + right
    for side in (left, right):
        if isinstance(side, bool) or not isinstance(side, _NUMERIC):
            raise EvalError(f"'{op}' needs numbers, got {_name(left)} and {_name(right)}")
    if {Decimal, float} <= {type(left), type(right)}:
        # Python raises on Decimal * float, and the two ways out are both wrong: widening the
        # Decimal to a float throws away the precision the spec asked for, and narrowing the float
        # to a Decimal invents digits. Refusing names both operands so the author can pick.
        raise EvalError(
            f"'{op}' will not mix a decimal with a float ({_name(left)} and {_name(right)}) — "
            f"one of them would have to lose precision. Write the literal as a decimal string"
        )
    if op == "/" and right == 0:
        raise EvalError("division by zero")
    # '/' is true division and yields a fraction even for two integers. The validator's offline
    # inference is optimistic about that; the strict half is `coerce_value`, which refuses to store
    # a fractional number in an int property rather than truncating it.
    return {"+": lambda: left + right, "-": lambda: left - right,
            "*": lambda: left * right, "/": lambda: left / right}[op]()


def _require_value(op: str, *values: Any) -> None:
    if any(v is None for v in values):
        raise EvalError(
            f"'{op}' has no meaning for null — null is a value you can test with '==' or replace "
            f"with coalesce(), not one you can order or compute with"
        )


# ---- functions -----------------------------------------------------------------


def _call(call: Call, scope: Scope) -> Any:
    if call.name == "coalesce":
        # The designed way out of the strict null rules above, so it is the one function that
        # evaluates its arguments lazily and tolerates a null.
        for arg in call.args:
            value = _eval(arg, scope)
            if value is not None:
                return value
        return None
    args = [_eval(a, scope) for a in call.args]
    if call.name == "now":
        return datetime.now(UTC)
    if call.name in ("lower", "upper", "len"):
        (value,) = args
        if not isinstance(value, str):
            raise EvalError(f"{call.name}() needs a string, got {_name(value)}")
        return {"lower": value.lower, "upper": value.upper, "len": lambda: len(value)}[call.name]()
    raise EvalError(f"unknown function '{call.name}()'")  # pragma: no cover - validator rejects it


# ---- helpers -------------------------------------------------------------------


def _instant(value: date | datetime) -> datetime:
    """Dates and timestamps compare on one axis, with a bare date read as midnight UTC — the same
    convention `coerce_value` uses when it reads a naive timestamp as UTC."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _name(value: Any) -> str:
    """A value's type, in the spec's vocabulary where there is one — an author who wrote `decimal`
    should not have to recognize `Decimal` in an error about it."""
    if value is None:
        return "null"
    return {
        bool: "boolean", int: "int", float: "double", Decimal: "decimal",
        str: "string", datetime: "timestamp", date: "date",
    }.get(type(value), type(value).__name__)
