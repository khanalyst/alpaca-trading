"""Truthful command exit semantics for operator-facing safety checks."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import uuid
import unittest
from unittest.mock import patch

import main
from agent.alpaca_domain import Account, MarketClock, Order, Position


class _Engine:
    def __init__(self, *, check=None, status=None, flatten=True,
                 resume=None, resume_error=None):
        self._check = check or {}
        self._status = status or {}
        self._flatten = flatten
        self._resume = resume if resume is not None else {"resumed": True}
        self._resume_error = resume_error
        self.closed = 0
        self.resume_calls = 0

    def check(self, authenticated=False):
        return {**self._check, "authenticated": authenticated}

    def status(self):
        return dict(self._status)

    def flatten_all(self, reason):
        return self._flatten

    def resume(self):
        self.resume_calls += 1
        if self._resume_error is not None:
            raise self._resume_error
        return self._resume

    def close(self):
        self.closed += 1


class _SmokeProvider:
    mode = "paper"
    paper = True

    def __init__(self, *, market_open=True, fail_first_close=False,
                 stale_open_after_exit=False):
        self.market_open = market_open
        self.fail_first_close = fail_first_close
        self.stale_open_after_exit = stale_open_after_exit
        self.close_calls = 0
        self.cancel_calls = 0
        self.open_order_checks = 0
        self.positions_live = []
        self.orders_by_client_id = {}
        self.requests = []

    def account(self):
        return Account("paper-account", "active", Decimal("100000"),
                       Decimal("100000"), Decimal("200000"))

    def clock(self):
        now = datetime(2026, 8, 14, 15, 0, tzinfo=timezone.utc)
        close = datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)
        return MarketClock(now, self.market_open, next_close=close)

    def positions(self):
        return list(self.positions_live)

    def orders(self, *, status=None, client_order_id=None):
        if client_order_id:
            order = self.orders_by_client_id.get(client_order_id)
            return [order] if order is not None else []
        if status == "open":
            self.open_order_checks += 1
            if (self.stale_open_after_exit and len(self.requests) >= 2 and
                    self.open_order_checks == 2):
                filled = self.orders_by_client_id[
                    self.requests[-1].client_order_id]
                return [Order(
                    filled.id, filled.symbol, filled.qty, filled.side,
                    "accepted", filled.type, filled.time_in_force,
                    client_order_id=filled.client_order_id)]
            return []
        return list(self.orders_by_client_id.values())

    def submit_order(self, request):
        self.requests.append(request)
        order = Order(
            f"order-{len(self.requests)}", request.symbol, request.qty,
            request.side, "filled", request.type, request.time_in_force,
            client_order_id=request.client_order_id,
            filled_qty=request.qty, filled_avg_price=Decimal("500"))
        self.orders_by_client_id[request.client_order_id] = order
        if request.side == "buy":
            self.positions_live = [Position(
                request.symbol, request.qty, "long")]
        else:
            self.positions_live = []
        return order

    def close_position(self, symbol, qty=None, *, client_order_id=None,
                       order_type="market", time_in_force="day"):
        self.close_calls += 1
        if self.fail_first_close and self.close_calls == 1:
            raise RuntimeError("injected close failure")
        held = next((position for position in self.positions_live
                     if position.symbol == symbol), None)
        if held is None:
            return None
        return self.submit_order(SimpleNamespace(
            symbol=symbol, qty=Decimal(str(qty or held.qty)), side="sell",
            type=order_type, time_in_force=time_in_force,
            client_order_id=client_order_id))

    def cancel_all_orders(self):
        self.cancel_calls += 1


class CliSafetyTests(unittest.TestCase):
    def test_checked_in_config_loads_without_yaml_only_syntax(self):
        cfg = main.load_cfg(Path(main.__file__).resolve().with_name("config.yaml"))
        self.assertEqual(cfg["mode"], "paper")
        self.assertTrue(cfg["research"]["require_validated_variant"])

    def test_dump_yaml_serializes_nested_uuid_values(self):
        account_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
        payload = {"account": {"id": account_id, "equity": Decimal("100000")},
                   "accounts": [account_id],
                   "by_id": {account_id: {"id": account_id}}}

        plain = main._plain(payload)
        dumped = main._dump_yaml(payload)

        self.assertEqual(plain["account"]["equity"], "100000")
        self.assertIn(str(account_id), dumped)
        self.assertIn("100000", dumped)
        self.assertNotIn("UUID(", dumped)

    def test_check_and_status_fail_when_required_edge_is_missing(self):
        engine = _Engine(
            check={"edge_required": True, "edge_ready": False},
            status={"edge_required": True, "edge_ready": False})
        with patch.object(main, "_engine", return_value=engine), \
                redirect_stdout(StringIO()):
            self.assertEqual(main.cmd_check(
                SimpleNamespace(authenticated=True), {}), 1)
            self.assertEqual(main.cmd_status(SimpleNamespace(), {}), 1)
        self.assertEqual(engine.closed, 2)

    def test_flatten_never_reports_success_with_residual_positions(self):
        with patch.object(main, "_engine", return_value=_Engine(flatten=False)), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()) as error:
            code = main.cmd_flatten(SimpleNamespace(reason="test"), {})
        self.assertEqual(code, 1)
        self.assertIn("residual", error.getvalue())

    def test_authenticated_check_is_default_and_offline_is_explicit(self):
        parsed = main.parser().parse_args(["check"])
        self.assertTrue(parsed.authenticated)
        parsed = main.parser().parse_args(["check", "--offline"])
        self.assertFalse(parsed.authenticated)

    def test_offline_check_never_constructs_engine_or_requires_edge(self):
        output = StringIO()
        cfg = {"mode": "paper", "research": {
            "enabled": True, "require_validated_variant": True}}
        with patch.object(main, "_engine") as factory, redirect_stdout(output):
            code = main.cmd_check(SimpleNamespace(authenticated=False), cfg)
        self.assertEqual(code, 0)
        factory.assert_not_called()
        self.assertIn("local_config_valid", output.getvalue())
        self.assertIn("edge_checked", output.getvalue())
        self.assertIn("true", output.getvalue())

    def test_resume_parser_is_authenticated_flat_only_without_force_or_offline(self):
        parsed = main.parser().parse_args(["resume"])
        self.assertEqual(parsed.command, "resume")
        self.assertNotIn("offline", vars(parsed))
        self.assertNotIn("force", vars(parsed))
        parser = main.parser()
        subparsers = next(action for action in parser._actions
                          if getattr(action, "choices", None))
        self.assertIn("authenticate", subparsers.choices["resume"].description.lower())
        self.assertIn("flat", subparsers.choices["resume"].description)

    def test_resume_success_prints_result_without_closing_control_engine(self):
        output = StringIO()
        engine = _Engine(resume={"action": "resume", "state": "PAUSED"})
        with patch.object(main, "_engine", return_value=engine), \
                redirect_stdout(output), redirect_stderr(StringIO()):
            code = main.cmd_resume(SimpleNamespace(), {})
        self.assertEqual(code, 0)
        self.assertEqual(engine.resume_calls, 1)
        self.assertEqual(engine.closed, 0)
        self.assertTrue("action: resume" in output.getvalue() or
                        '"action": "resume"' in output.getvalue())

    def test_resume_failure_is_structured_on_stderr_without_closing(self):
        error = StringIO()
        engine = _Engine(resume_error=RuntimeError("not flat"))
        with patch.object(main, "_engine", return_value=engine), \
                redirect_stdout(StringIO()), redirect_stderr(error):
            code = main.cmd_resume(SimpleNamespace(), {})
        self.assertEqual(code, 1)
        self.assertIn("resume failed: not flat", error.getvalue())
        self.assertEqual(engine.closed, 0)

    def test_resume_constructor_failure_is_structured_on_stderr(self):
        error = StringIO()
        with patch.object(main, "_engine",
                          side_effect=RuntimeError("provider unavailable")), \
                redirect_stdout(StringIO()), redirect_stderr(error):
            code = main.cmd_resume(SimpleNamespace(), {})
        self.assertEqual(code, 1)
        self.assertIn("resume failed: provider unavailable", error.getvalue())

    def test_paper_smoke_places_closes_and_proves_flat(self):
        provider = _SmokeProvider()
        output = StringIO()
        cfg = {"mode": "paper", "broker": {
            "paper": True, "allow_live": False}}
        args = SimpleNamespace(symbol="SPY", timeout=1.0, confirm="PAPER")
        with patch.object(main, "_provider", return_value=provider), \
                redirect_stdout(output), redirect_stderr(StringIO()):
            code = main.cmd_paper_smoke(args, cfg)

        self.assertEqual(code, 0)
        self.assertEqual([request.side for request in provider.requests],
                         ["buy", "sell"])
        self.assertTrue(provider.requests[0].client_order_id.startswith(
            "smoke-open-"))
        self.assertTrue(provider.requests[1].client_order_id.startswith(
            "smoke-close-"))
        self.assertEqual(provider.positions(), [])
        self.assertIn("paper-order-smoke.v1", output.getvalue())
        self.assertTrue("flat: true" in output.getvalue() or
                        '"flat": true' in output.getvalue())

    def test_paper_smoke_refuses_live_without_constructing_provider(self):
        cfg = {"mode": "live", "broker": {
            "paper": False, "allow_live": True}}
        args = SimpleNamespace(symbol="SPY", timeout=1.0, confirm="PAPER")
        with patch.object(main, "_provider") as factory, \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main.cmd_paper_smoke(args, cfg)
        self.assertEqual(code, 2)
        factory.assert_not_called()

    def test_paper_smoke_refuses_closed_market_without_order(self):
        provider = _SmokeProvider(market_open=False)
        cfg = {"mode": "paper", "broker": {
            "paper": True, "allow_live": False}}
        args = SimpleNamespace(symbol="SPY", timeout=1.0, confirm="PAPER")
        with patch.object(main, "_provider", return_value=provider), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()) as error:
            code = main.cmd_paper_smoke(args, cfg)
        self.assertEqual(code, 1)
        self.assertEqual(provider.requests, [])
        self.assertIn("market is closed", error.getvalue())

    def test_paper_smoke_failure_recovers_to_flat(self):
        provider = _SmokeProvider(fail_first_close=True)
        cfg = {"mode": "paper", "broker": {
            "paper": True, "allow_live": False}}
        args = SimpleNamespace(symbol="SPY", timeout=1.0, confirm="PAPER")
        with patch.object(main, "_provider", return_value=provider), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()) as error:
            code = main.cmd_paper_smoke(args, cfg)
        self.assertEqual(code, 1)
        self.assertEqual(provider.cancel_calls, 1)
        self.assertEqual(provider.close_calls, 2)
        self.assertEqual(provider.positions(), [])
        self.assertTrue("flat: true" in error.getvalue() or
                        '"flat": true' in error.getvalue())

    def test_paper_smoke_waits_for_stale_open_order_view_to_clear(self):
        provider = _SmokeProvider(stale_open_after_exit=True)
        cfg = {"mode": "paper", "broker": {
            "paper": True, "allow_live": False}}
        args = SimpleNamespace(symbol="SPY", timeout=1.0, confirm="PAPER")
        with patch.object(main, "_provider", return_value=provider), \
                patch.object(main.time, "sleep", return_value=None), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main.cmd_paper_smoke(args, cfg)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(provider.open_order_checks, 3)
        self.assertEqual(provider.positions(), [])

    def test_paper_smoke_never_mutates_preexisting_exposure(self):
        provider = _SmokeProvider()
        provider.positions_live = [Position("QQQ", Decimal("1"), "long")]
        cfg = {"mode": "paper", "broker": {
            "paper": True, "allow_live": False}}
        args = SimpleNamespace(symbol="SPY", timeout=1.0, confirm="PAPER")
        with patch.object(main, "_provider", return_value=provider), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()) as error:
            code = main.cmd_paper_smoke(args, cfg)
        self.assertEqual(code, 1)
        self.assertEqual(provider.cancel_calls, 0)
        self.assertEqual(provider.close_calls, 0)
        self.assertEqual(provider.positions_live[0].symbol, "QQQ")
        self.assertIn("must start flat", error.getvalue())

    def test_paper_smoke_refuses_when_trader_holds_runtime_lock(self):
        cfg = {"mode": "paper", "broker": {
            "paper": True, "allow_live": False}}
        args = SimpleNamespace(symbol="SPY", timeout=1.0, confirm="PAPER")
        with patch.object(main.state, "acquire_run_lock", return_value=None), \
                patch.object(main, "_provider") as factory, \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()) as error:
            code = main.cmd_paper_smoke(args, cfg)
        self.assertEqual(code, 1)
        factory.assert_not_called()
        self.assertIn("stop the trader", error.getvalue())


if __name__ == "__main__":
    unittest.main()
