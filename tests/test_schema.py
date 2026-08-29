"""Schema validation tests — T1.1 acceptance: an invalid strategy is rejected
with a clear error."""

import pytest
from pydantic import ValidationError

from schema import Condition, IndicatorName, IndicatorRef, Join, Operator, Rule, Strategy


def _cond(indicator="SMA", period=10, op=Operator.CROSSES_ABOVE, right_period=30):
    return Condition(
        left=IndicatorRef(indicator=indicator, period=period),
        operator=op,
        right=IndicatorRef(indicator=IndicatorName.SMA, period=right_period),
    )


def _strategy(entry=None, exit=None):
    return Strategy(
        name="s",
        symbol="AAPL",
        entry=entry or Rule(conditions=[_cond(op=Operator.CROSSES_ABOVE)]),
        exit=exit or Rule(conditions=[_cond(op=Operator.CROSSES_BELOW)]),
        rationale="because",
    )


def test_valid_strategy_builds():
    s = _strategy()
    assert s.symbol == "AAPL"
    assert s.entry.conditions[0].operator is Operator.CROSSES_ABOVE


def test_unknown_indicator_rejected():
    with pytest.raises(ValidationError):
        IndicatorRef(indicator="MACD", period=10)


@pytest.mark.parametrize("bad_period", [1, 0, -5, 251, 1000])
def test_nonsensical_period_rejected(bad_period):
    with pytest.raises(ValidationError):
        IndicatorRef(indicator=IndicatorName.SMA, period=bad_period)


def test_identical_entry_and_exit_rejected():
    same = Rule(conditions=[_cond(op=Operator.CROSSES_ABOVE)])
    with pytest.raises(ValidationError, match="identical"):
        _strategy(entry=same, exit=same)


def test_two_condition_rule_requires_join():
    with pytest.raises(ValidationError, match="join"):
        Rule(conditions=[_cond(op=Operator.CROSSES_ABOVE), _cond(op=Operator.GT)])


def test_single_condition_rule_rejects_join():
    with pytest.raises(ValidationError, match="join"):
        Rule(conditions=[_cond()], join=Join.AND)


def test_three_conditions_rejected():
    with pytest.raises(ValidationError):
        Rule(
            conditions=[_cond(), _cond(op=Operator.GT), _cond(op=Operator.LT)],
            join=Join.AND,
        )
