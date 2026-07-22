"""Optional webhook alerts for events that need human attention."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("alerts")

LEVELS = {"warning": 1, "error": 2, "critical": 3}
DELIVERY_ATTEMPTS = 3
FAILED_ALERTS_FILE = (
    Path(__file__).resolve().parent.parent / "runtime" / "failed_alerts.jsonl"
)


class AlertManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg.get("alerts") or {}
        self.enabled = bool(self.cfg.get("enabled"))
        self.url = os.getenv(self.cfg.get("webhook_url_env", "ALERT_WEBHOOK_URL"), "")
        self.minimum = LEVELS.get(self.cfg.get("minimum_level", "error"), 2)
        self.timeout = float(self.cfg.get("timeout_seconds", 5))
        self.format = self.cfg.get("format", "generic")
        if self.enabled and not self.url:
            log.warning("alerts.enabled is true but %s is not set",
                        self.cfg.get("webhook_url_env", "ALERT_WEBHOOK_URL"))

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.url)

    def require_live_ready(self, probe: bool = False) -> None:
        if not self.ready:
            raise RuntimeError(
                "live trading requires alerts.enabled: true and a configured "
                f"{self.cfg.get('webhook_url_env', 'ALERT_WEBHOOK_URL')}")
        if probe and not self.send(
                "critical", "live_preflight",
                "OKX agent live alert delivery preflight"):
            raise RuntimeError(
                "live alert webhook did not acknowledge the preflight message")

    @staticmethod
    def _dead_letter(body: dict, error: Exception) -> None:
        record = dict(body)
        record["delivery_error"] = str(error)
        record["failed_at"] = int(time.time())
        try:
            FAILED_ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with FAILED_ALERTS_FILE.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            os.chmod(FAILED_ALERTS_FILE, 0o600)
        except Exception as exc:
            log.critical("could not persist failed alert %s: %s",
                         body.get("event"), exc)

    def send(self, level: str, event: str, message: str,
             details: dict | None = None) -> bool:
        if (not self.enabled or not self.url
                or LEVELS.get(level, 0) < self.minimum):
            return False
        body = {
            "level": level,
            "event": event,
            "message": message,
            "timestamp": int(time.time()),
            "details": details or {},
        }
        text = f"[{level.upper()}] {event}: {message}"
        if self.format == "slack":
            payload = {"text": text, "attachments": [{"text": json.dumps(body["details"])}]}
        elif self.format == "discord":
            payload = {"content": text + (f"\n```json\n{json.dumps(body['details'])}\n```"
                                          if body["details"] else "")}
        else:
            payload = body
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        last_error = RuntimeError("alert delivery did not run")
        for attempt in range(DELIVERY_ATTEMPTS):
            try:
                with urllib.request.urlopen(
                        request, timeout=self.timeout) as response:
                    if 200 <= int(response.status) < 300:
                        return True
                    last_error = RuntimeError(
                        f"webhook returned HTTP {response.status}")
            except Exception as exc:
                last_error = exc
            if attempt + 1 < DELIVERY_ATTEMPTS:
                time.sleep(0.5 * (attempt + 1))
        log.error("webhook alert failed for %s after %d attempts: %s",
                  event, DELIVERY_ATTEMPTS, last_error)
        self._dead_letter(body, last_error)
        return False
