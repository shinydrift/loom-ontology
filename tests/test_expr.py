import pytest

from loom.expr import Binary, Call, ExprError, Literal, Ref, Unary, parse


def test_parses_comparison():
    e = parse("newTier != object.tier")
    assert isinstance(e.root, Binary) and e.root.op == "!="
    refs = {".".join(r.path) for r in e.refs()}
    assert refs == {"newTier", "object.tier"}


def test_strips_template_braces():
    e = parse("{{ customer }}")
    assert isinstance(e.root, Ref) and e.root.path == ("customer",)


def test_precedence_and_associativity():
    e = parse("1 + 2 * 3")
    # multiplication binds tighter -> 1 + (2 * 3)
    assert isinstance(e.root, Binary) and e.root.op == "+"
    assert isinstance(e.root.right, Binary) and e.root.right.op == "*"


def test_boolean_and_unary():
    e = parse("!(a && b) || c")
    assert isinstance(e.root, Binary) and e.root.op == "||"
    assert isinstance(e.root.left, Unary) and e.root.left.op == "!"


def test_function_call_and_literals():
    e = parse("coalesce(a, 'x', 3)")
    assert isinstance(e.root, Call) and e.root.name == "coalesce"
    assert len(e.root.args) == 3
    assert isinstance(e.root.args[1], Literal) and e.root.args[1].value == "x"


def test_keyword_literals():
    assert parse("true").root == Literal(True)
    assert parse("null").root == Literal(None)


@pytest.mark.parametrize("bad", ["", "1 +", "(a", "a b", "'unterminated", "1 @ 2"])
def test_malformed_raises(bad):
    with pytest.raises(ExprError):
        parse(bad)
