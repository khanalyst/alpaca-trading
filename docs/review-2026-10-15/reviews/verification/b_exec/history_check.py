import sys
sys.path.insert(0, "/home/user/alpaca-trading")
from agent.alpaca_provider import AlpacaProvider
class Fake:
    def get_all_positions(self):
        return []
    def get_orders(self, *args, **kwargs):
        # one old, already-filled GTC order placed manually in the paper UI
        return [{"id": "manual-1", "client_order_id": "manual", "symbol": "SPY",
                 "asset_class": "us_equity", "qty": "1", "filled_qty": "1",
                 "side": "buy", "status": "filled", "type": "limit",
                 "time_in_force": "gtc", "limit_price": "500"}]
cfg = {"mode": "paper", "broker": {"paper": True, "allow_live": False, "data_feed": "iex",
       "options_feed": "indicative"}}
p = AlpacaProvider(cfg, trading_client=Fake())
try:
    p.reconcile()
    print("reconcile ok")
except Exception as exc:
    print(type(exc).__name__, exc)
