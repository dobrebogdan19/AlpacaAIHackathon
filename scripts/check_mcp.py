"""Manual check: drive the Alpaca MCP server over stdio from our backend.

Not part of the test suite (it spawns the MCP server subprocess and hits Alpaca).
Run from the repo root:

    python scripts/check_mcp.py            # read-only: account info through MCP
    python scripts/check_mcp.py --order    # ALSO place a $1 notional AAPL paper buy

Requires ALPACA_API_KEY / ALPACA_SECRET_KEY in .env. Paper only.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> None:
    acct = mcp_client.check_connection()
    print(f"\nMCP account check OK: {acct.get('account_number')} "
          f"status={acct.get('status')} cash={acct.get('cash')}")

    if "--order" in sys.argv:
        res = mcp_client.submit_market_order("AAPL", notional=1.0, side="buy", dry_run=False)
        print(f"\norder ok={res.ok} status={res.status} id={res.broker_order_id} "
              f"(via {res.via})")
        print(f"raw: {res.raw[:400]}")


if __name__ == "__main__":
    main()
