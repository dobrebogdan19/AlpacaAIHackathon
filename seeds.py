"""Hand-written seed candidates + a generator wrapper that appends them.

Why this exists: LLM-generated crossover strategies rarely clear the deliberately
strict ``min_trades = 10`` gate on ~1 year of daily bars (D21), so a pure
``generator.generate()`` often promotes nothing on a given day — the honest
outcome, and fine for the record, but a weak thing to hand a visitor who just
clicked "Run a new cycle". These fast SMA-crossover seeds trade often enough to
usually clear the trade-count threshold, so a cycle reliably exercises the whole
pipeline including a real MCP order.

Nothing about the gate is relaxed for them. Seeds go through the identical
``engine.run_backtest`` -> ``gate.evaluate`` -> ``risk.check`` -> ``mcp_client``
path as any LLM candidate (D26). They are stored with ``source='manual'`` so a
reviewer can tell them apart in the ``strategies`` table.
"""

from __future__ import annotations

from dataclasses import dataclass

import db
import generator as _generator
from schema import Condition, IndicatorName, IndicatorRef, Operator, Rule, Strategy


def _fast_cross(symbol: str, fast: int, slow: int) -> Strategy:
    ref = lambda p: IndicatorRef(indicator=IndicatorName.SMA, period=p)
    return Strategy(
        name=f"{symbol} SMA {fast}/{slow} fast-cross (seed)",
        symbol=symbol,
        rationale="fast SMA crossover; trades often enough to clear the trade-count gate",
        entry=Rule(conditions=[Condition(left=ref(fast), operator=Operator.CROSSES_ABOVE,
                                         right=ref(slow))], join=None),
        exit=Rule(conditions=[Condition(left=ref(fast), operator=Operator.CROSSES_BELOW,
                                        right=ref(slow))], join=None),
    )


# A spread of liquid-options symbols and crossover speeds. Some clear the gate on
# real bars and some do not — which is the point of showing them. Widened 4 -> 8
# for the competition window (D53): breadth, not cycle frequency, is what puts new
# positions on a daily-bar account, and every symbol here has a tight option
# market for the expression layer (D48).
SEED_STRATEGIES: list[Strategy] = [
    _fast_cross("SPY", 3, 7),
    _fast_cross("QQQ", 4, 9),
    _fast_cross("AAPL", 2, 8),
    _fast_cross("MSFT", 5, 12),
    _fast_cross("NVDA", 4, 10),
    _fast_cross("AMZN", 3, 11),
    _fast_cross("AMD", 5, 15),
    _fast_cross("META", 6, 13),
]


@dataclass
class _Gen:
    strategies: list
    strategy_ids: list


def generate_with_seeds(*, n, symbols, conn, run_id, seeds: list[Strategy] | None = None):
    """A ``generate_fn`` for ``cycle.run_cycle``: real LLM candidates + the seeds."""
    real = _generator.generate(n=n, symbols=symbols, conn=conn, run_id=run_id)
    strategies = list(real.strategies)
    ids = list(real.strategy_ids)
    for s in (seeds if seeds is not None else SEED_STRATEGIES):
        key = _generator.dedup_key(s)
        sid = db.insert_strategy(
            conn, name=s.name, symbol=s.symbol, schema_json=s.model_dump_json(),
            rationale=s.rationale, source="manual", status="candidate", dedup_key=key,
        )
        strategies.append(s)
        ids.append(sid)
    return _Gen(strategies, ids)
