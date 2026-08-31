"""Options contract selection — the *expression* layer (Phase 9 / D48).

The hackathon requires strategies to incorporate options. This project keeps the
split honest:

  * **Signal** — unchanged. The replay engine still evaluates every strategy on
    the underlying equity's daily bars (``engine.py``). A promotion is still a
    statement about the *underlying*.
  * **Expression** — new. When a promoted strategy fires, the agent no longer
    buys shares; it selects a single-leg long **call** on that underlying and
    executes the contract through the Alpaca MCP server.

We do **not** backtest option prices. Alpaca's options history is short and
modelling premium decay / IV would cost days we do not have. The backtest speaks
only in the underlying; the option is chosen live at execution time by the
explicit rules in ``SELECTION_RULES`` below. This is stated the same way in
DECISIONS.md and the README.

Data note: the free / paper options feed carries **no Greeks, no IV, and
``open_interest`` is null**. So contract selection is by **expiry window +
moneyness** (strike vs. the underlying's last close) — not by a delta target —
and tradeability is proxied by "a two-sided quote exists" rather than an
open-interest floor. Both limits are documented, not hidden.

Every selection records *why* the contract was chosen (or why none was), because
this project logs its reasoning everywhere and options are no exception.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import mcp_client

log = logging.getLogger("options")

# --- selection rules — the only knobs, all in one place (cf. gate.py) --------
SELECTION_RULES: dict[str, float | str | bool] = {
    "contract_type": "call",        # a long-only (bullish) signal -> a long call
    "dte_min": 30,                  # nearest acceptable expiry, in calendar days
    "dte_max": 45,                  # furthest acceptable expiry
    "target_moneyness": 1.03,       # strike / spot — ~3% out of the money
    "moneyness_tolerance": 0.08,    # accept strikes with |strike/spot - target| <= this
    "require_two_sided_quote": True,  # both bid and ask must be > 0 (tradeability proxy)
    "max_quote_spread_pct": 60.0,   # reject (ask-bid)/mid wider than this; options data is thin
    "chain_fetch_limit": 100,       # cap the chain request size
}

_CONTRACT_MULTIPLIER = 100          # shares per option contract (equity options are always 100)


@dataclass
class ContractChoice:
    """A contract the rules selected, with the reasoning that picked it."""

    occ_symbol: str        # OCC-21 symbol, e.g. "AAPL260925C00200000"
    underlying: str
    right: str             # "call" / "put"
    strike: float
    expiry: str            # ISO date
    dte: int               # calendar days to expiry at selection time
    spot: float            # underlying reference price used (last close)
    bid: float
    ask: float
    premium: float         # cash to open ONE contract = ask * 100 (= max risk for a long call)
    reason: str


@dataclass
class NoContract:
    """No contract cleared the rules. Not an error — the cycle records and moves on."""

    underlying: str
    reason: str


# --- OCC symbol parsing -----------------------------------------------------

def parse_occ(symbol: str) -> tuple[str, date, str, float]:
    """Split an OCC-21 option symbol into (root, expiry, right, strike).

    Layout: ``<root><YYMMDD><C|P><strike * 1000, zero-padded to 8>``. The root is
    variable length, so we peel the fixed 15-char tail off the end.
    """
    if len(symbol) < 16:
        raise ValueError(f"not an OCC option symbol: {symbol!r}")
    tail = symbol[-15:]
    root = symbol[:-15]
    yy, mm, dd = int(tail[0:2]), int(tail[2:4]), int(tail[4:6])
    right_code = tail[6].upper()
    if right_code not in ("C", "P"):
        raise ValueError(f"bad right in OCC symbol {symbol!r}: {right_code!r}")
    strike = int(tail[7:15]) / 1000.0
    expiry = date(2000 + yy, mm, dd)
    right = "call" if right_code == "C" else "put"
    return root, expiry, right, strike


# --- quote extraction -----------------------------------------------------

def _quote(snapshot: dict) -> tuple[float, float]:
    """(bid, ask) from a chain/snapshot entry; 0.0 where the field is absent."""
    q = snapshot.get("latestQuote") or snapshot.get("latest_quote") or {}
    bid = float(q.get("bp", q.get("bid_price", 0.0)) or 0.0)
    ask = float(q.get("ap", q.get("ask_price", 0.0)) or 0.0)
    return bid, ask


# --- selection ------------------------------------------------------------

def select_contract(
    underlying: str,
    spot: float,
    *,
    chain: dict | None = None,
    today: date | None = None,
    rules: dict | None = None,
) -> ContractChoice | NoContract:
    """Pick one contract for ``underlying`` given the current ``spot`` price.

    ``chain`` — a snapshots dict keyed by OCC symbol (as ``get_option_chain``
    returns). If omitted, it is fetched through the Alpaca MCP server. Passing it
    in keeps the selection logic unit-testable with no network.
    """
    r = {**SELECTION_RULES, **(rules or {})}
    today = today or date.today()
    ctype = str(r["contract_type"])
    exp_lo = today + timedelta(days=int(r["dte_min"]))
    exp_hi = today + timedelta(days=int(r["dte_max"]))

    if spot <= 0:
        return NoContract(underlying, f"invalid spot price {spot!r} for {underlying}")

    target = float(r["target_moneyness"])
    tol = float(r["moneyness_tolerance"])

    if chain is None:
        try:
            chain = mcp_client.option_chain(
                underlying,
                contract_type=ctype,
                exp_gte=exp_lo.isoformat(),
                exp_lte=exp_hi.isoformat(),
                strike_gte=spot * (target - tol),
                strike_lte=spot * (target + tol),
                limit=int(r["chain_fetch_limit"]),
            )
        except Exception as exc:  # noqa: BLE001 — a chain fetch failure is a skip, not a crash
            log.warning("option chain fetch for %s failed: %s", underlying, exc)
            return NoContract(underlying, f"option chain fetch failed: {exc}")

    if not chain:
        return NoContract(underlying, f"empty option chain for {underlying}")

    n_seen = 0
    rejected: list[str] = []
    candidates: list[tuple[float, ContractChoice]] = []

    for sym, snap in chain.items():
        try:
            _root, expiry, right, strike = parse_occ(sym)
        except ValueError:
            continue
        if right != ctype:
            continue
        n_seen += 1
        dte = (expiry - today).days
        if not (int(r["dte_min"]) <= dte <= int(r["dte_max"])):
            rejected.append(f"{sym}: {dte} DTE outside {r['dte_min']}-{r['dte_max']}")
            continue

        bid, ask = _quote(snap if isinstance(snap, dict) else {})
        if r["require_two_sided_quote"] and (bid <= 0 or ask <= 0):
            rejected.append(f"{sym}: no two-sided quote (bid={bid}, ask={ask})")
            continue
        mid = (bid + ask) / 2.0
        if mid > 0:
            spread_pct = (ask - bid) / mid * 100.0
            if spread_pct > float(r["max_quote_spread_pct"]):
                rejected.append(f"{sym}: quote spread {spread_pct:.0f}% too wide")
                continue

        moneyness = strike / spot
        dist = abs(moneyness - target)
        if dist > tol:
            rejected.append(f"{sym}: moneyness {moneyness:.3f} > {tol:.2f} from target {target:.2f}")
            continue

        candidates.append((dist, ContractChoice(
            occ_symbol=sym, underlying=underlying, right=right, strike=strike,
            expiry=expiry.isoformat(), dte=dte, spot=round(spot, 2),
            bid=round(bid, 2), ask=round(ask, 2),
            premium=round(ask * _CONTRACT_MULTIPLIER, 2),
            reason="",  # filled in below for the winner
        )))

    if not candidates:
        why = (f"no {ctype} on {underlying} cleared the rules "
               f"(spot ${spot:,.2f}; {n_seen} {ctype}(s) in chain; "
               f"want {r['dte_min']}-{r['dte_max']} DTE, a two-sided quote, and "
               f"moneyness within {tol:.2f} of {target:.2f}x)")
        if rejected:
            why += ". Nearest misses: " + "; ".join(rejected[:3])
        return NoContract(underlying, why)

    candidates.sort(key=lambda c: c[0])
    _dist, choice = candidates[0]
    moneyness = choice.strike / spot
    choice.reason = (
        f"long {choice.right} {choice.underlying} ${choice.strike:g} exp {choice.expiry} "
        f"({choice.dte} DTE): strike is {(moneyness - 1) * 100:+.1f}% vs spot ${spot:,.2f} "
        f"(target {(target - 1) * 100:+.0f}%), closest of {len(candidates)} qualifying "
        f"contract(s); quote ${choice.bid:.2f}/${choice.ask:.2f}, "
        f"~${choice.premium:,.2f} to open 1 contract"
    )
    log.info("selected %s — %s", choice.occ_symbol, choice.reason)
    return choice
