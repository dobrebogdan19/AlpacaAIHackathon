import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

load_dotenv()

key = os.getenv("ALPACA_API_KEY")
secret = os.getenv("ALPACA_SECRET_KEY")
print("Key loaded:", bool(key), "| Secret loaded:", bool(secret))

client = TradingClient(key, secret, paper=True)
acct = client.get_account()
print("Status:", acct.status)
print("Cash:", acct.cash)
print("Portfolio value:", acct.portfolio_value)