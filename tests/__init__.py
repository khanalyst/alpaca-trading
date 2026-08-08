"""Regression tests for the Alpaca paper trading agent.

The suite writes only to an isolated paper journal. This environment is
established before test modules import ``agent.state``.
"""

import atexit
import os
import tempfile


_TEST_RUNTIME = tempfile.TemporaryDirectory(prefix="alpaca-agent-tests-")
atexit.register(_TEST_RUNTIME.cleanup)
os.environ["ALPACA_AGENT_RUNTIME_ROOT"] = _TEST_RUNTIME.name
os.environ["ALPACA_AGENT_RUNTIME_SCOPE"] = "paper"
