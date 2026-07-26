"""Regression tests for the OKX trading agent.

The suite must never append synthetic fills/events to a real demo or live
journal. This environment is established before test modules import
``agent.state``.
"""

import os
import tempfile


_TEST_RUNTIME = tempfile.TemporaryDirectory(prefix="okx-agent-tests-")
os.environ["OKX_AGENT_RUNTIME_ROOT"] = _TEST_RUNTIME.name
os.environ["OKX_AGENT_RUNTIME_SCOPE"] = "test"
