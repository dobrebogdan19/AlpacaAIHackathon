"""Manual probe: can our paper account see option chains and place option orders,
and does the Alpaca MCP server expose the tools we would need?

Not part of the test suite (it spawns the MCP subprocess and hits Alpaca). Run
from the repo root:

    python scripts/check_options.py            # read-only: tools + account level + one chain
    python scripts/check_options.py --order     # ALSO place ONE 1-contract paper call buy

Requires ALPACA_API_KEY / ALPACA_SECRET_KEY in .env. Paper only.
"""

import asyncio
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

UNDERLYING = "AAPL"
WANTED_TOOLS = [
    "get_option_contracts",
    "get_option_chain",
    "get_option_snapshot",
    "get_option_latest_quote",
    "place_option_order",
    "close_position",
    "exercise_options_position",
]


async def _run(place_order: bool) -> None:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    cmd = mcp_client._server_cmd()
    transport = StdioTransport(command=cmd[0], args=cmd[1:], env=mcp_client._server_env())
    async with Client(transport) as client:
        tools = sorted(t.name for t in await client.list_tools())
        print(f"\nMCP tools advertised ({len(tools)})")
        print("\n=== option-related tools ===")
        for name in WANTED_TOOLS:
            print(f"  {'OK ' if name in tools else 'MISSING':8} {name}")
        opt_tools = [t for t in tools if "option" in t.lower()]
        print(f"\n  all tools containing 'option': {opt_tools}")

        async def call(tool: str, args: dict | None = None):
            if tool not in tools:
                return {"error": {"message": f"tool {tool} not advertised"}}
            res = await client.call_tool(tool, args or {})
            raw = mcp_client._result_text(res)
            try:
                return json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return {"raw": raw}

        # --- account options trading level ---
        cfg, _ = mcp_client._unwrap(await call("get_account_config") or {})
        print("\n=== account config (options level) ===")
        if isinstance(cfg, dict):
            for k in ("max_options_trading_level", "options_trading_level",
                      "options_buying_power", "trading_blocked"):
                if k in cfg:
                    print(f"  {k:28} {cfg[k]}")
        else:
            print(f"  {cfg}")

        # --- one option chain for the underlying ---
        exp_gte = (date.today() + timedelta(days=25)).isoformat()
        exp_lte = (date.today() + timedelta(days=55)).isoformat()
        chain, err = mcp_client._unwrap(
            await call("get_option_chain", {
                "underlying_symbol": UNDERLYING,
                "type": "call",
                "expiration_date_gte": exp_gte,
                "expiration_date_lte": exp_lte,
                "limit": 20,
            }) or {}
        )
        print(f"\n=== get_option_chain({UNDERLYING}, call, {exp_gte}..{exp_lte}) ===")
        if err:
            print(f"  ERROR: {err}")
        else:
            snapshots = chain.get("snapshots", chain) if isinstance(chain, dict) else chain
            keys = list(snapshots)[:8] if isinstance(snapshots, dict) else []
            print(f"  {len(snapshots) if hasattr(snapshots, '__len__') else '?'} contracts; "
                  f"sample OCC symbols: {keys}")
            if keys:
                print(f"  sample contract payload:\n{json.dumps(snapshots[keys[0]], indent=2)[:800]}")

        # --- contracts listing (trading API — has open_interest, close_price) ---
        contracts, err = mcp_client._unwrap(
            await call("get_option_contracts", {
                "underlying_symbols": UNDERLYING,
                "type": "call",
                "expiration_date_gte": exp_gte,
                "expiration_date_lte": exp_lte,
                "limit": 5,
            }) or {}
        )
        print(f"\n=== get_option_contracts({UNDERLYING}) ===")
        if err:
            print(f"  ERROR: {err}")
        else:
            rows = contracts.get("option_contracts", contracts) if isinstance(contracts, dict) else contracts
            print(f"  {len(rows) if hasattr(rows, '__len__') else '?'} contracts")
            if rows:
                print(f"  sample:\n{json.dumps(rows[0], indent=2)[:800]}")

        if not place_order:
            print("\n(pass --order to also place ONE 1-contract paper call buy)")
            return

        # --- place ONE single-leg long call via options.select_contract ---
        import options  # noqa: PLC0415

        # a rough spot: use the underlying's latest trade
        ssnap, _ = mcp_client._unwrap(await call("get_stock_snapshot", {"symbols": UNDERLYING}) or {})
        spot = 0.0
        try:
            spot = float(ssnap["snapshots"][UNDERLYING]["latestTrade"]["p"])
        except Exception:
            try:
                spot = float(ssnap[UNDERLYING]["latestTrade"]["p"])
            except Exception:
                spot = 0.0
        print(f"\nspot({UNDERLYING}) ~ {spot}")

        # fetch a narrow chain here (offline to select_contract — no nested event loop)
        r = options.SELECTION_RULES
        narrow, cerr = mcp_client._unwrap(await call("get_option_chain", {
            "underlying_symbol": UNDERLYING, "type": "call",
            "expiration_date_gte": (date.today() + timedelta(days=int(r["dte_min"]))).isoformat(),
            "expiration_date_lte": (date.today() + timedelta(days=int(r["dte_max"]))).isoformat(),
            "strike_price_gte": f"{spot * (float(r['target_moneyness']) - float(r['moneyness_tolerance'])):.2f}",
            "strike_price_lte": f"{spot * (float(r['target_moneyness']) + float(r['moneyness_tolerance'])):.2f}",
            "limit": "100",
        }) or {})
        chain_snaps = narrow.get("snapshots", {}) if isinstance(narrow, dict) else {}
        print(f"  narrow chain: {len(chain_snaps)} contract(s) near the money")
        choice = (options.select_contract(UNDERLYING, spot, chain=chain_snaps)
                  if spot else options.NoContract(UNDERLYING, "no spot"))
        print(f"\n=== options.select_contract -> {type(choice).__name__} ===")
        if isinstance(choice, options.NoContract):
            print(f"  {choice.reason}")
            return
        print(f"  {choice.reason}")
        print(f"\n=== place_option_order(buy 1 {choice.occ_symbol} limit {choice.ask}) ===")
        res, err = mcp_client._unwrap(
            await call("place_option_order", {
                "symbol": choice.occ_symbol, "side": "buy", "qty": "1",
                "type": "limit", "limit_price": f"{choice.ask:.2f}",
                "time_in_force": "day", "position_intent": "buy_to_open",
            }) or {}
        )
        if err:
            print(f"  ERROR: {err}")
        else:
            print(f"  {json.dumps(res, indent=2)[:800]}")


if __name__ == "__main__":
    asyncio.run(_run("--order" in sys.argv))
