"""After a flatten: does the monitor clean the phantom, and what happens to the next SPY trade?"""
import sys, unittest
from decimal import Decimal
sys.path.insert(0, "/home/user/alpaca-trading")
from tests.test_broker_protection import ProtectionHarness, ProtectionProvider
from agent import state
from agent.alpaca_domain import OrderRequest, Position

class Phantom(ProtectionHarness):
    def runTest(self):
        self._bind_engine(runtime_name="rt-phantom")
        first = self._open_bracketed_position()
        prov = self.provider
        orig_close = prov.close_position
        def close_position(symbol, qty=None, **kw):
            res = ProtectionProvider.close_position(prov, symbol, qty, **kw)
            prov.set_order(res.id, status="filled", filled_qty=res.qty, filled_avg_price=100.0)
            prov.positions_live = []
            return prov.orders_by_id[res.id]
        prov.close_position = close_position
        print("flatten:", self.engine._flatten_all_impl("before_close"))
        mon = self.engine._monitor_positions(self.NOW, [])
        self.engine.reconcile()
        st = state.load_state()
        print("monitor result:", mon)
        print("after monitor+reconcile active:", {k: (v.get("order_id"), v.get("status"), v.get("entry_price"))
                                                 for k, v in st["active_trades"].items()})
        # next day: a new SPY entry fills at 110 with fresh broker legs
        req = OrderRequest("SPY", Decimal("5"), "buy", client_order_id="entry-second",
                           order_class="bracket", take_profit=Decimal("112"),
                           stop_loss=Decimal("109"))
        second = prov.submit_order(req)
        plan = {"execution_profile": "shares", "direction": "long", "entry_price": 110.0,
                "stop_price": 109, "target_price": 112, "underlying_stop_price": 109,
                "underlying_target_price": 112, "underlying_symbol": "SPY",
                "contract_multiplier": Decimal("1"), "setup_id": "entry-second",
                "setup_type": "ibr", "risk_usd": 5, "notional": 550}
        self.engine._record_open_order(req, second, plan)
        prov.set_order(second.id, status="filled", filled_qty=5, filled_avg_price=110.0)
        prov.positions_live = [Position("SPY", Decimal("5"), "long",
                                        avg_entry_price=Decimal("110"),
                                        current_price=Decimal("110"))]
        self.engine.reconcile()
        st = state.load_state()
        t = st["active_trades"]["SPY"]
        print("second entry order id:", second.id)
        print("active SPY after second fill: order_id=%s entry_price=%s qty=%s setup_id=%s legs=%s"
              % (t.get("order_id"), t.get("entry_price"), t.get("qty"), t.get("setup_id"),
                 [(l.get("order_id"), l.get("status")) for l in t.get("protective_legs", [])]))
        print("broker legs for second entry:", [(o.id, o.status) for o in prov.orders_by_id.values() if o.id.startswith("order-3-")])
        n_before = len(prov.close_requests)
        mon = self.engine._monitor_positions(self.NOW, list(prov.positions_live))
        print("monitor on second trade:", mon, "| new close requests:",
              [(r.symbol, str(r.qty), r.client_order_id) for r in prov.close_requests[n_before:]])

r = unittest.TextTestRunner(verbosity=0).run(Phantom())
print("errors:", [str(e[1])[-600:] for e in r.errors], "failures:", [str(f[1])[-400:] for f in r.failures])
