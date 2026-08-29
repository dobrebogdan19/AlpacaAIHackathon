"""The promotion gate (T3.1).

A candidate strategy is promoted only if its backtest metrics clear every
threshold below. Nothing about the gate is implicit: the thresholds live in one
dict at the top of this file, and every candidate — promoted or rejected —
comes back with a written reason that names the specific threshold(s) it failed.
The caller persists that reason to a ``decisions`` row (see ``cycle.py``); this
module never touches the database, so it stays trivially testable.

On ``min_trades``: the Phase 1 skeleton used 3, with a TODO saying that was too
low. Three realised round-trips over ~250 daily bars is statistical noise. It is
raised to 10 here. If most candidates now fail the gate, that is the correct
outcome and it is reported as-is — the bar is not lowered to make a demo look
better (CLAUDE.md: never overstate what this system does).

The gate reads only *realised* trade metrics. Per DECISIONS.md D11 the engine
already excludes a still-open terminal position from ``num_trades``, so a
position that was never actually closed cannot satisfy ``min_trades``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- thresholds — the only knobs, all in one place ------------------------
GATE_THRESHOLDS: dict[str, float] = {
    "min_total_return_pct": 0.0,   # must at least not lose money over the window
    "max_drawdown_pct": 25.0,      # peak-to-trough equity decline ceiling
    "min_trades": 10,              # realised round-trips; below this the result is noise
}


@dataclass
class GateResult:
    promoted: bool
    reason: str
    failed: list[str] = field(default_factory=list)   # threshold keys that failed


def evaluate(metrics: dict, thresholds: dict[str, float] | None = None) -> GateResult:
    """Apply the gate to one backtest's ``metrics`` dict (as engine.run_backtest returns).

    Returns a :class:`GateResult` whose ``reason`` always names concrete numbers
    and thresholds, whether the candidate passed or failed.
    """
    t = thresholds or GATE_THRESHOLDS
    total_return = float(metrics["total_return_pct"])
    max_dd = float(metrics["max_drawdown_pct"])
    n_trades = int(metrics["num_trades"])

    failed: list[str] = []
    reasons: list[str] = []

    if total_return < t["min_total_return_pct"]:
        failed.append("min_total_return_pct")
        reasons.append(
            f"total return {total_return:.2f}% < {t['min_total_return_pct']:.2f}% required"
        )
    if max_dd > t["max_drawdown_pct"]:
        failed.append("max_drawdown_pct")
        reasons.append(
            f"max drawdown {max_dd:.2f}% > {t['max_drawdown_pct']:.2f}% allowed"
        )
    if n_trades < t["min_trades"]:
        failed.append("min_trades")
        reasons.append(
            f"only {n_trades} realised trade(s), {int(t['min_trades'])} required"
        )

    if failed:
        return GateResult(promoted=False, reason="; ".join(reasons), failed=failed)

    reason = (
        f"all thresholds passed: total return {total_return:.2f}% "
        f">= {t['min_total_return_pct']:.2f}%, max drawdown {max_dd:.2f}% "
        f"<= {t['max_drawdown_pct']:.2f}%, {n_trades} trades >= {int(t['min_trades'])}"
    )
    return GateResult(promoted=True, reason=reason, failed=[])
