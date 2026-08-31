"""The agent's order-execution path — routed through the Alpaca MCP server (T3.2 / D7).

This is the sponsor integration and it must be visible in the demo, so orders go
through the **Alpaca MCP server**, not direct ``alpaca-py`` SDK calls. The SDK is
kept only for bulk historical bars (``data.py``), where MCP adds nothing.

How it works: we spawn the ``alpaca-mcp-server`` package as a subprocess speaking
MCP over stdio and drive it with an MCP client (``fastmcp.Client``). Every call
logs, at INFO, that it is going over the MCP path, which tool it is invoking, and
the raw tool result — so the MCP hop is demonstrable on the recording, not just
asserted.

Safety:
  * ``ALPACA_PAPER_TRADE=true`` is forced in the subprocess environment — the MCP
    server can only ever reach the paper endpoint from here.
  * ``DRY_RUN`` is honoured: when active, no subprocess is spawned and no order
    tool is called; a ``dry_run`` result is returned.
  * Risk checks (``risk.py``) run *before* this module is called; this module
    does not itself gate on position limits.

If the MCP server ever becomes impractical to drive this way, that is a
pitch-level change (it decides what we can claim) — do not silently fall back to
the SDK.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field

from dotenv import load_dotenv

log = logging.getLogger("mcp_client")

PLACE_ORDER_TOOL = "place_stock_order"
PLACE_OPTION_ORDER_TOOL = "place_option_order"
OPTION_CHAIN_TOOL = "get_option_chain"
ACCOUNT_TOOL = "get_account_info"
CLOSE_POSITION_TOOL = "close_position"
CLOCK_TOOL = "get_clock"
POSITIONS_TOOL = "get_all_positions"
ORDERS_TOOL = "get_orders"

# The MCP server has no ``__main__``; invoke its CLI entry point directly so this
# works regardless of how the console script is installed / on PATH.
_DEFAULT_CMD = [
    sys.executable, "-c", "from alpaca_mcp_server.cli import main; main()",
    "--transport", "stdio",
]


@dataclass
class OrderResult:
    ok: bool
    status: str                       # Alpaca order status, or 'dry_run' / 'error'
    broker_order_id: str | None = None
    symbol: str | None = None
    qty: float | None = None
    side: str | None = None
    raw: str = ""                     # raw MCP tool result text, for the audit row
    error: str | None = None
    via: str = "mcp"
    tools_seen: list[str] = field(default_factory=list)


def _server_env() -> dict:
    load_dotenv()
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set (.env)")
    return {
        **os.environ,
        "ALPACA_API_KEY": key,
        "ALPACA_SECRET_KEY": secret,
        "ALPACA_PAPER_TRADE": "true",          # paper only — never the live endpoint
        "FASTMCP_SHOW_SERVER_BANNER": "false",
    }


def _server_cmd() -> list[str]:
    override = os.getenv("ALPACA_MCP_CMD")
    if override:
        return override.split()
    return list(_DEFAULT_CMD)


def _unwrap(payload):
    """Strip the Alpaca MCP security envelope; return (data, error_message|None)."""
    if isinstance(payload, dict):
        if "error" in payload and isinstance(payload["error"], dict):
            return payload, payload["error"].get("message", "MCP tool returned an error")
        if "data" in payload:
            inner = payload["data"]
            if isinstance(inner, dict) and "error" in inner and isinstance(inner["error"], dict):
                return inner, inner["error"].get("message", "MCP tool returned an error")
            return inner, None
    return payload, None


def _result_text(result) -> str:
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            return text
    data = getattr(result, "structured_content", None) or getattr(result, "data", None)
    return json.dumps(data) if data is not None else ""


async def _acall(tool: str, args: dict) -> tuple[dict, str, list[str]]:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=_server_cmd()[0], args=_server_cmd()[1:], env=_server_env(),
    )
    log.info("→ Alpaca MCP server (stdio subprocess): %s", " ".join(_server_cmd()))
    async with Client(transport) as client:
        tools = [t.name for t in await client.list_tools()]
        log.info("  MCP session up — %d tools advertised; invoking %r", len(tools), tool)
        if tool not in tools:
            raise RuntimeError(f"MCP server does not expose {tool!r}")
        result = await client.call_tool(tool, args)
        raw = _result_text(result)
        log.info("  MCP tool %r returned: %s", tool, raw[:500])
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return payload, raw, tools


def _call(tool: str, args: dict) -> tuple[dict, str, list[str]]:
    """Synchronous wrapper around the async MCP round trip. Monkeypatched in tests."""
    return asyncio.run(_acall(tool, args))


def check_connection() -> dict:
    """Read-only round trip through the MCP path — for demos / health checks."""
    payload, _raw, tools = _call(ACCOUNT_TOOL, {})
    data, err = _unwrap(payload)
    if err:
        raise RuntimeError(f"MCP account check failed: {err}")
    log.info("MCP connection OK — paper account %s (%d tools)",
             data.get("account_number", "?"), len(tools))
    return data


def _as_list(data) -> list:
    """Normalise an MCP tool result to a list of dicts.

    The OpenAPI-derived tools return either a bare JSON array or an object that
    wraps it (``{"positions": [...]}`` / ``{"orders": [...]}`` / ``{"data": [...]}``).
    """
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        # The Alpaca MCP server wraps list results as {"data": {"result": [...]}}
        # (unwrapped to {"result": [...]} here); a few tools use other keys.
        for key in ("result", "results", "positions", "orders", "data"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
    return []


def market_clock() -> dict:
    """Current market status via the MCP server's ``get_clock`` tool.

    Returns ``{"is_open": bool, "next_open": str|None, "next_close": str|None}``.
    Raises ``RuntimeError`` if the MCP path fails — the scheduler treats that as
    "cannot confirm the market is open" and does not trade.
    """
    payload, _raw, _tools = _call(CLOCK_TOOL, {})
    data, err = _unwrap(payload)
    if err:
        raise RuntimeError(f"{CLOCK_TOOL} failed: {err}")
    if not isinstance(data, dict):
        raise RuntimeError(f"{CLOCK_TOOL} returned an unexpected shape: {type(data).__name__}")
    is_open = data.get("is_open")
    if isinstance(is_open, str):
        is_open = is_open.strip().lower() in ("true", "1", "yes")
    return {
        "is_open": bool(is_open),
        "next_open": data.get("next_open"),
        "next_close": data.get("next_close"),
        "timestamp": data.get("timestamp"),
    }


def list_positions() -> list[dict]:
    """Every open position on the paper account (``get_all_positions``)."""
    payload, _raw, _tools = _call(POSITIONS_TOOL, {})
    data, err = _unwrap(payload)
    if err:
        raise RuntimeError(f"{POSITIONS_TOOL} failed: {err}")
    return _as_list(data)


def list_open_orders() -> list[dict]:
    """Open (working) orders on the paper account (``get_orders`` status=open)."""
    payload, _raw, _tools = _call(ORDERS_TOOL, {"status": "open"})
    data, err = _unwrap(payload)
    if err:
        raise RuntimeError(f"{ORDERS_TOOL} failed: {err}")
    return _as_list(data)


def list_recent_orders(limit: int = 100) -> list[dict]:
    """Recent orders in any state (``get_orders`` status=all), newest first.

    Used by the scheduler to recover "when did we last trade?" from the broker
    after a DB wipe, so it does not re-run an entry cycle it has already done (D58).
    """
    payload, _raw, _tools = _call(
        ORDERS_TOOL, {"status": "all", "limit": str(limit), "direction": "desc"}
    )
    data, err = _unwrap(payload)
    if err:
        raise RuntimeError(f"{ORDERS_TOOL} failed: {err}")
    return _as_list(data)


def close_position(symbol: str, *, dry_run: bool | None = None) -> OrderResult:
    """Close the whole paper position in ``symbol`` via the MCP server (T4.3).

    Used when a retired strategy is still holding. ``dry_run`` (default: the
    ``DRY_RUN`` env var) spawns nothing and closes nothing. A "no open position"
    response from the server is treated as success — there is nothing to do.
    """
    import risk

    is_dry = risk.dry_run_active(dry_run)
    if is_dry:
        log.info("DRY_RUN — NOT closing the %s position through MCP", symbol)
        return OrderResult(ok=True, status="dry_run", symbol=symbol, side="sell",
                           raw="dry_run: no MCP call made")

    log.info("CLOSING POSITION VIA ALPACA MCP SERVER — tool=%s symbol=%s",
             CLOSE_POSITION_TOOL, symbol)
    try:
        payload, raw, tools = _call(CLOSE_POSITION_TOOL, {"symbol": symbol})
    except Exception as exc:  # noqa: BLE001 — surface, do not crash the retirement
        log.error("MCP close-position path failed: %s", exc)
        return OrderResult(ok=False, status="error", symbol=symbol, side="sell",
                           error=str(exc), raw="")

    data, err = _unwrap(payload)
    if err:
        if "no open position" in str(err).lower() or "position does not exist" in str(err).lower():
            log.info("MCP: no open %s position to close — nothing to do", symbol)
            return OrderResult(ok=True, status="no_position", symbol=symbol,
                               side="sell", raw=raw, tools_seen=tools)
        log.error("MCP close-position rejected: %s", err)
        return OrderResult(ok=False, status="error", symbol=symbol, side="sell",
                           error=err, raw=raw, tools_seen=tools)

    order_id = str(data.get("id")) if isinstance(data, dict) and data.get("id") else None
    status = str(data.get("status")) if isinstance(data, dict) and data.get("status") else "accepted"
    log.info("MCP POSITION CLOSE ACCEPTED — %s id=%s status=%s", symbol, order_id, status)
    return OrderResult(ok=True, status=status, broker_order_id=order_id, symbol=symbol,
                       side="sell", raw=raw, tools_seen=tools)


def option_chain(
    underlying: str,
    *,
    contract_type: str = "call",
    exp_gte: str | None = None,
    exp_lte: str | None = None,
    strike_gte: float | None = None,
    strike_lte: float | None = None,
    limit: int = 100,
) -> dict:
    """Fetch the option chain for ``underlying`` through the MCP server (read-only).

    Returns the ``snapshots`` dict keyed by OCC symbol. Raises ``RuntimeError`` if
    the MCP tool reports an error — the caller (``options.select_contract``)
    turns that into a "skipped, no contract" record rather than crashing.

    Strike/expiry bounds are passed through so the request is narrow: a full
    chain can be hundreds of contracts and the free feed paginates.
    """
    args: dict = {"underlying_symbol": underlying, "type": contract_type,
                  "limit": str(limit)}
    if exp_gte:
        args["expiration_date_gte"] = exp_gte
    if exp_lte:
        args["expiration_date_lte"] = exp_lte
    if strike_gte is not None:
        args["strike_price_gte"] = f"{strike_gte:.2f}"
    if strike_lte is not None:
        args["strike_price_lte"] = f"{strike_lte:.2f}"
    log.info("→ Alpaca MCP server: %s(%s)", OPTION_CHAIN_TOOL, args)
    payload, _raw, _tools = _call(OPTION_CHAIN_TOOL, args)
    data, err = _unwrap(payload)
    if err:
        raise RuntimeError(f"{OPTION_CHAIN_TOOL} failed: {err}")
    if isinstance(data, dict):
        snapshots = data.get("snapshots")
        if isinstance(snapshots, dict):
            return snapshots
        return data
    return {}


def submit_option_order(
    occ_symbol: str,
    *,
    qty: int,
    side: str = "buy",
    limit_price: float | None = None,
    dry_run: bool | None = None,
) -> OrderResult:
    """Submit ONE single-leg option order for ``occ_symbol`` via the MCP server (D48).

    ``qty`` is a number of contracts. Start with single-leg long calls; a
    defined-risk spread is a later, separate change.

    ``limit_price`` (per-share, not per-contract) makes this a **limit** order.
    Alpaca rejects options *market* orders outside market hours (code 42210000),
    and the demo must work at any hour (CLAUDE.md), so the cycle always passes a
    marketable limit (the contract's ask). Omitting it falls back to a market
    order — only usable while the market is open.

    ``dry_run`` defaults to the ``DRY_RUN`` env var; a dry run spawns nothing.
    """
    import risk

    is_dry = risk.dry_run_active(dry_run)
    if is_dry:
        log.info("DRY_RUN — NOT routing a %s %s x%s option order through MCP",
                 side, occ_symbol, qty)
        return OrderResult(
            ok=True, status="dry_run", symbol=occ_symbol, qty=float(qty), side=side,
            raw="dry_run: no MCP call made",
        )

    intent = "buy_to_open" if side == "buy" else "sell_to_close"
    args = {
        "symbol": occ_symbol, "side": side, "qty": str(qty),
        "time_in_force": "day", "position_intent": intent,
    }
    if limit_price is not None:
        args["type"] = "limit"
        args["limit_price"] = f"{float(limit_price):.2f}"
    else:
        args["type"] = "market"
    log.info("ROUTING OPTION ORDER VIA ALPACA MCP SERVER — tool=%s args=%s",
             PLACE_OPTION_ORDER_TOOL, args)
    try:
        payload, raw, tools = _call(PLACE_OPTION_ORDER_TOOL, args)
    except Exception as exc:                       # noqa: BLE001 — surface, do not crash the cycle
        log.error("MCP option order path failed: %s", exc)
        return OrderResult(ok=False, status="error", symbol=occ_symbol,
                           qty=float(qty), side=side, error=str(exc), raw="")

    data, err = _unwrap(payload)
    if err:
        log.error("MCP option order rejected: %s", err)
        return OrderResult(ok=False, status="error", symbol=occ_symbol,
                           qty=float(qty), side=side, error=err, raw=raw, tools_seen=tools)

    order_id = str(data.get("id")) if isinstance(data, dict) and data.get("id") else None
    status = str(data.get("status")) if isinstance(data, dict) and data.get("status") else "unknown"
    filled_qty = data.get("qty") if isinstance(data, dict) else None
    log.info("MCP OPTION ORDER ACCEPTED — id=%s status=%s (submitted via MCP)", order_id, status)
    return OrderResult(
        ok=True, status=status, broker_order_id=order_id, symbol=occ_symbol,
        qty=float(filled_qty) if filled_qty else float(qty), side=side, raw=raw, tools_seen=tools,
    )


def submit_market_order(
    symbol: str,
    *,
    qty: float | None = None,
    notional: float | None = None,
    side: str = "buy",
    dry_run: bool | None = None,
) -> OrderResult:
    """Submit ONE market order for ``symbol`` via the MCP server.

    Give exactly one of ``qty`` (shares) or ``notional`` (dollars). ``dry_run``
    defaults to the ``DRY_RUN`` env var; a dry run spawns nothing and submits
    nothing.
    """
    import risk

    if (qty is None) == (notional is None):
        raise ValueError("pass exactly one of qty or notional")

    is_dry = risk.dry_run_active(dry_run)
    sizing = f"{qty} shares" if qty is not None else f"${notional:,.2f} notional"
    if is_dry:
        log.info("DRY_RUN — NOT routing a %s %s (%s) order through MCP", side, symbol, sizing)
        return OrderResult(
            ok=True, status="dry_run", symbol=symbol,
            qty=float(qty) if qty is not None else None, side=side,
            raw="dry_run: no MCP call made",
        )

    args = {"symbol": symbol, "side": side, "type": "market", "time_in_force": "day"}
    if qty is not None:
        args["qty"] = str(qty)
    else:
        args["notional"] = str(notional)
    log.info("ROUTING ORDER VIA ALPACA MCP SERVER — tool=%s args=%s", PLACE_ORDER_TOOL, args)
    qty_val = float(qty) if qty is not None else None
    try:
        payload, raw, tools = _call(PLACE_ORDER_TOOL, args)
    except Exception as exc:                       # noqa: BLE001 — surface, do not crash the cycle
        log.error("MCP order path failed: %s", exc)
        return OrderResult(ok=False, status="error", symbol=symbol, qty=qty_val,
                           side=side, error=str(exc), raw="")

    data, err = _unwrap(payload)
    if err:
        log.error("MCP order rejected: %s", err)
        return OrderResult(ok=False, status="error", symbol=symbol, qty=qty_val,
                           side=side, error=err, raw=raw, tools_seen=tools)

    order_id = str(data.get("id")) if isinstance(data, dict) and data.get("id") else None
    status = str(data.get("status")) if isinstance(data, dict) and data.get("status") else "unknown"
    filled_qty = data.get("qty") if isinstance(data, dict) else None
    log.info("MCP ORDER ACCEPTED — id=%s status=%s (submitted via MCP)", order_id, status)
    return OrderResult(
        ok=True, status=status, broker_order_id=order_id, symbol=symbol,
        qty=float(filled_qty) if filled_qty else qty_val, side=side, raw=raw, tools_seen=tools,
    )
