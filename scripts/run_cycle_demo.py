"""Manual demo: one full cycle that reaches the MCP order path.

LLM-generated crossover strategies rarely clear the (deliberately strict)
``min_trades = 10`` gate on ~1 year of daily bars, so a pure ``run_cycle()`` on
a given day often promotes nothing — which is the honest outcome and is fine.
This script guarantees the demo shows the whole pipeline including a real order
by ALSO feeding in the hand-written fast-crossover seeds from ``seeds.py``. They
go through the *real* gate on *real* bars exactly like any other candidate.

    python scripts/run_cycle_demo.py            # DRY_RUN honoured from env
    DRY_RUN= python scripts/run_cycle_demo.py   # live: real paper orders via MCP

Requires OPENAI_API_KEY + ALPACA_API_KEY/SECRET in .env. Paper only.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cycle  # noqa: E402
import db  # noqa: E402
from seeds import generate_with_seeds  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> None:
    result = cycle.run_cycle(n=4, generate_fn=generate_with_seeds)
    print("\n" + result.summary())
    conn = db.connect()
    print("\norders table:")
    for o in db.list_orders(conn):
        print(f"  strategy {o['strategy_id']}  {o['symbol']}  {o['side']}  "
              f"status={o['status']}  broker_id={o['broker_order_id']}  via={o['submitted_via']}")
    conn.close()


if __name__ == "__main__":
    main()
