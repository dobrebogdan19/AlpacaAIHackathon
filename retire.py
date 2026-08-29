"""The retirement rule (T4.3) — when does a shadow retire an active strategy?

Like ``gate.py``, this module is pure decision logic: it never touches the
database or the broker. ``regret.py`` calls :func:`find_retirements` and then
does the persistence, the MCP position close, and the post-mortem.

The rule, stated so a reviewer can check it against the numbers:

    An active strategy A is retired in favour of a rejected shadow S when, over
    the forward window (bars AFTER the as-of decision date):

      1. the window is at least ``min_forward_bars`` bars long — below this,
         daily-bar forward performance is dominated by one or two trades and is
         not evidence of anything;
      2. S's forward total return exceeds A's by more than
         ``min_outperformance_pct`` percentage points — the gap must be
         economically meaningful, not rounding;
      3. A's *own* forward total return is at or below
         ``active_max_forward_return_pct`` — an absolute condition. If A is still
         making money we do not retire it just because some shadow made more:
         that would churn active strategies on noise. The claim is that the
         agent revises a decision when evidence *contradicts* it, and a losing
         active strategy that a rejected candidate beat is exactly that.

Shadows are matched to actives by **symbol** — comparing forward returns only
controls for the instrument when the instrument is the same. A shadow on a
different symbol is not a like-for-like alternative to the capital decision that
was made, so it is not considered here.

Defaults are justified in DECISIONS.md (D36). All three live in one dict.
"""

from __future__ import annotations

from dataclasses import dataclass, field

RETIREMENT_POLICY: dict[str, float] = {
    "min_forward_bars": 40,                 # ~2 trading months of forward evidence
    "min_outperformance_pct": 5.0,          # shadow return − active return, pp
    "active_max_forward_return_pct": 0.0,   # active must also be flat-to-losing forward
}


@dataclass
class ShadowRecord:
    """One strategy as seen by the regret ledger: its as-of decision and both
    halves of the split (in-sample basis + forward tracking)."""

    strategy_id: int
    name: str
    symbol: str
    as_of_decision: str            # 'promoted' | 'rejected'
    insample_metrics: dict
    forward_metrics: dict
    forward_bars: int

    @property
    def forward_return_pct(self) -> float:
        return float(self.forward_metrics["total_return_pct"])

    @property
    def insample_return_pct(self) -> float:
        return float(self.insample_metrics["total_return_pct"])


@dataclass
class Retirement:
    retired: ShadowRecord
    winner: ShadowRecord
    reason: str
    facts: dict = field(default_factory=dict)


def _facts(active: ShadowRecord, shadow: ShadowRecord, policy: dict) -> dict:
    """Only numbers — this is what the post-mortem LLM is allowed to see (D37)."""
    keys = ("total_return_pct", "max_drawdown_pct", "num_trades", "win_rate_pct",
            "open_position", "unrealized_pnl_pct")
    return {
        "symbol": active.symbol,
        "forward_bars": shadow.forward_bars,
        "policy": dict(policy),
        "retired_strategy": {
            "name": active.name,
            "insample": {k: active.insample_metrics.get(k) for k in keys},
            "forward": {k: active.forward_metrics.get(k) for k in keys},
        },
        "promoted_shadow": {
            "name": shadow.name,
            "insample": {k: shadow.insample_metrics.get(k) for k in keys},
            "forward": {k: shadow.forward_metrics.get(k) for k in keys},
        },
    }


def find_retirements(
    records: list[ShadowRecord], policy: dict[str, float] | None = None
) -> list[Retirement]:
    """Return every (active -> shadow) retirement the rule fires for.

    Actives are considered worst-forward first, so a shadow that could retire
    several is spent on the biggest regret. One active is retired at most once,
    by its single best-forward shadow; one shadow retires at most one active.
    """
    p = policy or RETIREMENT_POLICY
    by_symbol: dict[str, list[ShadowRecord]] = {}
    for r in records:
        by_symbol.setdefault(r.symbol, []).append(r)

    retirements: list[Retirement] = []
    claimed_winners: set[int] = set()

    for symbol, group in by_symbol.items():
        actives = [r for r in group if r.as_of_decision == "promoted"]
        shadows = [
            r for r in group
            if r.as_of_decision == "rejected"
            and r.forward_bars >= p["min_forward_bars"]
        ]
        for active in sorted(actives, key=lambda a: a.forward_return_pct):
            if active.forward_return_pct > p["active_max_forward_return_pct"]:
                continue  # active is still making money — do not churn it
            beaten_by = [
                s for s in shadows
                if s.strategy_id not in claimed_winners
                and s.forward_return_pct - active.forward_return_pct
                > p["min_outperformance_pct"]
            ]
            if not beaten_by:
                continue
            winner = max(beaten_by, key=lambda s: s.forward_return_pct)
            claimed_winners.add(winner.strategy_id)
            margin = winner.forward_return_pct - active.forward_return_pct
            reason = (
                f"shadow '{winner.name}' returned {winner.forward_return_pct:+.2f}% vs "
                f"active '{active.name}' {active.forward_return_pct:+.2f}% over "
                f"{winner.forward_bars} forward bars (margin {margin:.2f}pp > "
                f"{p['min_outperformance_pct']:.2f}pp), and the active strategy's "
                f"forward return {active.forward_return_pct:+.2f}% is at or below "
                f"{p['active_max_forward_return_pct']:.2f}%"
            )
            retirements.append(
                Retirement(retired=active, winner=winner, reason=reason,
                           facts=_facts(active, winner, p))
            )
    return retirements
