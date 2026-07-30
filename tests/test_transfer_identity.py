import json
import unittest
from unittest.mock import Mock, patch

from agent.engine import Engine
from agent.exchange import Exchange, TransferBatch, TransferRecord


class ExchangeTransferIdentityTests(unittest.TestCase):
    def exchange(self, rows):
        exchange = Exchange.__new__(Exchange)
        exchange.x = Mock()
        exchange.x.fetch_ledger.return_value = rows
        exchange.retry = lambda fn, *args: fn(*args)
        return exchange

    def test_stable_okx_ids_are_deduplicated_exactly(self):
        rows = [
            {"id": "bill-1", "timestamp": 100, "type": "transfer",
             "direction": "in", "amount": 25, "info": {}},
            {"id": "bill-1", "timestamp": 100, "type": "transfer",
             "direction": "in", "amount": 25, "info": {}},
            {"timestamp": 101, "type": "transfer", "direction": "out",
             "amount": 4, "info": {"billId": "bill-2"}},
        ]

        batch = self.exchange(rows).transfers_since(100)

        self.assertEqual(batch.net_usdt, 21)
        self.assertEqual([row.transfer_id for row in batch.records],
                         ["okx:bill-1", "okx:bill-2"])
        repeated = self.exchange(rows).transfers_since(
            100, {"okx:bill-1", "okx:bill-2"})
        self.assertEqual(repeated.net_usdt, 0)
        self.assertEqual(repeated.records, ())

    def test_missing_id_is_explicitly_reconciliation_required(self):
        batch = self.exchange([{
            "timestamp": 100, "type": "transfer", "direction": "in",
            "amount": 25, "info": {},
        }]).transfers_since(100)

        self.assertEqual(batch.net_usdt, 25)
        record = batch.records[0]
        self.assertIsNone(record.transfer_id)
        self.assertTrue(record.reconciliation_required)
        self.assertEqual(
            record.as_event()["identity_status"],
            "reconciliation_required")


class EngineTransferIdentityTests(unittest.TestCase):
    @patch("agent.engine.state.log_event")
    def test_identified_and_unidentified_rows_are_persisted_separately(
            self, log_event):
        engine = Engine.__new__(Engine)
        engine.ex = Mock()
        engine.ex.transfers_since.return_value = TransferBatch(20, 102, (
            TransferRecord("okx:bill-1", 100, 25, False,
                           "ledger:100:in:25:0"),
            TransferRecord(None, 101, -5, True,
                           "ledger:101:out:5:1"),
        ))
        st = {
            "last_ledger_ts": 99,
            "processed_transfer_ids": {},
            "transfer_reconciliation_required": {},
            "high_water_mark": 1000,
            "day_start_equity": 900,
        }

        engine._sync_transfers(st, 200)

        self.assertEqual(st["processed_transfer_ids"], {"okx:bill-1": 100})
        self.assertIn("ledger:101:out:5:1",
                      st["transfer_reconciliation_required"])
        self.assertEqual(st["high_water_mark"], 1020)
        self.assertEqual(st["day_start_equity"], 920)
        payloads = [json.loads(call.args[1]) for call in log_event.call_args_list
                    if call.args[0] == "transfer"]
        self.assertEqual(len(payloads), 2)
        self.assertEqual(
            {payload["identity_status"] for payload in payloads},
            {"identified", "reconciliation_required"})


if __name__ == "__main__":
    unittest.main()
