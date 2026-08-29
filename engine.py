"""The replay engine — a hand-written, lookahead-free backtest loop.

Ported from ``skeleton.py``'s ``backtest()`` with **behaviour parity** as the
first requirement (see tests/test_engine.py::test_regression). The only change
is generalisation: indicators are computed generically from the `Strategy`
grammar (schema.py) instead of being hardcoded to a fast/slow SMA pair.

Semantics preserved verbatim from skeleton.py and DECISIONS.md D1 / D11:

  * The signal for bar N is computed only from data known once bar N has
    closed (closes[0..N], and the bar-N-1 values for crossover detection).
  * The resulting trade executes at bar N+1's OPEN. The loop runs
    ``for n in range(len(bars) - 1)`` — a signal on the last bar can never be
    filled and is silently dropped.
  * Long only, one position, all-in, no leverage. Commission is assumed
    ZERO (stated explicitly; skeleton.py made the same assumption).
  * A position still open on the final bar was NOT closed by an exit signal.
    It is marked to market at the last close and reported as
    ``open_position`` + ``unrealized_pnl_pct``; it does NOT increment
    ``num_trades`` and is NOT counted in ``win_rate_pct``. ``total_return_pct``
    still includes its mark-to-market value.

No backtesting library is used (DECISIONS.md D2).

Only SMA is exercised by the regression oracle. EMA / RSI / ATR / MOMENTUM /
VOLUME_AVG are implemented to the grammar but are not covered by skeleton.py.
"""

from __future__ import annotations

import statistics
from typing import Optional

from schema import Condition, IndicatorName, IndicatorRef, Join, Operator, Rule, Strategy

STARTING_CASH = 10_000.0
COMMISSION_PER_TRADE = 0.0  # explicit: we assume zero commission (as skeleton.py did)


# --- indicators -----------------------------------------------------------
# Each returns the indicator value evaluated AT bar `idx` (inclusive), using
# only bars[0..idx]. Returns None when there is not enough history — callers
# treat None as "no signal", matching skeleton.py's `None not in (...)` guard.
# `idx` may be negative (e.g. -1 for the bar before the first); that yields
# None, exactly as skeleton.py's sma(closes, -1, w) did.


def _series(bars, field: str) -> list[float]:
    try:
        return [float(b[field]) for b in bars]
    except (KeyError, TypeError):
        raise ValueError(f"bars do not carry a '{field}' field required by this indicator")


def _sma(values, idx, period) -> Optional[float]:
    if idx + 1 < period:
        return None
    return statistics.fmean(values[idx + 1 - period : idx + 1])


def _ema(values, idx, period) -> Optional[float]:
    if idx + 1 < period:
        return None
    ema = statistics.fmean(values[:period])  # seed with the first `period` SMA
    mult = 2.0 / (period + 1)
    for i in range(period, idx + 1):
        ema = (values[i] - ema) * mult + ema
    return ema


def _rsi(closes, idx, period) -> Optional[float]:
    if idx + 1 < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, idx + 1)]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = statistics.fmean(gains[:period])
    avg_loss = statistics.fmean(losses[:period])
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _atr(bars, idx, period) -> Optional[float]:
    if idx + 1 < period + 1:
        return None
    highs, lows, closes = _series(bars, "high"), _series(bars, "low"), _series(bars, "close")
    trs = []
    for i in range(1, idx + 1):
        trs.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )
    atr = statistics.fmean(trs[:period])
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return atr


def _momentum(closes, idx, period) -> Optional[float]:
    if idx + 1 < period + 1:
        return None
    past = closes[idx - period]
    if past == 0:
        return None
    return (closes[idx] - past) / past * 100.0


def _indicator_value(ref: IndicatorRef, bars, idx: int) -> Optional[float]:
    name, period = ref.indicator, ref.period
    if name is IndicatorName.SMA:
        return _sma(_series(bars, "close"), idx, period)
    if name is IndicatorName.EMA:
        return _ema(_series(bars, "close"), idx, period)
    if name is IndicatorName.RSI:
        return _rsi(_series(bars, "close"), idx, period)
    if name is IndicatorName.ATR:
        return _atr(bars, idx, period)
    if name is IndicatorName.MOMENTUM:
        return _momentum(_series(bars, "close"), idx, period)
    if name is IndicatorName.VOLUME_AVG:
        return _sma(_series(bars, "volume"), idx, period)
    raise ValueError(f"engine cannot compute indicator {name!r}")  # pragma: no cover


# --- rule evaluation ----------------------------------------------------------


def _side_value(side, bars, idx) -> Optional[float]:
    if isinstance(side, IndicatorRef):
        return _indicator_value(side, bars, idx)
    return float(side)  # numeric constant — same at every bar


