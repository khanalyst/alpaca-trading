import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import alerts
from agent.alerts import AlertManager, DELIVERY_ATTEMPTS
from tests.helpers import valid_config


class AlertSafetyTests(unittest.TestCase):
    def test_live_readiness_rejects_disabled_alerts(self):
        with self.assertRaisesRegex(RuntimeError, "live trading requires"):
            AlertManager(valid_config()).require_live_ready()

    def test_failed_delivery_is_retried_and_persisted(self):
        cfg = valid_config()
        cfg["alerts"]["enabled"] = True
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"ALERT_WEBHOOK_URL": "https://alerts.invalid"}), \
                patch.object(alerts, "FAILED_ALERTS_FILE",
                             Path(directory) / "failed.jsonl"), \
                patch("agent.alerts.urllib.request.urlopen",
                      side_effect=OSError("offline")) as urlopen, \
                patch("agent.alerts.time.sleep"):
            manager = AlertManager(cfg)
            self.assertFalse(manager.send(
                "critical", "test", "delivery must persist"))
            self.assertEqual(urlopen.call_count, DELIVERY_ATTEMPTS)
            dead_letters = alerts.FAILED_ALERTS_FILE.read_text()
            self.assertIn('"event":"test"', dead_letters)
            self.assertIn("offline", dead_letters)


if __name__ == "__main__":
    unittest.main()
