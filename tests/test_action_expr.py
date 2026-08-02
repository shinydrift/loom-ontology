"""The expression evaluator — §5 run rather than parsed.

Two things are asserted here and nowhere else: the **value domain** (a decimal read out of a row
must still be a decimal after a comparison and after arithmetic — the read path already promised
that, and a rule that quietly widened it to a float would break the promise from the other side)
and the **null semantics**, which are a deliberate departure from SQL and therefore the thing most
likely to be "fixed" by someone who assumed three-valued logic.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from loom.action import EvalError, Scope, evaluate
from loom.expr import ExprError
from loom.expr import parse as parse_expr


def run(source: str, **names):
    """Evaluate an expression with bare names as parameters and `object.*` from `_row`."""
    row = names.pop("_row", None)
    return evaluate(parse_expr(source), Scope(parameters=names, object_row=row))


# ---- one language, no templating -----------------------------------------------


def test_braces_and_bare_text_are_the_same_grammar():
    """`{{ customer }}` in an effect and `newTier != object.tier` in a rule parse to the same AST
    shapes. There is no templating pass — the braces are gone by the time anything runs."""
    assert parse_expr("{{ customer }}").root == parse_expr("customer").root
    assert run("{{ newTier }}", newTier="gold") == "gold"


def test_an_effect_value_may_be_a_whole_expression_not_just_a_reference():
    assert run("upper(tier) + '-' + region", tier="gold", region="emea") == "GOLD-emea"
    assert isinstance(run("now()"), datetime)


def test_a_template_is_a_parse_error_that_says_what_to_do_instead():
    with pytest.raises(ExprError) as e:
        parse_expr("tier-{{ newTier }}")
    assert "no string interpolation" in str(e.value)
    assert "'+'" in str(e.value)


def test_braces_may_only_wrap_the_whole_expression():
    with pytest.raises(ExprError):
        parse_expr("{{ a }} && {{ b }}")


# ---- the value domain ----------------------------------------------------------


def test_a_decimal_stays_a_decimal_through_comparison_and_arithmetic():
    """The whole reason a spec writes `decimal(12,2)` is that the value must not pass through a
    float. An evaluator that widened on the way to a comparison would undo that silently."""
    total = Decimal("1299.99")
    assert run("object.total > 1000", _row={"total": total}) is True
    assert run("object.total == 1299.99", _row={"total": total}) is False  # the literal is a float
    assert run("object.total == total", total=total, _row={"total": total}) is True
    doubled = run("object.total * 2", _row={"total": total})
    assert doubled == Decimal("2599.98") and isinstance(doubled, Decimal)


def test_mixing_a_decimal_with_a_float_is_refused_by_name():
    """Python raises on `Decimal * float`. Both ways out lose: widening drops the precision the
    spec asked for, narrowing invents digits. So it refuses and names both sides."""
    with pytest.raises(EvalError) as e:
        run("object.total * 1.1", _row={"total": Decimal("10.00")})
    assert "decimal" in str(e.value) and "double" in str(e.value)
    # An integer multiplier is exact, so it is fine.
    assert run("object.total * 3", _row={"total": Decimal("10.00")}) == Decimal("30.00")


def test_numbers_compare_across_their_python_types():
    assert run("object.ltv == 1", _row={"ltv": 1.0}) is True
    assert run("n == 1", n=Decimal("1.00")) is True


def test_a_boolean_is_not_a_number():
    """`True == 1` is a Python accident, not a Loom semantic."""
    assert run("flag == 1", flag=True) is False
    with pytest.raises(EvalError):
        run("flag > 0", flag=True)


def test_dates_and_timestamps_compare_on_one_axis():
    """`datetime` is a `date` subclass, so a plain `==` between them is silently False forever."""
    assert run("object.placedAt == d", d=date(2026, 1, 4),
               _row={"placedAt": datetime(2026, 1, 4, tzinfo=UTC)}) is True
    assert run("object.placedAt < d", d=date(2026, 2, 1),
               _row={"placedAt": datetime(2026, 1, 4, tzinfo=UTC)}) is True


# ---- null ----------------------------------------------------------------------


def test_null_is_a_value_you_can_test():
    """Deliberately *not* SQL's three-valued logic. A precondition is meant to be a decision, and
    an "unknown" one would force the runtime to refuse — making null a landmine in every rule
    written about a nullable property."""
    assert run("object.tier != 'gold'", _row={"tier": None}) is True
    assert run("object.tier == null", _row={"tier": None}) is True
    assert run("object.tier == 'gold'", _row={"tier": None}) is False
    assert run("null == null") is True


@pytest.mark.parametrize("source", ["object.ltv > 1", "object.ltv <= 1", "object.ltv + 1", "-object.ltv"])
def test_null_cannot_be_ordered_or_computed_with(source):
    with pytest.raises(EvalError) as e:
        run(source, _row={"ltv": None})
    assert "null" in str(e.value)


def test_coalesce_is_the_way_out_and_tolerates_nulls():
    assert run("coalesce(object.ltv, 0) > 1", _row={"ltv": None}) is False
    assert run("coalesce(object.ltv, 0) > 1", _row={"ltv": 5.0}) is True
    assert run("coalesce(a, b)", a=None, b=None) is None


def test_boolean_operators_short_circuit_which_is_what_makes_strict_nulls_livable():
    """`object.ltv != null && object.ltv > 100` is the idiom. It only works if the right side is
    never evaluated once the left has decided."""
    assert run("object.ltv != null && object.ltv > 100", _row={"ltv": None}) is False
    assert run("object.ltv != null && object.ltv > 100", _row={"ltv": 500.0}) is True
    assert run("object.ltv == null || object.ltv > 100", _row={"ltv": None}) is True


def test_a_non_boolean_in_a_boolean_operator_is_an_error_not_a_truthiness_test():
    with pytest.raises(EvalError):
        run("object.tier && true", _row={"tier": "gold"})
    with pytest.raises(EvalError):
        run("!object.tier", _row={"tier": "gold"})


# ---- references and functions --------------------------------------------------


def test_a_property_the_row_has_no_value_for_is_null_but_one_never_read_is_an_error():
    """Two different things that both look like an absent value: the lake contradicting the spec is
    not the same as this row simply holding nothing there."""
    assert run("object.ltv == null", _row={"ltv": None}) is True
    with pytest.raises(EvalError) as e:
        run("object.ltv == null", _row={"tier": "gold"})
    assert "was not read from the row" in str(e.value)


def test_object_references_need_a_current_object():
    with pytest.raises(EvalError) as e:
        run("object.tier == 'gold'")
    assert "no current object" in str(e.value)


def test_string_functions_refuse_a_non_string():
    assert run("lower(t) + upper(t)", t="Ab") == "abAB"
    assert run("len(t) > 1", t="Ab") is True
    with pytest.raises(EvalError):
        run("lower(object.ltv)", _row={"ltv": 3.0})


def test_string_concatenation_needs_two_strings():
    """`'x' + 1` has two plausible meanings and no obvious one, so it has none."""
    with pytest.raises(EvalError):
        run("t + 1", t="x")


def test_division_by_zero_is_an_error_not_an_infinity():
    with pytest.raises(EvalError):
        run("a / b", a=1, b=0)
