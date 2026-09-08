"""Focused tests for bounded recorder snapshots."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from deploy.research_snapshot import (
    MANIFEST_NAME,
    SnapshotError,
    create_snapshot,
    verify_snapshot,
)


def _corpus(root: Path, count: int = 3) -> Path:
    (root / "sessions").mkdir(parents=True)
    (root / ".recorder-index.json").write_text(
        json.dumps({"schema": "recorder-index.v1", "partition_sources": {}}),
        encoding="utf-8")
    for number in range(count):
        day = f"2026-01-{number + 1:02d}"
        (root / "sessions" / f"market-{day}.csv").write_text(
            "event_type,symbol,timestamp\n"
            f"bar_1m,SPY,2026-01-{number + 1:02d}T14:30:00+00:00\n",
            encoding="utf-8")
    return root


class ResearchSnapshotTests(unittest.TestCase):
    def test_date_and_byte_limits_are_enforced_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _corpus(root / "recorder")
            with self.assertRaises(SnapshotError):
                create_snapshot(source, root / "large", session_window=3, max_bytes=1)
            self.assertFalse((root / "large").exists())
            result = create_snapshot(source, root / "snapshot", session_window=2,
                                     end_session="2026-01-02")
            self.assertEqual(result["partitions"], ["market-2026-01-01.csv", "market-2026-01-02.csv"])
            from deploy.recorder import corpus_write_lock
            with self.assertRaisesRegex(RuntimeError, "sealed"):
                with corpus_write_lock(root / "snapshot" / "market.csv"):
                    self.fail("recorder must refuse a sealed destination")

    def test_failed_copy_and_bad_sidecar_never_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _corpus(root / "recorder", 1)
            with patch("deploy.research_snapshot._copy_and_hash", side_effect=OSError("disk full")):
                with self.assertRaises(SnapshotError):
                    create_snapshot(source, root / "snapshot", session_window=1)
            self.assertFalse((root / "snapshot").exists())
            self.assertFalse(list(root.glob(".snapshot.staging-*")))
            (source / "sessions/market-2026-01-01.csv.source.json").write_text('{}')
            with self.assertRaises(SnapshotError):
                create_snapshot(source, root / "snapshot", session_window=1)
            self.assertFalse((root / "snapshot").exists())

    def test_snapshot_is_bounded_and_ignores_post_publish_append(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorder_root = _corpus(root / "recorder")
            snapshot_root = root / "snapshot"
            result = create_snapshot(recorder_root, snapshot_root, session_window=2)
            self.assertEqual(result["partitions"], [
                "market-2026-01-02.csv", "market-2026-01-03.csv"])
            before = (snapshot_root / "sessions" / "market-2026-01-03.csv").read_bytes()
            (recorder_root / "sessions" / "market-2026-01-03.csv").write_bytes(
                before + b"bar_1m,QQQ,2026-01-03T14:31:00+00:00\n")
            checked = verify_snapshot(snapshot_root)
            self.assertEqual(checked["identity"], result["identity"])
            self.assertEqual(
                (snapshot_root / "sessions" / "market-2026-01-03.csv").read_bytes(),
                before)

    def test_tampering_and_partial_staging_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorder_root = _corpus(root / "recorder", count=1)
            snapshot_root = root / "snapshot"
            create_snapshot(recorder_root, snapshot_root, session_window=1)
            partition = snapshot_root / "sessions" / "market-2026-01-01.csv"
            partition.chmod(0o644)  # deliberate operator tampering with a sealed file
            partition.write_bytes(partition.read_bytes() + b"tampered")
            with self.assertRaisesRegex(SnapshotError, "identity mismatch"):
                verify_snapshot(snapshot_root)

            staging = root / ".snapshot.staging-crashed"
            (staging / "sessions").mkdir(parents=True)
            (staging / "sessions" / "market-2026-01-01.csv").write_bytes(b"partial")
            self.assertFalse((staging / MANIFEST_NAME).exists())
            with self.assertRaises(SnapshotError):
                verify_snapshot(staging)

    def test_symlinked_partition_is_rejected_before_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorder_root = _corpus(root / "recorder", count=1)
            partition = recorder_root / "sessions" / "market-2026-01-01.csv"
            target = recorder_root / "sessions" / "real.csv"
            target.write_bytes(partition.read_bytes())
            partition.unlink()
            partition.symlink_to(target)
            with self.assertRaisesRegex(SnapshotError, "symlink"):
                create_snapshot(recorder_root, root / "snapshot", session_window=1)

    def test_create_waits_for_the_recorder_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorder_root = _corpus(root / "recorder", count=1)
            script = (
                "import sys,time\n"
                "from pathlib import Path\n"
                "from deploy.recorder import corpus_write_lock\n"
                "with corpus_write_lock(Path(sys.argv[1])):\n"
                "    time.sleep(1.0)\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(recorder_root / "market.csv")],
                cwd=Path(__file__).parents[1])
            try:
                time.sleep(0.15)
                started = time.monotonic()
                create_snapshot(recorder_root, root / "snapshot", session_window=1)
                self.assertGreaterEqual(time.monotonic() - started, 0.55)
            finally:
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
