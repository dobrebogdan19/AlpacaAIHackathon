"""Build ``seed.db`` — a database with real, varied results so a fresh deploy
shows a full dashboard immediately (T7.1).

It runs a fixed sequence of cycles against a clean ``seed.db``:

  1. LLM candidates + fast-crossover seeds, **live** — usually promotes one or
     more and submits real paper orders through the Alpaca MCP server.
  2. Slow-crossover seeds only, dry — every candidate fails ``min_trades``; a
     run that rejects everything, which is the common honest outcome (D21).
  3. Plain LLM candidates, dry — typically also all-rejected.
  4. LLM candidates + fast seeds again, dry — a second promoting run for variety.

Then a wider candidate batch (24 symbols x 3 LLM candidates each, persisted but
not cycled) enlarges the pool, and the regret ledger runs once, dry, as of a
PINNED past date (``REGRET_AS_OF``) so the wider batch is judged on the same
terms as the four cycles' output — a larger selection-bias sample (D38).

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
import regret as _regret  # noqa: E402
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


# --- wider candidate batch, for a larger selection-bias sample ---------------
#
# The selection-bias number (D38) was computed over only the 19 strategies the
# four seed cycles produced, across 7 symbols — too small to read into. This
# batch enlarges the pool the regret ledger judges. Generating candidates alone
# does nothing (new strategies have no forward data); they only count once the
# ledger splits them at the same past as-of date (D35) as everything else.
#
# Committed up front, NOT tuned to an outcome (D35 / D10): a fixed 24-symbol set
# chosen for liquidity + history depth, a fixed 3 candidates requested per
# symbol (72 total), one pass. Whatever survives validation and dedup is the
# sample; the ledger runs once and the spread is reported as-is.
_WIDER_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "SPY", "QQQ",
    "JPM", "BAC", "V", "MA", "WMT", "HD", "PG", "KO", "XOM", "CVX", "JNJ", "DIS",
    "NFLX", "AMD",
]
_WIDER_PER_SYMBOL = 3
REGRET_AS_OF = "2026-06-17"  # pinned to the date the first principled run used (D35)


def _generate_wider_batch(conn) -> int:
    """Generate ``_WIDER_PER_SYMBOL`` LLM candidates for each ``_WIDER_SYMBOLS``.

    Persisted as ordinary ``source='llm'`` candidates (``generator.generate``
    with a ``conn`` writes them). No cycle, no backtest, no order — the regret
    ledger re-runs the gate as-of on every stored strategy anyway, so these are
    evaluated on identical terms to the four cycles' output. Duplicates collapse
    against what is already stored (``insert_strategy`` returns the existing id
    on a dedup-key hit).
    """
    before = conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
    for sym in _WIDER_SYMBOLS:
        res = _generator.generate(n=_WIDER_PER_SYMBOL, symbols=[sym], conn=conn)
        log.info("  wider batch %-5s: %d unique candidate(s) (%d dup collapsed)",
                 sym, len(res.strategy_ids), res.duplicates_collapsed)
    after = conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
    log.info("wider batch: %d new strategy row(s) across %d symbols",
             after - before, len(_WIDER_SYMBOLS))
    return after - before


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

        # --- wider candidate batch (larger selection-bias sample, D38) --------
        log.info("=== seeding step: wider candidate batch (%d symbols x %d) ===",
                 len(_WIDER_SYMBOLS), _WIDER_PER_SYMBOL)
        _generate_wider_batch(conn)

        # --- Phase 4: the regret ledger, as-of a principled past date (D35) ---
        # Always dry: this is a historical simulation replayed over stored bars,
        # not a path that should place or close live orders during seeding.
        # as_of is PINNED (not auto-picked) so this run is comparable to the
        # first principled run — same terms for the wider batch and the originals.
        log.info("=== seeding step: regret ledger (as-of forward tracking) ===")
        rr = _regret.run_regret_ledger(conn=conn, dry_run=True, as_of=REGRET_AS_OF)
        log.info("  -> regret run %d, as of %s: %d evaluated, %d retirement(s)",
                 rr.run_id, rr.as_of, len(rr.records), len(rr.retirements))
        if rr.selection_bias:
            log.info("  -> %s", rr.selection_bias.headline())

        runs = conn.execute(
            "SELECT id, n_promoted, n_rejected FROM runs WHERE as_of IS NULL ORDER BY id").fetchall()
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
