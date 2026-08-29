"""Manual demo: one full cycle that reaches the MCP order path.

LLM-generated crossover strategies rarely clear the (deliberately strict)
``min_trades = 10`` gate on ~1 year of daily bars, so a pure ``run_cycle()`` on
a given day often promotes nothing — which is the honest outcome and is fine.
This script guarantees the demo shows the whole pipeline including a real order
by ALSO feeding in a few hand-written fast-crossover candidates. They are
evaluated by the *real* gate on *real* bars exactly like any other candidate —
nothing about the gate is relaxed.

    python scripts/run_cycle_demo.py            # DRY_RUN honoured from env
    DRY_RUN= python scripts/run_cycle_demo.py   # live: real paper orders via MCP

Requires OPENAI_API_KEY + ALPACA_API_KEY/SECRET in .env. Paper only.
"""

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cycle  # noqa: E402
import db  # noqa: E402
import generator as _generator  # noqa: E402
from schema import Condition, IndicatorName, IndicatorRef, Operator, Rule, Strategy  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


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


SEEDS = [_fast_cross("SPY", 3, 7), _fast_cross("AAPL", 2, 8)]


@dataclass
class _Gen:
    strategies: list
    strategy_ids: list


def _generate_with_seeds(*, n, symbols, conn, run_id):
    real = _generator.generate(n=n, symbols=symbols, conn=conn, run_id=run_id)
    strategies = list(real.strategies)
    ids = list(real.strategy_ids)
    for s in SEEDS:
        key = _generator.dedup_key(s)
        sid = db.insert_strategy(
            conn, name=s.name, symbol=s.symbol, schema_json=s.model_dump_json(),
            rationale=s.rationale, source="manual", status="candidate", dedup_key=key,
        )
        strategies.append(s)
        ids.append(sid)
    return _Gen(strategies, ids)


def main() -> None:
    result = cycle.run_cycle(n=4, generate_fn=_generate_with_seeds)
    print("\n" + result.summary())
    conn = db.connect()
    print("\norders table:")
    for o in db.list_orders(conn):
        print(f"  strategy {o['strategy_id']}  {o['symbol']}  {o['side']}  "
              f"status={o['status']}  broker_id={o['broker_order_id']}  via={o['submitted_via']}")
    conn.close()


if __name__ == "__main__":
    main()
