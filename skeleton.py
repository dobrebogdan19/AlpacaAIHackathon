"""Walking skeleton for an autonomous trading agent.

Five steps, in order:
  1. define one hardcoded SMA-crossover strategy (plain dict)
  2. fetch 1y of daily bars and run a minimal, lookahead-free backtest
  3. apply a hardcoded promotion gate -> PROMOTED / REJECTED
  4. if promoted, submit one 1-share paper market order (guarded by DRY_RUN)
  5. print a structured decision record

Deliberately simple. No LLM, no web UI, no scheduler, no backtest library.
"""

import os
import statistics
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

# If True, step 4 does everything except actually send the order.
DRY_RUN = True

STARTING_CASH = 10_000.0

# --- step 1: one hardcoded strategy -----------------------------------------
# Closed vocabulary: "sma_crossover" only. entry/exit rules are (fast, slow)
# SMA window pairs. Enter long when fast crosses ABOVE slow; exit when it
# crosses BACK BELOW.
STRATEGY = {
    "name": "AAPL SMA(10/30) crossover",
    "symbol": "AAPL",
    "entry_rule": {"type": "sma_crossover", "direction": "above", "fast": 10, "slow": 30},
    "exit_rule": {"type": "sma_crossover", "direction": "below", "fast": 10, "slow": 30},
}

# --- promotion gate thresholds --------------------------------------------
GATE = {"min_total_return_pct": 0.0, "max_drawdown_pct": 25.0, "min_trades": 3}


def load_clients():
    load_dotenv()
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    data = StockHistoricalDataClient(key, secret)
    trading = TradingClient(key, secret, paper=True)
    return data, trading


def fetch_daily_bars(data_client, symbol, days=365):
    """Return a list of bars (oldest first) as dicts with open/close/timestamp."""
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=days),
    )
    df = data_client.get_stock_bars(req).df  # MultiIndex (symbol, timestamp)
    df = df.xs(symbol, level="symbol").sort_index()
    return [
        {"timestamp": ts.to_pydatetime(), "open": float(row["open"]), "close": float(row["close"])}
        for ts, row in df.iterrows()
    ]


def sma(values, end_idx, window):
    """SMA of `window` closes ending AT (and including) end_idx. None if not enough history."""
    if end_idx + 1 < window:
        return None
    return statistics.fmean(values[end_idx + 1 - window : end_idx + 1])


def backtest(bars, strategy):
    """Minimal event-driven backtest. Long-only, one position, all-in, no leverage.

    LOOKAHEAD AVOIDANCE (the whole point of this function):
      - The signal for bar N is computed ONLY from closes[0..N] -- data that is
        known once bar N has closed.
      - The resulting trade is executed at bar N+1's OPEN, never at bar N's own
        close. So the loop stops at len(bars)-2: a decision on the last bar can
        never be filled and is simply dropped.
      - We compare SMA(N-1) vs SMA(N) to detect a crossover; both are past/last
        closes, so no future information leaks in.
    """
    closes = [b["close"] for b in bars]
    fast_w = strategy["entry_rule"]["fast"]
    slow_w = strategy["entry_rule"]["slow"]

    cash = STARTING_CASH
    shares = 0.0
    entry_price = None
    trades = []          # completed round-trips: {"entry", "exit", "pnl"}
    equity_curve = []    # mark-to-market equity at each bar's close

    for n in range(len(bars) - 1):  # -1: need bar n+1 to exist for execution
        # ----- decision uses only data up to and including bar n's close -----
        f_now, s_now = sma(closes, n, fast_w), sma(closes, n, slow_w)
        f_prev, s_prev = sma(closes, n - 1, fast_w), sma(closes, n - 1, slow_w)

        signal = None
        if None not in (f_now, s_now, f_prev, s_prev):
            crossed_up = f_prev <= s_prev and f_now > s_now
            crossed_down = f_prev >= s_prev and f_now < s_now
            if crossed_up and shares == 0:
                signal = "enter"
            elif crossed_down and shares > 0:
                signal = "exit"

        # ----- execution happens at NEXT bar's open -----
        exec_price = bars[n + 1]["open"]
        if signal == "enter":
            shares = cash / exec_price
            entry_price = exec_price
            cash = 0.0
        elif signal == "exit":
            trades.append({"entry": entry_price, "exit": exec_price, "pnl": shares * (exec_price - entry_price)})
            cash = shares * exec_price
            shares = 0.0
            entry_price = None

        # mark-to-market equity after this bar (using bar n's close)
        equity_curve.append(cash + shares * closes[n])

    # terminal condition: a position still open at the final bar was NOT closed
    # by a strategy exit signal. We do not fabricate a trade for it. It is
    # marked to market at the last close and reported separately as unrealized.
    open_position = shares > 0
    unrealized_pnl_pct = 0.0
    if open_position:
        last_close = closes[-1]
        mtm_value = shares * last_close
        unrealized_pnl_pct = (last_close - entry_price) / entry_price * 100.0
        equity_curve.append(mtm_value)          # final equity point = mark-to-market
        final_equity = mtm_value
    else:
        equity_curve.append(cash)
        final_equity = cash

    # total_return_pct INCLUDES the mark-to-market value of any still-open
    # position (final_equity is mtm_value when open_position is True).
    total_return_pct = (final_equity - STARTING_CASH) / STARTING_CASH * 100.0

    # max drawdown over the equity curve
    peak = equity_curve[0]
    max_dd_pct = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100.0)

    # win rate is over REALIZED trades only (positions closed by an exit signal)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    win_rate = (wins / len(trades) * 100.0) if trades else 0.0

    return {
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "num_trades": len(trades),          # realized trades ONLY
        "win_rate_pct": round(win_rate, 2),
        "open_position": open_position,
        "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
    }


