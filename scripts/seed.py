"""Build ``seed.db`` — a database with real, varied results so a fresh deploy
shows a full dashboard immediately (T7.1).

It runs a fixed sequence of cycles against a clean ``seed.db``:

  1. LLM candidates + fast-crossover seeds, **live** — usually promotes one or
     more and submits real paper orders through the Alpaca MCP server.
  2. Slow-crossover seeds only, dry — every candidate fails ``min_trades``; a
     run that rejects everything, which is the common honest outcome (D21).
  3. Plain LLM candidates, dry — typically also all-rejected.
  4. LLM candidates + fast seeds again, dry — a second promoting run for variety.

Guarantees at least one run that promoted and one that rejected everything.

    python scripts/seed.py             # step 1 live (real paper orders via MCP)
    python scripts/seed.py --all-dry   # every step dry — no orders submitted

Commit the resulting ``seed.db``. On the deployed instance ``api.py`` copies it
onto the persistent volume on first boot.
"""

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# Point the whole system at seed.db BEFORE importing db / data (they read the
# DB_PATH env var at import time).
SEED_DB = _ROOT / "seed.db"
os.environ["DB_PATH"] = str(SEED_DB)

import logging  # noqa: E402

import cycle  # noqa: E402
import db  # noqa: E402
import generator as _generator  # noqa: E402
from schema import Condition, IndicatorName, IndicatorRef, Operator, Rule, Strategy  # noqa: E402
from seeds import _Gen, generate_with_seeds  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("seed")


def _slow_cross(symbol: str, fast: int, slow: int) -> Strategy:
    ref = lambda p: IndicatorRef(indicator=IndicatorName.SMA, period=p)
    return Strategy(
        name=f"{symbol} SMA {fast}/{slow} slow-cross (seed)",
        symbol=symbol,
        rationale="slow SMA crossover; trades rarely — expected to fail the trade-count gate",
        entry=Rule(conditions=[Condition(left=ref(fast), operator=Operator.CROSSES_ABOVE,
                                         right=ref(slow))], join=None),
        exit=Rule(conditions=[Condition(left=ref(fast), operator=Operator.CROSSES_BELOW,
                                        right=ref(slow))], join=None),
    )


_SLOW_SEEDS = [_slow_cross("SPY", 50, 150), _slow_cross("QQQ", 60, 180),
               _slow_cross("AAPL", 50, 200)]


def _slow_seeds_only(*, n, symbols, conn, run_id):
    strategies, ids = [], []
    for s in _SLOW_SEEDS:
        key = _generator.dedup_key(s)
        sid = db.insert_strategy(
            conn, name=s.name, symbol=s.symbol, schema_json=s.model_dump_json(),
            rationale=s.rationale, source="manual", status="candidate", dedup_key=key,
        )
        strategies.append(s); ids.append(sid)
    return _Gen(strategies, ids)


def main() -> None:
    all_dry = "--all-dry" in sys.argv
    if SEED_DB.exists():
        SEED_DB.unlink()
        log.info("removed existing %s", SEED_DB)

    steps = [
        ("LLM + fast seeds (live)", generate_with_seeds, None if not all_dry else True),
        ("slow seeds only (dry)", _slow_seeds_only, True),
        ("plain LLM (dry)", _generator.generate, True),
        ("LLM + fast seeds (dry)", generate_with_seeds, True),
    ]

    conn = db.connect()
    try:
        for label, gen_fn, dry in steps:
            log.info("=== seeding step: %s ===", label)
            res = cycle.run_cycle(n=4, conn=conn, generate_fn=gen_fn, dry_run=dry)
            log.info("  -> run %d: %d generated, %d promoted, %d rejected, %d orders",
                     res.run_id, res.n_generated, res.n_promoted, res.n_rejected,
                     res.n_orders_submitted)

        runs = conn.execute(
            "SELECT id, n_promoted, n_rejected FROM runs ORDER BY id").fetchall()
        promoted_any = any(r["n_promoted"] and r["n_promoted"] > 0 for r in runs)
        rejected_all = any(
            (r["n_promoted"] or 0) == 0 and (r["n_rejected"] or 0) > 0 for r in runs)
        print("\nseed summary:")
        for r in runs:
            print(f"  run {r['id']}: {r['n_promoted']} promoted / {r['n_rejected']} rejected")
        print(f"\n  at least one promoting run: {promoted_any}")
        print(f"  at least one all-rejected run: {rejected_all}")
        if not (promoted_any and rejected_all):
            print("\n  WARNING: seed did not produce both a promote and an all-reject run.")
            sys.exit(1)
        print(f"\n  wrote {SEED_DB} ({SEED_DB.stat().st_size:,} bytes) — commit it.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
