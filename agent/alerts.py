"""Optional webhook alerts for events that need human attention."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

log = logging.getLogger("alerts")

LEVELS = {"warning": 1, "error": 2, "critical": 3}


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
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return 200 <= int(response.status) < 300
        except Exception as exc:
            log.error("webhook alert failed for %s: %s", event, exc)
            return False
