"""Does a flatten-driven close get booked locally (trade row, outcome, active_trades)?"""
import sys, unittest
sys.path.insert(0, "/home/user/alpaca-trading")
from tests.test_broker_protection import ProtectionHarness, ProtectionProvider
from agent import state

class FlattenBooking(ProtectionHarness):
    def runTest(self):
        self._bind_engine(runtime_name="rt-flatten-book")
        order = self._open_bracketed_position()
        prov = self.provider
        st = state.load_state()
        print("before flatten: active_trades =", sorted(st["active_trades"]),
              "| journal trades =", self._journal_trades())
        def close_position(symbol, qty=None, **kw):
            res = ProtectionProvider.close_position(prov, symbol, qty, **kw)
            prov.set_order(res.id, status="filled", filled_qty=res.qty,
                           filled_avg_price=100.0)          # broker fills the flatten
            prov.positions_live = []                         # broker book is now flat
            return prov.orders_by_id[res.id]
        prov.close_position = close_position
        ok = self.engine._flatten_all_impl("run_exit")
        for i in range(3):
            self.engine.reconcile()
        st = state.load_state()
        print("flatten returned:", ok, "| broker positions:", prov.positions_live)
        print("after flatten + 3 reconciles: active_trades =", sorted(st["active_trades"]),
              "status =", {k: v.get("status") for k, v in st["active_trades"].items()})
        print("local orders =", {k: (v.get("action"), v.get("status")) for k, v in st["orders"].items()})
        print("journal trades =", self._journal_trades())
        print("edge_outbox rows =", len(st["edge_outbox"]))
        try:
            self.engine._resume_pause_invariants(
                {**st, "state": state.PAUSED, "operator_pause": True}, check_orders=True)
            print("resume invariants: pass")
        except Exception as exc:
            print("resume invariants:", type(exc).__name__, exc)

r = unittest.TextTestRunner(verbosity=0).run(FlattenBooking())
print("errors:", [str(e[1])[-400:] for e in r.errors], "failures:", [str(f[1])[-400:] for f in r.failures])