def promotion_gate(metrics):
    reasons = []
    if metrics["total_return_pct"] <= GATE["min_total_return_pct"]:
        reasons.append(f"total return {metrics['total_return_pct']}% <= {GATE['min_total_return_pct']}%")
    if metrics["max_drawdown_pct"] >= GATE["max_drawdown_pct"]:
        reasons.append(f"max drawdown {metrics['max_drawdown_pct']}% >= {GATE['max_drawdown_pct']}%")
    if metrics["num_trades"] < GATE["min_trades"]:
        reasons.append(f"only {metrics['num_trades']} trades (< {GATE['min_trades']})")
    promoted = not reasons
    reason = "all thresholds passed" if promoted else "; ".join(reasons)
    return promoted, reason


def submit_paper_order(trading_client, symbol):
    """Submit ONE market buy for 1 share. Market-closed is fine: the order just
    queues (TimeInForce.DAY) -- we return whatever status Alpaca gives back."""
    order_req = MarketOrderRequest(
        symbol=symbol, qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY
    )
    order = trading_client.submit_order(order_req)
    return str(order.id), str(order.status)


def main():
    print(f"=== Autonomous trading agent -- walking skeleton (DRY_RUN={DRY_RUN}) ===\n")

    print("[1] STRATEGY")
    for k, v in STRATEGY.items():
        print(f"    {k}: {v}")

    data_client, trading_client = load_clients()

    print("\n[2] BACKTEST")
    bars = fetch_daily_bars(data_client, STRATEGY["symbol"])
    print(f"    fetched {len(bars)} daily bars "
          f"({bars[0]['timestamp'].date()} -> {bars[-1]['timestamp'].date()})")
    metrics = backtest(bars, STRATEGY)
    for k, v in metrics.items():
        print(f"    {k}: {v}")

    print("\n[3] PROMOTION GATE")
    print(f"    thresholds: {GATE}")
    promoted, reason = promotion_gate(metrics)
    print(f"    {'PROMOTED' if promoted else 'REJECTED'} -- {reason}")

    print("\n[4] ORDER")
    order_id, order_status = None, None
    if not promoted:
        print("    skipped (not promoted)")
    elif DRY_RUN:
        print("    DRY_RUN -- would submit 1-share market BUY of "
              f"{STRATEGY['symbol']} to paper account; no order sent")
    else:
        try:
            order_id, order_status = submit_paper_order(trading_client, STRATEGY["symbol"])
            print(f"    order id: {order_id}")
            print(f"    status:   {order_status}  (may be 'accepted'/'pending_new' if market closed)")
        except Exception as exc:  # don't crash the pipeline, but show the full traceback
            import traceback
            traceback.print_exc()
            order_status = f"error: {exc}"
            print(f"    order failed: {exc}")

    decision_record = {
        "strategy_name": STRATEGY["name"],
        "symbol": STRATEGY["symbol"],
        "backtest_metrics": metrics,
        "gate_outcome": "PROMOTED" if promoted else "REJECTED",
        "reason": reason,
        "dry_run": DRY_RUN,
        "order_id": order_id,
        "order_status": order_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    print("\n[5] DECISION RECORD")
    print(f"    {decision_record}")


if __name__ == "__main__":
    main()
