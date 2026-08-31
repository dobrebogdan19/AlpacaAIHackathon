"""calibrate.py — propose gate thresholds from forward evidence, honestly.

A Phase-4 extension. The regret ledger already measures that the promotion gate
rejected the two biggest forward winners (D45) and does nothing with it. This
closes that loop: it searches the threshold space for the combination that
*would have* maximised the mean forward return of the promoted set, on the same
as-of run the ledger built.

**The result is optimistic by construction.** It is in-sample optimisation over
the very forward returns it is being scored on — the textbook way to manufacture
a backtest that does not survive contact with new data. This module refuses to
present it as "better thresholds":

  * it holds out one candidate in every ``HOLDOUT_EVERY`` (deterministically, by
    strategy id), calibrates on the rest, and reports the calibrated thresholds'
    performance on the held-out candidates — one number that is not fitted;
  * it reports how much of the in-sample improvement survives that holdout;
  * if the improvement does not survive, it says so plainly and recommends
    keeping the current thresholds. That is the correct and interesting result
    (D10) — not a bug to be worked around.

Nothing here writes to ``gate.py``. The proposal is stored (``calibrations``
table) and surfaced (``GET /api/calibration``). Applying it stays a deliberate
human act — a system that silently retunes itself on fitted data is exactly the
failure mode this project exists to expose.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import date

import db
import gate

log = logging.getLogger("calibrate")

# --- the search space — explicit, ordered, so the search is deterministic ---
GRID: dict[str, list[float]] = {
    "min_total_return_pct": [0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0],
    "max_drawdown_pct":     [10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0],
    "min_trades":           [0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0],
}

# A promoted set smaller than this makes the mean one or two strategies' luck —
# the same noise argument that puts ``min_trades`` at 10 in gate.py. Chosen up
# front, not tuned to an outcome; the holdout verdict is the same for 3 or 8.
MIN_PROMOTED = 5

# Every Nth candidate (ordered by strategy id) is held out of calibration.
HOLDOUT_EVERY = 3

# Below this many percentage points, the search "found" nothing over the current
# thresholds and we say so rather than reporting a rounding difference as a win.
MIN_IMPROVEMENT_PP = 0.01


@dataclass
class Candidate:
    """One as-of decision: the metrics the gate saw, and what happened after."""

    strategy_id: int
    name: str
    symbol: str
    insample: dict            # total_return_pct, max_drawdown_pct, num_trades
    forward_return_pct: float


# --- loading ---------------------------------------------------------------


def _latest_as_of_run(conn) -> dict | None:
    row = conn.execute(
        "SELECT * FROM runs WHERE as_of IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row is not None else None


def load_candidates(conn, run_id: int | None = None) -> tuple[dict, list[Candidate]]:
    """Read every as-of decision of one regret-ledger run straight from storage.

    No bars are fetched and no backtest is re-run — the in-sample and forward
    metrics the ledger already stored (``backtests.kind`` in ('insample',
    'forward')) are exactly what the gate saw and what actually happened.
    """
    if run_id is None:
        run = _latest_as_of_run(conn)
        if run is None:
            raise ValueError("no as-of run in the database — run the regret ledger first")
    else:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        run = dict(row)

    rows = conn.execute(
        """SELECT d.strategy_id, s.name, s.symbol,
                  bi.metrics_json AS insample_json,
                  bf.metrics_json AS forward_json
             FROM decisions d
             JOIN strategies s ON s.id = d.strategy_id
             JOIN backtests bi ON bi.strategy_id = d.strategy_id
                              AND bi.run_id = d.run_id AND bi.kind = 'insample'
             JOIN backtests bf ON bf.strategy_id = d.strategy_id
                              AND bf.run_id = d.run_id AND bf.kind = 'forward'
            WHERE d.run_id = ? AND d.outcome IN ('promoted', 'rejected')
            ORDER BY d.strategy_id""",
        (run["id"],),
    ).fetchall()

    candidates: list[Candidate] = []
    for r in rows:
        ins = json.loads(r["insample_json"])
        fwd = json.loads(r["forward_json"])
        candidates.append(Candidate(
            strategy_id=int(r["strategy_id"]),
            name=r["name"],
            symbol=r["symbol"],
            insample={
                "total_return_pct": float(ins["total_return_pct"]),
                "max_drawdown_pct": float(ins["max_drawdown_pct"]),
                "num_trades": int(ins["num_trades"]),
            },
            forward_return_pct=float(fwd["total_return_pct"]),
        ))
    return {"run_id": int(run["id"]), "as_of": run["as_of"]}, candidates


# --- the search ----------------------------------------------------------


def _promotes(insample: dict, thresholds: dict[str, float]) -> bool:
    """Exactly the gate's own logic (gate.evaluate), so a proposal means the
    same thing a real promotion would."""
    return gate.evaluate(insample, thresholds).promoted


def mean_forward(
    candidates: list[Candidate],
    thresholds: dict[str, float],
    *,
    min_promoted: int = 1,
) -> tuple[float | None, list[int]]:
    """Mean forward return of the candidates these thresholds would promote.

    Returns ``(None, ids)`` if fewer than ``min_promoted`` are promoted — too
    small a set to take a mean of.
    """
    promoted = [c for c in candidates if _promotes(c.insample, thresholds)]
    ids = [c.strategy_id for c in promoted]
    if len(promoted) < min_promoted:
        return None, ids
    return sum(c.forward_return_pct for c in promoted) / len(promoted), ids


def grid_search(
    candidates: list[Candidate],
    *,
    min_promoted: int = MIN_PROMOTED,
) -> tuple[dict[str, float] | None, float | None]:
    """Deterministic exhaustive search of ``GRID``.

    Iterates the grid in a fixed nested order and keeps the first combination
    that strictly beats the best so far, so the result never depends on dict or
    set ordering. Returns ``(thresholds, mean_forward_return)`` or ``(None, None)``
    if no combination promotes ``min_promoted`` candidates.
    """
    best_thresholds: dict[str, float] | None = None
    best_score: float | None = None
    for tr in GRID["min_total_return_pct"]:
        for dd in GRID["max_drawdown_pct"]:
            for tt in GRID["min_trades"]:
                thresholds = {
                    "min_total_return_pct": tr,
                    "max_drawdown_pct": dd,
                    "min_trades": tt,
                }
                score, _ = mean_forward(candidates, thresholds, min_promoted=min_promoted)
                if score is None:
                    continue
                if best_score is None or score > best_score + 1e-9:
                    best_score, best_thresholds = score, thresholds
    return best_thresholds, best_score


# --- holdout -------------------------------------------------------------


def split_holdout(
    candidates: list[Candidate], every: int = HOLDOUT_EVERY
) -> tuple[list[Candidate], list[Candidate]]:
    """Partition candidates into (train, holdout), deterministically.

    Sorted by strategy id, every ``every``-th candidate (0, every, 2*every, …) is
    held out. Interleaving keeps both sides representative across the run rather
    than splitting on time or symbol.
    """
    ordered = sorted(candidates, key=lambda c: c.strategy_id)
    holdout = [c for i, c in enumerate(ordered) if i % every == 0]
    train = [c for i, c in enumerate(ordered) if i % every != 0]
    return train, holdout


# --- the record --------------------------------------------------------


def _threshold_moves(current: dict[str, float], proposed: dict[str, float]) -> dict:
    return {
        key: {
            "from": current[key],
            "to": proposed[key],
            "delta": round(proposed[key] - current[key], 4),
        }
        for key in ("min_total_return_pct", "max_drawdown_pct", "min_trades")
    }


def _summary(candidates: list[Candidate], thresholds: dict[str, float]) -> dict:
    score, ids = mean_forward(candidates, thresholds, min_promoted=1)
    return {
        "thresholds": {k: thresholds[k] for k in
                       ("min_total_return_pct", "max_drawdown_pct", "min_trades")},
        "n_promoted": len(ids),
        "promoted_strategy_ids": ids,
        "mean_forward_return_pct": round(score, 4) if score is not None else None,
    }


def calibrate(conn, run_id: int | None = None) -> dict:
    """Build the full calibration record for one as-of run. Pure computation —
    the caller persists it."""
    min_promoted = MIN_PROMOTED  # module global, re-read here so tests can override it
    meta, candidates = load_candidates(conn, run_id)
    if len(candidates) < min_promoted * 2:
        raise ValueError(
            f"only {len(candidates)} as-of candidates — too few to calibrate against"
        )

    current = {k: float(v) for k, v in gate.GATE_THRESHOLDS.items()}
    current_full = _summary(candidates, current)

    # --- 1. the optimistic full-sample fit (item 2 of the brief) ---
    proposed_thresholds, proposed_score = grid_search(candidates, min_promoted=min_promoted)
    if proposed_thresholds is None:  # pragma: no cover - guarded by the size check
        proposed_thresholds, proposed_score = current, current_full["mean_forward_return_pct"]
    full_improvement_pp = round(
        (proposed_score or 0.0) - (current_full["mean_forward_return_pct"] or 0.0), 4
    )
    if full_improvement_pp < MIN_IMPROVEMENT_PP:
        # the search found nothing worth proposing — do not surface an arbitrary
        # grid corner as a "proposal", report the current thresholds unchanged
        proposed_thresholds = current
        full_improvement_pp = 0.0
    proposed_full = _summary(candidates, proposed_thresholds)

    # --- 2. the holdout — one number that is not fitted (item 3) ---
    train, holdout = split_holdout(candidates)
    train_thresholds, _ = grid_search(train, min_promoted=min_promoted)
    if train_thresholds is None:  # pragma: no cover
        train_thresholds = current

    cur_train, _ = mean_forward(train, current, min_promoted=1)
    prop_train, _ = mean_forward(train, train_thresholds, min_promoted=1)
    cur_hold, cur_hold_ids = mean_forward(holdout, current, min_promoted=1)
    prop_hold, prop_hold_ids = mean_forward(holdout, train_thresholds, min_promoted=1)

    train_improvement_pp = (
        round(prop_train - cur_train, 4)
        if prop_train is not None and cur_train is not None else None
    )
    holdout_improvement_pp = (
        round(prop_hold - cur_hold, 4)
        if prop_hold is not None and cur_hold is not None else None
    )
    if (train_improvement_pp and holdout_improvement_pp is not None
            and abs(train_improvement_pp) > 1e-9):
        survival_frac = round(holdout_improvement_pp / train_improvement_pp, 4)
    else:
        survival_frac = None

    survives = holdout_improvement_pp is not None and holdout_improvement_pp > 0

    # --- 3. the verdict — honest about what this is (item 3, D10) ---
    if full_improvement_pp < MIN_IMPROVEMENT_PP:
        verdict = "no-improvement"
        recommendation = (
            "Keep the current thresholds. The search found no combination that "
            "beats them on the full as-of sample."
        )
    elif not survives:
        verdict = "does-not-survive-holdout"
        why = ("the calibrated thresholds promote too few held-out candidates to score"
               if holdout_improvement_pp is None
               else f"the in-sample gain reverses to {holdout_improvement_pp:+.2f}pp on the holdout")
        recommendation = (
            "Keep the current thresholds. The full-sample gain is in-sample "
            f"overfitting: {why}."
        )
    else:
        verdict = "survives-holdout"
        recommendation = (
            "The improvement partially survives a holdout. Treat as a candidate "
            "change only — a human must still edit gate.py deliberately. One "
            "as-of run is not enough evidence to retune on."
        )

    return {
        "schema_version": 1,
        "run_id": meta["run_id"],
        "as_of": meta["as_of"],
        "generated_on": date.today().isoformat(),
        "n_candidates": len(candidates),
        "objective": "maximise mean forward total return of the promoted set",
        "grid": GRID,
        "min_promoted": min_promoted,
        "current": current_full,
        "proposed": {
            **proposed_full,
            "delta_vs_current_pp": full_improvement_pp,
            "note": (
                "In-sample optimisation over the same forward returns it is "
                "scored on. Optimistic by construction — see the holdout."
            ),
        },
        "threshold_moves": _threshold_moves(
            current_full["thresholds"], proposed_full["thresholds"]
        ),
        "holdout": {
            "method": f"every {HOLDOUT_EVERY}rd candidate by strategy id",
            "n_train": len(train),
            "n_holdout": len(holdout),
            "thresholds_calibrated_on_train": {
                k: train_thresholds[k] for k in
                ("min_total_return_pct", "max_drawdown_pct", "min_trades")
            },
            "train": {
                "current_mean_forward_pct": round(cur_train, 4) if cur_train is not None else None,
                "proposed_mean_forward_pct": round(prop_train, 4) if prop_train is not None else None,
                "improvement_pp": train_improvement_pp,
            },
            "holdout": {
                "current_mean_forward_pct": round(cur_hold, 4) if cur_hold is not None else None,
                "current_n_promoted": len(cur_hold_ids),
                "proposed_mean_forward_pct": round(prop_hold, 4) if prop_hold is not None else None,
                "proposed_n_promoted": len(prop_hold_ids),
                "improvement_pp": holdout_improvement_pp,
            },
            "improvement_survival_fraction": survival_frac,
            "survives": survives,
        },
        "verdict": verdict,
        "recommendation": recommendation,
        "applied": False,
        "caveat": (
            "This proposal is NOT applied. gate.py is unchanged. A system that "
            "silently retunes itself on fitted data is the failure mode this "
            "project exists to expose — applying thresholds stays a human act."
        ),
    }


# --- persistence + CLI --------------------------------------------------


def run_and_store(conn, run_id: int | None = None) -> tuple[int, dict]:
    record = calibrate(conn, run_id)
    cal_id = db.insert_calibration(
        conn, run_id=record["run_id"], as_of=record["as_of"],
        record_json=json.dumps(record),
    )
    return cal_id, record


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=None,
                        help="SQLite file to read/write (default: db.DB_PATH)")
    parser.add_argument("--run-id", type=int, default=None,
                        help="regret-ledger run id (default: the latest as-of run)")
    parser.add_argument("--no-store", action="store_true",
                        help="compute and print, but do not write the calibrations row")
    args = parser.parse_args()

    conn = db.connect(args.db_path or db.DB_PATH)
    try:
        if args.no_store:
            record = calibrate(conn, args.run_id)
            cal_id = None
        else:
            cal_id, record = run_and_store(conn, args.run_id)
    finally:
        conn.close()

    print(json.dumps(record, indent=2))
    print()
    print(f"verdict: {record['verdict']}")
    print(f"recommendation: {record['recommendation']}")
    if cal_id is not None:
        print(f"stored as calibrations.id = {cal_id}")


if __name__ == "__main__":
    main()
