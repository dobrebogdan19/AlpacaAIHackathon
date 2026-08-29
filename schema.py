"""Strategy grammar as validated data — never code.

A strategy in this system is a Pydantic structure, not a Python snippet. The
LLM that proposes strategies emits JSON conforming to the closed vocabulary
below; anything that fails validation is rejected and regenerated. See D3 in
DECISIONS.md: this converts the riskiest component (free-form model output)
into the most controlled one and removes an arbitrary-code-execution surface.
The replay engine (engine.py) is the *only* place that turns this data into
behaviour, and it can only compute the indicators named in `IndicatorName`.

Closed vocabulary (must match CLAUDE.md):
  indicators : SMA, EMA, RSI, ATR, MOMENTUM, VOLUME_AVG
  operators  : >, <, CROSSES_ABOVE, CROSSES_BELOW
  composition: a single condition, or exactly two joined by AND / OR

Adding an indicator means: extend `IndicatorName`, implement it in engine.py,
add a test. Never let a strategy reference something the engine cannot compute.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

MIN_PERIOD = 2
MAX_PERIOD = 250


class IndicatorName(str, Enum):
    SMA = "SMA"
    EMA = "EMA"
    RSI = "RSI"
    ATR = "ATR"
    MOMENTUM = "MOMENTUM"
    VOLUME_AVG = "VOLUME_AVG"


class Operator(str, Enum):
    GT = ">"
    LT = "<"
    CROSSES_ABOVE = "CROSSES_ABOVE"
    CROSSES_BELOW = "CROSSES_BELOW"


class Join(str, Enum):
    AND = "AND"
    OR = "OR"


class IndicatorRef(BaseModel):
    """A single indicator evaluation, e.g. SMA over a 10-bar window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    indicator: IndicatorName
    period: int = Field(
        ...,
        ge=MIN_PERIOD,
        le=MAX_PERIOD,
        description=f"lookback window in bars, {MIN_PERIOD}..{MAX_PERIOD}",
    )


class Condition(BaseModel):
    """`left <op> right`, where right is another indicator or a numeric constant.

    For `>` / `<` the comparison is on the current-bar values. For
    `CROSSES_ABOVE` / `CROSSES_BELOW` the previous-bar and current-bar values
    are both consulted (see engine.py).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    left: IndicatorRef
    operator: Operator
    right: Union[IndicatorRef, float]


class Rule(BaseModel):
    """One condition, or exactly two joined by AND / OR. No deeper nesting."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conditions: list[Condition] = Field(..., min_length=1, max_length=2)
    join: Optional[Join] = None

    @model_validator(mode="after")
    def _check_join(self) -> "Rule":
        if len(self.conditions) == 2 and self.join is None:
            raise ValueError("a two-condition rule must specify join = AND or OR")
        if len(self.conditions) == 1 and self.join is not None:
            raise ValueError("a single-condition rule must not specify a join")
        return self


class Strategy(BaseModel):
    """A named, single-symbol, long-only strategy: enter on `entry`, exit on `exit`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=1)
    entry: Rule
    exit: Rule
    rationale: str = Field(..., min_length=1, description="why this strategy might work")

    @model_validator(mode="after")
    def _entry_exit_differ(self) -> "Strategy":
        if self.entry == self.exit:
            raise ValueError("entry and exit rules are identical — the strategy would never hold a position")
        return self
