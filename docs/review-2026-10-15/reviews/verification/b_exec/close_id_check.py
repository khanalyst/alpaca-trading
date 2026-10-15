import sys
sys.path.insert(0, "/home/user/alpaca-trading")
from types import SimpleNamespace
from agent.alpaca_provider import AlpacaProvider
from agent.market_entry_risk import MarketEntryRiskMixin

class Fake:
    def __init__(self, held_qty):
        self.held_qty = held_qty
        self.submitted = []
    def get_all_positions(self):
        return [{"symbol": "SPY", "asset_class": "us_equity", "qty": str(self.held_qty),
                 "side": "long", "market_value": "1000"}]
    def get_order_by_client_id(self, client_order_id):
        # yesterday's filled close that used the same deterministic id
        return {"id": "old-order-1", "client_order_id": client_order_id, "symbol": "SPY",
                "asset_class": "us_equity", "qty": "10", "filled_qty": "10",
                "side": "sell", "status": "filled", "type": "market",
                "time_in_force": "day", "filled_avg_price": "500.00"}
    def submit_order(self, req):
        self.submitted.append(req)
        raise AssertionError("broker submit reached")

# the id the runtime derives for a max-hold close on attempt 0
class Ids(MarketEntryRiskMixin):
    cfg = {"execution": {"client_order_id_prefix": "edge"}}
cid = Ids()._client_id("close", {"symbol": "SPY", "setup_id": "max_hold"}, 0)
cid_ff = Ids()._client_id("flatten", {"symbol": "SPY", "setup_id": "before_close"}, 0)
print("close id:", cid, "| flatten id:", cid_ff)
cfg = {"mode": "paper", "broker": {"paper": True, "allow_live": False, "data_feed": "iex",
       "options_feed": "indicative"}}
for held in (10, 12):
    fake = Fake(held)
    p = AlpacaProvider(cfg, trading_client=fake)
    try:
        out = p.close_position("SPY", qty=held, client_order_id=cid)
        print(f"held={held}: returned order id={out.id} status={out.status} (new submits: {len(fake.submitted)})")
    except Exception as exc:
        print(f"held={held}: {type(exc).__name__}: {exc} (new submits: {len(fake.submitted)})")