def _eval_condition(cond: Condition, bars, n: int) -> bool:
    """True iff `cond` holds at bar `n`. Missing indicator history -> False."""
    left_now = _side_value(cond.left, bars, n)
    right_now = _side_value(cond.right, bars, n)
    op = cond.operator

    if op is Operator.GT:
        if None in (left_now, right_now):
            return False
        return left_now > right_now
    if op is Operator.LT:
        if None in (left_now, right_now):
            return False
        return left_now < right_now

    # crossover operators also need the previous bar's values
    left_prev = _side_value(cond.left, bars, n - 1)
    right_prev = _side_value(cond.right, bars, n - 1)
    if None in (left_now, right_now, left_prev, right_prev):
        return False
    if op is Operator.CROSSES_ABOVE:
        return left_prev <= right_prev and left_now > right_now
    if op is Operator.CROSSES_BELOW:
        return left_prev >= right_prev and left_now < right_now
    raise ValueError(f"unknown operator {op!r}")  # pragma: no cover


def _eval_rule(rule: Rule, bars, n: int) -> bool:
    results = [_eval_condition(c, bars, n) for c in rule.conditions]
    if len(results) == 1:
        return results[0]
    if rule.join is Join.AND:
        return results[0] and results[1]
    return results[0] or results[1]  # Join.OR


# --- backtest ---------------------------------------------------------------


def run_backtest(strategy: Strategy, bars, *, start_index: int = 0) -> dict:
    """Replay `strategy` over `bars` (oldest first). Returns a metrics dict.

    `bars` is a list of dicts with at least ``open``/``close`` (and
    ``high``/``low``/``volume`` if the strategy uses ATR / VOLUME_AVG).

    ``start_index`` (default 0 — unchanged behaviour) begins the *trading loop*
    and the equity curve at ``bars[start_index]``, while indicators are still
    computed from the full history ``bars[0..n]``. This is how a shadow is
    tracked forward from its rejection date (Phase 4 / D34): the bars before
    ``start_index`` are warm-up only — needed so an SMA/EMA/RSI is defined on the
    first forward bar — and the strategy starts flat there and trades forward.
    D1 semantics are unchanged: a decision on bar n's close still fills at bar
    n+1's open, and a signal on the last bar is still dropped.
    """
    if len(bars) < 2:
        raise ValueError("need at least 2 bars to run a backtest")
    if not 0 <= start_index < len(bars) - 1:
        raise ValueError(
            f"start_index {start_index} out of range for {len(bars)} bars"
        )

    closes = [float(b["close"]) for b in bars]

    cash = STARTING_CASH
    shares = 0.0
    entry_price: Optional[float] = None
    trades: list[dict] = []          # realised round-trips: {"entry", "exit", "pnl"}
    equity_curve: list[dict] = []    # {"date", "equity"} mark-to-market at each close

    for n in range(start_index, len(bars) - 1):  # -1: bar n+1 must exist for execution
        # ----- decision: data up to and including bar n's close only -----
        signal = None
        if shares == 0 and _eval_rule(strategy.entry, bars, n):
            signal = "enter"
        elif shares > 0 and _eval_rule(strategy.exit, bars, n):
            signal = "exit"

        # ----- execution: at bar n+1's OPEN -----
        exec_price = float(bars[n + 1]["open"])
        if signal == "enter":
            shares = cash / exec_price
            entry_price = exec_price
            cash = 0.0
        elif signal == "exit":
            trades.append(
                {"entry": entry_price, "exit": exec_price, "pnl": shares * (exec_price - entry_price)}
            )
            cash = shares * exec_price
            shares = 0.0
            entry_price = None

        equity_curve.append({"date": bars[n]["timestamp"], "equity": cash + shares * closes[n]})

    # terminal: a position still open was NOT closed by an exit signal (D11).
    open_position = shares > 0
    unrealized_pnl_pct = 0.0
    if open_position:
        last_close = closes[-1]
        mtm_value = shares * last_close
        unrealized_pnl_pct = (last_close - entry_price) / entry_price * 100.0
        equity_curve.append({"date": bars[-1]["timestamp"], "equity": mtm_value})
        final_equity = mtm_value
    else:
        equity_curve.append({"date": bars[-1]["timestamp"], "equity": cash})
        final_equity = cash

    total_return_pct = (final_equity - STARTING_CASH) / STARTING_CASH * 100.0

    peak = equity_curve[0]["equity"]
    max_dd_pct = 0.0
    for point in equity_curve:
        eq = point["equity"]
        peak = max(peak, eq)
        max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100.0)

    wins = sum(1 for t in trades if t["pnl"] > 0)
    win_rate = (wins / len(trades) * 100.0) if trades else 0.0

    return {
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "num_trades": len(trades),          # realised trades ONLY
        "win_rate_pct": round(win_rate, 2),
        "open_position": open_position,
        "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
        "equity_curve": equity_curve,
    }
