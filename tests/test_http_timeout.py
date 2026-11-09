"""Every Alpaca HTTP call gets a default timeout (review finding B-10)."""

import os
import unittest
from unittest.mock import patch

from agent import alpaca_session
from agent.alpaca_provider import SUBMIT_AMBIGUOUS, submit_failure_kind


class FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append(kwargs)
        return "ok"


class FakeClient:
    def __init__(self):
        self._session = FakeSession()


class HttpTimeoutTests(unittest.TestCase):
    def test_default_timeout_is_added_and_explicit_one_kept(self):
        client = alpaca_session.with_http_timeout(FakeClient())
        client._session.request("GET", "https://paper-api.alpaca.markets/v2/clock")
        client._session.request("GET", "https://paper-api.alpaca.markets/v2/clock", timeout=3)
        calls = client._session.calls
        self.assertEqual(calls[0]["timeout"], alpaca_session.DEFAULT_HTTP_TIMEOUT_SECONDS)
        self.assertEqual(calls[1]["timeout"], 3)

    def test_wrapping_twice_is_idempotent(self):
        client = alpaca_session.with_http_timeout(FakeClient())
        first = client._session.request
        alpaca_session.with_http_timeout(client)
        self.assertIs(client._session.request, first)

    def test_environment_override(self):
        with patch.dict(os.environ, {alpaca_session.HTTP_TIMEOUT_ENV: "12"}):
            self.assertEqual(alpaca_session.http_timeout(), (5.0, 12.0))
        with patch.dict(os.environ, {alpaca_session.HTTP_TIMEOUT_ENV: "nonsense"}):
            self.assertEqual(alpaca_session.http_timeout(),
                             alpaca_session.DEFAULT_HTTP_TIMEOUT_SECONDS)
        with patch.dict(os.environ, {alpaca_session.HTTP_TIMEOUT_ENV: "-1"}):
            self.assertEqual(alpaca_session.http_timeout(),
                             alpaca_session.DEFAULT_HTTP_TIMEOUT_SECONDS)

    def test_a_client_without_a_session_is_left_alone(self):
        marker = object()
        self.assertIs(alpaca_session.with_http_timeout(marker), marker)

    def test_a_timed_out_submission_is_ambiguous(self):
        import requests
        self.assertEqual(submit_failure_kind(requests.exceptions.ReadTimeout("slow")),
                         SUBMIT_AMBIGUOUS)


if __name__ == "__main__":
    unittest.main()
