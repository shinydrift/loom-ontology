"""The expression mini-language — §5 of the spec grammar.

Deliberately tiny so it stays portable across query engines and safe to evaluate: references
(`param`, `object.prop`), literals, comparison/boolean/arithmetic operators, and a fixed
function allow-list. No loops, lambdas, assignment, or arbitrary code.

This module only *parses and shape-checks* expressions into an AST. Binding references to real
parameters/properties, evaluating the AST against a bound row, and later lowering it to engine SQL
happen in the validator, the action runtime and the resolver respectively — but they build on the
`refs()` / `calls()` the AST exposes here.

The `{{ … }}` an effect writes its values in is *not* a second grammar. `parse()` strips it, so by
the time anything runs there is only ever an `Expr`. See `parse()`.
"""

from __future__ import annotations

from dataclasses import dataclass

# name -> (min_args, max_args); max_args=None means variadic.
FUNCTIONS: dict[str, tuple[int, int | None]] = {
    "now": (0, 0),
    "lower": (1, 1),
    "upper": (1, 1),
    "len": (1, 1),
    "coalesce": (1, None),
}

_BINARY = {"||", "&&", "==", "!=", "<", "<=", ">", ">=", "+", "-", "*", "/"}
_KEYWORD_LITERALS = {"true": True, "false": False, "null": None}


class ExprError(ValueError):
    """Raised on a malformed expression. Callers wrap it into a SpecError with location."""


# ---- AST nodes -----------------------------------------------------------------


@dataclass(frozen=True)
class Literal:
    value: object


@dataclass(frozen=True)
class Ref:
    path: tuple[str, ...]  # ("customer",) or ("object", "tier")


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple[object, ...]


@dataclass(frozen=True)
class Unary:
    op: str
    operand: object


@dataclass(frozen=True)
class Binary:
    op: str
    left: object
    right: object


@dataclass(frozen=True)
class Expr:
    """A parsed expression plus the raw source, for good error messages."""

    root: object
    raw: str

    def refs(self) -> list[Ref]:
        out: list[Ref] = []
        _walk(self.root, lambda n: out.append(n) if isinstance(n, Ref) else None)
        return out

    def calls(self) -> list[Call]:
        out: list[Call] = []
        _walk(self.root, lambda n: out.append(n) if isinstance(n, Call) else None)
        return out


def _walk(node: object, visit) -> None:
    visit(node)
    if isinstance(node, Unary):
        _walk(node.operand, visit)
    elif isinstance(node, Binary):
        _walk(node.left, visit)
        _walk(node.right, visit)
    elif isinstance(node, Call):
        for a in node.args:
            _walk(a, visit)


# ---- tokenizer -----------------------------------------------------------------

_PUNCT = ["||", "&&", "==", "!=", "<=", ">=", "(", ")", ",", ".", "!", "<", ">", "+", "-", "*", "/"]


def _tokenize(s: str) -> list[tuple[str, object]]:
    toks: list[tuple[str, object]] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c in "'\"":
            j = i + 1
            buf = []
            while j < n and s[j] != c:
                buf.append(s[j])
                j += 1
            if j >= n:
                raise ExprError(f"unterminated string in {s!r}")
            toks.append(("str", "".join(buf)))
            i = j + 1
            continue
        if c.isdigit() or (c == "." and i + 1 < n and s[i + 1].isdigit()):
            j = i
            while j < n and (s[j].isdigit() or s[j] == "."):
                j += 1
            num = s[i:j]
            toks.append(("num", float(num) if "." in num else int(num)))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            toks.append(("ident", s[i:j]))
            i = j
            continue
        for p in _PUNCT:
            if s.startswith(p, i):
                toks.append(("op", p))
                i += len(p)
                break
        else:
            raise ExprError(f"unexpected character {c!r} in {s!r}")
    toks.append(("end", None))
    return toks


# ---- Pratt parser --------------------------------------------------------------

