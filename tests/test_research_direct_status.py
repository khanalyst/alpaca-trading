"""Exercise the real shell status handlers without starting research or a broker."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ResearchDirectStatusTests(unittest.TestCase):
    def run_status_handlers(self, body):
        source = (ROOT / "deploy/research-cycle.sh").read_text()
        prefix = source.split("# Load only provider keys", 1)[0]
        lines = prefix.splitlines()
        lines = ["repo_root=" + shlex.quote(str(ROOT)) if line.startswith("repo_root=")
                 else line for line in lines]
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "status.json"
            result = subprocess.run(["bash", "-c", "\n".join(lines) + "\n" + body],
                cwd=ROOT, env={**os.environ, "PYTHON": sys.executable,
                    "ALPACA_RESEARCH_DIRECT_HEARTBEAT_SECONDS": "1",
                    "ALPACA_RESEARCH_DIRECT_STATUS_FILE": str(status)},
                capture_output=True, text=True, timeout=15)
            return result, json.loads(status.read_text())

    def test_startup_is_visible_before_preflight(self):
        result, _ = self.run_status_handlers('cat "$direct_status_file"\ncycle_finalized=1\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        startup = json.loads(result.stdout)
        self.assertEqual(startup["status"], "running")
        self.assertEqual(startup["progress"]["phase"], "startup")

    def test_unexpected_abort_preserves_last_progress(self):
        result, final = self.run_status_handlers('emit_progress evaluating 2 12 arms equity\nexit 7\n')
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["progress"]["phase"], "evaluating")
        self.assertEqual((final["progress"]["done"], final["progress"]["total"]), (2, 12))

    def test_older_owner_cannot_overwrite_newer_job(self):
        result, final = self.run_status_handlers('''
direct_job_id=older-owner
direct_started_ts=1
write_direct_status failed failed 0 1 steps both failed older-failure 7
cycle_finalized=1
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(final["job_id"], "older-owner")
        self.assertEqual(final["status"], "running")


if __name__ == "__main__":
    unittest.main()
