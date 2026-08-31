"""The promotion gate (T3.1).

A candidate strategy is promoted only if its backtest metrics clear every
threshold below. Nothing about the gate is implicit: the thresholds live in one
dict at the top of this file, and every candidate — promoted or rejected —
comes back with a written reason that names the specific threshold(s) it failed.
The caller persists that reason to a ``decisions`` row (see ``cycle.py``); this
module never touches the database, so it stays trivially testable.

On ``min_trades``: the Phase 1 skeleton used 3, with a TODO saying that was too
low. Three realised round-trips over ~250 daily bars is statistical noise. It is
raised to 10 in ``GATE_THRESHOLDS`` here. If most candidates fail the gate on
those defaults, that is the correct outcome and it is reported as-is — the bar is
not lowered to make a demo look better (CLAUDE.md: never overstate).

For a short *live* window (the hackathon judges ~four trading days of paper
P&L), ``min_trades = 10`` filters out exactly the medium-frequency strategies
that could plausibly open a position before the deadline. ``active_thresholds()``
overlays the ``GATE_*`` env vars on the strict dict for that case (D50); the
competition instance sets only ``GATE_MIN_TRADES=3``. ``GATE_THRESHOLDS`` itself
is unchanged, so the regret ledger, ``calibrate.py`` and the committed seed still
read the strict values and the earlier analysis stays valid.

The gate reads only *realised* trade metrics. Per DECISIONS.md D11 the engine
already excludes a still-open terminal position from ``num_trades``, so a
position that was never actually closed cannot satisfy ``min_trades``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("gate")

# --- thresholds — the strict defaults, all in one place ------------------------
# These are calibrated for judging a strategy over ~250 daily bars: 3 realised
# round-trips in a year is noise, so ``min_trades`` sits at 10 (D21). The regret
# ledger, ``calibrate.py`` and the committed seed all read THIS dict, so that
# earlier analysis stays valid whatever the live deployment does.
GATE_THRESHOLDS: dict[str, float] = {
    "min_total_return_pct": 0.0,   # must at least not lose money over the window
    "max_drawdown_pct": 25.0,      # peak-to-trough equity decline ceiling
    "min_trades": 10,              # realised round-trips; below this the result is noise
}

# For a short live window (the hackathon judges four trading days of paper P&L),
# the strict ``min_trades`` filters out exactly the medium-frequency strategies
# that could plausibly open a position before the deadline. The active thresholds
# are therefore env-overridable (D50) — the competition instance sets
# ``GATE_MIN_TRADES=3`` and nothing else. ``min_total_return_pct`` and
# ``max_drawdown_pct`` are quality/risk controls on the backtest and are NOT
# loosened to chase returns; the env hooks exist only so all three stay in one
# configurable place.
_ENV_KEYS: dict[str, str] = {
    "min_total_return_pct": "GATE_MIN_TOTAL_RETURN_PCT",
    "max_drawdown_pct": "GATE_MAX_DRAWDOWN_PCT",
    "min_trades": "GATE_MIN_TRADES",
}


def active_thresholds() -> dict[str, float]:
    """The thresholds actually in force: the strict defaults, with any set via
    the ``GATE_*`` env vars overlaid. Only keys with a numeric env value are
    overridden; a missing or unparseable var leaves the strict default."""
    t = dict(GATE_THRESHOLDS)
    for key, env in _ENV_KEYS.items():
        raw = os.getenv(env)
        if raw is None or not raw.strip():
            continue
        try:
            t[key] = float(raw)
        except ValueError:
            log.warning("ignoring non-numeric %s=%r — keeping default %s", env, raw, t[key])
    return t


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
    t = thresholds or active_thresholds()
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
