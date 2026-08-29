"""Ad-hoc readout of the Alpaca paper account, through the MCP path.

Not part of the suite and not wired into the app — a manual probe, like
``check_mcp.py``. Spawns the Alpaca MCP server over stdio, lists its tools, and
prints account balances + open positions with unrealised P&L.

    python scripts/check_account.py

Requires ALPACA_API_KEY / ALPACA_SECRET_KEY in .env. Paper only.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


async def _run() -> None:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    cmd = mcp_client._server_cmd()
    transport = StdioTransport(command=cmd[0], args=cmd[1:], env=mcp_client._server_env())
    async with Client(transport) as client:
        tools = sorted(t.name for t in await client.list_tools())
        print(f"\nMCP tools advertised ({len(tools)}):\n  " + ", ".join(tools))

        async def call(tool: str, args: dict | None = None):
            if tool not in tools:
                return None
            res = await client.call_tool(tool, args or {})
            raw = mcp_client._result_text(res)
            try:
                return json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return {"raw": raw}

        acct, _ = mcp_client._unwrap(await call("get_account_info") or {})
        print("\n=== account ===")
        for k in ("account_number", "status", "currency", "cash", "buying_power",
                  "portfolio_value", "equity", "long_market_value"):
            if isinstance(acct, dict) and k in acct:
                print(f"  {k:20} {acct[k]}")

        pos_tool = next((t for t in ("get_positions", "get_all_positions",
                                     "list_positions", "get_open_positions") if t in tools), None)
        positions, _ = mcp_client._unwrap(await call(pos_tool) or {}) if pos_tool else ([], None)
        rows = positions if isinstance(positions, list) else positions.get("positions", []) \
            if isinstance(positions, dict) else []
        print(f"\n=== open positions ({len(rows)}) ===")
        if not rows:
            print("  (none)")
        for p in rows:
            print(f"  {p.get('symbol'):6} qty={p.get('qty')}  "
                  f"mkt_value={p.get('market_value')}  "
                  f"avg_entry={p.get('avg_entry_price')}  "
                  f"unrealized_pl={p.get('unrealized_pl')} "
                  f"({p.get('unrealized_plpc')})")


if __name__ == "__main__":
    asyncio.run(_run())