# Higher binds tighter.
_PRECEDENCE = {
    "||": 1, "&&": 2,
    "==": 3, "!=": 3, "<": 3, "<=": 3, ">": 3, ">=": 3,
    "+": 4, "-": 4, "*": 5, "/": 5,
}


class _Parser:
    def __init__(self, toks: list[tuple[str, object]], raw: str):
        self.toks = toks
        self.pos = 0
        self.raw = raw

    def peek(self) -> tuple[str, object]:
        return self.toks[self.pos]

    def next(self) -> tuple[str, object]:
        t = self.toks[self.pos]
        self.pos += 1
        return t

    def expect_op(self, op: str) -> None:
        k, v = self.next()
        if not (k == "op" and v == op):
            raise ExprError(f"expected {op!r} but found {v!r} in {self.raw!r}")

    def parse(self) -> object:
        node = self.parse_expr(0)
        if self.peek()[0] != "end":
            raise ExprError(f"trailing tokens in {self.raw!r}")
        return node

    def parse_expr(self, min_prec: int) -> object:
        left = self.parse_unary()
        while True:
            k, v = self.peek()
            if k == "op" and v in _PRECEDENCE and _PRECEDENCE[v] >= min_prec:
                self.next()
                right = self.parse_expr(_PRECEDENCE[v] + 1)  # left-associative
                left = Binary(v, left, right)
            else:
                return left

    def parse_unary(self) -> object:
        k, v = self.peek()
        if k == "op" and v in ("!", "-"):
            self.next()
            return Unary(v, self.parse_unary())
        return self.parse_primary()

    def parse_primary(self) -> object:
        k, v = self.next()
        if k == "num":
            return Literal(v)
        if k == "str":
            return Literal(v)
        if k == "op" and v == "(":
            node = self.parse_expr(0)
            self.expect_op(")")
            return node
        if k == "ident":
            if v in _KEYWORD_LITERALS:
                return Literal(_KEYWORD_LITERALS[v])
            nk, nv = self.peek()
            if nk == "op" and nv == "(":
                return self.parse_call(v)
            return self.parse_ref(v)
        raise ExprError(f"unexpected token {v!r} in {self.raw!r}")

    def parse_ref(self, first: str) -> Ref:
        path = [first]
        while self.peek() == ("op", "."):
            self.next()
            k, v = self.next()
            if k != "ident":
                raise ExprError(f"expected identifier after '.' in {self.raw!r}")
            path.append(v)
        return Ref(tuple(path))

    def parse_call(self, name: str) -> Call:
        self.expect_op("(")
        args: list[object] = []
        if self.peek() != ("op", ")"):
            args.append(self.parse_expr(0))
            while self.peek() == ("op", ","):
                self.next()
                args.append(self.parse_expr(0))
        self.expect_op(")")
        return Call(name, tuple(args))


def parse(text: str) -> Expr:
    """Parse an expression (with optional surrounding `{{ }}`) into an Expr AST.
    Raises ExprError on malformed input.

    **There is one language, and this is where the braces stop existing.** `{{ customer }}` in an
    effect and `newTier != object.tier` in a validation rule are the same grammar written two ways:
    the wrapper is optional punctuation, stripped here at load, so nothing downstream — evaluator,
    validator, or engine — ever sees a brace. What it is *not* is a template. `"tier-{{ x }}"` does
    not interpolate; string building is the expression language's own `+`."""
    raw = text.strip()
    inner = raw
    if inner.startswith("{{") and inner.endswith("}}"):
        inner = inner[2:-2].strip()
    if "{{" in inner or "}}" in inner:
        raise ExprError(
            f"{raw!r} looks like a template, and Loom has no string interpolation — '{{{{ }}}}' may "
            f"only wrap a whole expression. Use the expression language's '+' to build a string"
        )
    if not inner:
        raise ExprError("empty expression")
    root = _Parser(_tokenize(inner), raw).parse()
    return Expr(root=root, raw=raw)
