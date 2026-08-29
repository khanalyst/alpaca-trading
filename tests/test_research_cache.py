"""Focused tests for the immutable research preprocessing cache."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from deploy import research_cache


def _identities(**overrides):
    values = {
        "source_identity": "sha256:" + hashlib.sha256(b"source").hexdigest(),
        "config_identity": "sha256:" + hashlib.sha256(b"config").hexdigest(),
        "code_identity": "sha256:" + hashlib.sha256(b"code").hexdigest(),
        "context_identity": "vehicles=equity;format=jsonl;schema=v1",
    }
    values.update(overrides)
    return values


class ResearchCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="research-cache-test-")
        self.base = Path(self.temporary.name)
        self.cache_root = self.base / "cache"
        self.cache = research_cache.ResearchArtifactCache(self.cache_root)

    def tearDown(self):
        self.temporary.cleanup()

    def _artifact(self, name: str, content: bytes) -> Path:
        path = self.base / name
        path.write_bytes(content)
        return path

    def test_key_binds_every_identity_and_canonicalizes_json_objects(self):
        base = _identities(
            context_identity={"format": "jsonl", "vehicles": ["equity"]})
        expected = research_cache.make_cache_key(**base)
        reordered = _identities(
            context_identity={"vehicles": ["equity"], "format": "jsonl"})
        self.assertEqual(research_cache.make_cache_key(**reordered), expected)

        replacements = {
            "source_identity": "sha256:" + "1" * 64,
            "config_identity": "sha256:" + "2" * 64,
            "code_identity": "sha256:" + "3" * 64,
            "context_identity": {"format": "csv", "vehicles": ["equity"]},
        }
        for field, value in replacements.items():
            with self.subTest(field=field):
                changed = dict(base)
                changed[field] = value
                self.assertNotEqual(
                    research_cache.make_cache_key(**changed), expected)

    def test_cache_use_rejects_missing_or_blank_explicit_identities(self):
        for field in _identities():
            for invalid in (None, "   "):
                with self.subTest(field=field, invalid=invalid):
                    identities = _identities(**{field: invalid})
                    with self.assertRaises(research_cache.CacheIdentityError):
                        self.cache.lookup(**identities)
        self.assertFalse(self.cache_root.exists())

    def test_publish_lookup_and_materialize_validate_complete_bundle(self):
        normalized = self._artifact("normalized.jsonl", b'{"kind":"bar"}\n')
        empty_options = self._artifact("options.jsonl", b"")
        published = self.cache.publish(
            {"normalized": normalized, "options": empty_options},
            **_identities())

        self.assertEqual(published["status"], "published")
        self.assertTrue(published["published"])
        self.assertEqual(
            published["artifacts"]["normalized"]["sha256"],
            hashlib.sha256(normalized.read_bytes()).hexdigest())
        self.assertEqual(published["artifacts"]["options"]["size"], 0)

        output_normalized = self.base / "cycle" / "market.jsonl"
        output_options = self.base / "cycle" / "options.jsonl"
        hit = self.cache.lookup(
            **_identities(),
            materialize={
                "normalized": output_normalized,
                "options": output_options,
            })
        self.assertEqual(hit["status"], "hit")
        self.assertTrue(hit["hit"])
        self.assertEqual(output_normalized.read_bytes(), normalized.read_bytes())
        self.assertEqual(output_options.read_bytes(), b"")
        self.assertTrue(hit["artifacts"]["normalized"]["materialized"])
        self.assertEqual(
            hit["artifacts"]["normalized"]["path"], str(output_normalized))

    def test_default_publish_copies_and_keeps_source(self):
        artifact = self._artifact("copied.jsonl", b"copy-me\n")

        published = self.cache.publish(
            {"view": artifact}, **_identities())

        self.assertEqual(published["status"], "published")
        self.assertTrue(artifact.is_file())
        self.assertEqual(artifact.read_bytes(), b"copy-me\n")

    def test_consume_publish_moves_sources_and_lookup_works(self):
        artifact = self._artifact("consumed.jsonl", b"consume-me\n")
        expected_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

        published = self.cache.publish(
            {"view": artifact}, **_identities(), consume_artifacts=True)

        self.assertEqual(published["status"], "published")
        self.assertFalse(artifact.exists())
        cached = Path(published["artifacts"]["view"]["path"])
        self.assertEqual(cached.read_bytes(), b"consume-me\n")
        self.assertEqual(published["artifacts"]["view"]["sha256"], expected_digest)
        self.assertTrue(self.cache.lookup(**_identities())["hit"])

    def test_existing_entry_does_not_consume_source(self):
        first_source = self._artifact("first.jsonl", b"first\n")
        self.cache.publish({"view": first_source}, **_identities())
        second_source = self._artifact("second.jsonl", b"second\n")

        result = self.cache.publish(
            {"view": second_source}, **_identities(),
            consume_artifacts=True)

        self.assertEqual(result["status"], "existing")
        self.assertFalse(result["published"])
        self.assertTrue(second_source.is_file())
        self.assertEqual(second_source.read_bytes(), b"second\n")

    def test_consume_cross_filesystem_failure_is_explicit_and_no_entry(self):
        artifact = self._artifact("cross-device.jsonl", b"cannot-copy\n")
        key = research_cache.make_cache_key(**_identities())

        def raise_cross_device(*_args, **_kwargs):
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        with mock.patch.object(
                research_cache.os, "replace", side_effect=raise_cross_device):
            with self.assertRaisesRegex(
                    research_cache.CacheArtifactError, "across filesystems"):
                self.cache.publish(
                    {"view": artifact}, **_identities(),
                    consume_artifacts=True)

        self.assertTrue(artifact.is_file())
        self.assertFalse((self.cache.entries / key).exists())
        self.assertEqual(list(self.cache.staging.iterdir()), [])

    def test_rename_domain_probe_succeeds_and_cleans_probe_files(self):
        tmp_root = self.base / "tmp"
        tmp_root.mkdir()
        staging_root = self.cache_root / "staging"

        result = research_cache.probe_rename_domain(tmp_root, staging_root)

        self.assertEqual(result["schema"],
                         "research-preprocessing-cache-topology.v1")
        self.assertEqual(result["operation"], "rename_probe")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(list(tmp_root.iterdir()), [])
        self.assertEqual(list(staging_root.iterdir()), [])

    def test_rename_domain_probe_exdev_is_explicit_and_cleans_probe_files(self):
        tmp_root = self.base / "tmp"
        tmp_root.mkdir()
        staging_root = self.cache_root / "staging"

        def raise_cross_device(*_args, **_kwargs):
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        with mock.patch.object(
                research_cache.os, "replace", side_effect=raise_cross_device):
            with self.assertRaisesRegex(
                    research_cache.CacheTopologyError,
                    r"rename-capable mount \(EXDEV\)"):
                research_cache.probe_rename_domain(tmp_root, staging_root)

        self.assertEqual(list(tmp_root.iterdir()), [])
        self.assertEqual(list(staging_root.iterdir()), [])

    def test_cli_topology_probe_emits_structured_result(self):
        tmp_root = self.base / "tmp"
        tmp_root.mkdir()
        staging_root = self.cache_root / "staging"
        script = Path(research_cache.__file__).resolve()

        result = subprocess.run(
            [sys.executable, str(script), "topology",
             "--tmp-root", str(tmp_root),
             "--staging-root", str(staging_root)],
            text=True, capture_output=True, check=False)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "ok")
        self.assertEqual(list(tmp_root.iterdir()), [])
        self.assertEqual(list(staging_root.iterdir()), [])

    def test_valid_entry_is_immutable_and_publish_never_overwrites_it(self):
        artifact = self._artifact("view.jsonl", b"first\n")
        first = self.cache.publish({"view": artifact}, **_identities())
        artifact.write_bytes(b"second\n")

        second = self.cache.publish({"view": artifact}, **_identities())
        self.assertEqual(second["status"], "existing")
        self.assertFalse(second["published"])
        self.assertEqual(second["key"], first["key"])

        restored = self.base / "restored.jsonl"
        hit = self.cache.lookup(
            **_identities(), materialize={"view": restored})
        self.assertTrue(hit["hit"])
        self.assertEqual(restored.read_bytes(), b"first\n")

    def test_same_size_sha256_corruption_is_quarantined_then_rebuilt(self):
        artifact = self._artifact("view.jsonl", b"original")
        published = self.cache.publish({"view": artifact}, **_identities())
        cached_path = Path(published["artifacts"]["view"]["path"])
        os.chmod(cached_path, 0o600)
        cached_path.write_bytes(b"tampered")
        self.assertEqual(len(b"original"), len(b"tampered"))

        miss = self.cache.lookup(**_identities())
        self.assertEqual(miss["status"], "miss")
        self.assertEqual(miss["reason"], "corrupt_or_partial")
        self.assertEqual(
            miss["validation_error"], "artifact_sha256_mismatch:view")
        self.assertIsNotNone(miss["quarantined"])
        self.assertTrue(Path(miss["quarantined"]).is_dir())
        self.assertFalse((self.cache.entries / published["key"]).exists())

        artifact.write_bytes(b"rebuilt!")
        rebuilt = self.cache.publish({"view": artifact}, **_identities())
        self.assertEqual(rebuilt["status"], "published")
        restored = self.base / "rebuilt.jsonl"
        self.assertTrue(self.cache.lookup(
            **_identities(), materialize={"view": restored})["hit"])
        self.assertEqual(restored.read_bytes(), b"rebuilt!")

    def test_partial_entry_is_a_safe_miss_and_is_quarantined(self):
        key = research_cache.make_cache_key(**_identities())
        partial = self.cache_root / "entries" / key
        partial.mkdir(parents=True)
        (partial / "unfinished").write_text("partial", encoding="utf-8")

        result = self.cache.lookup(**_identities())
        self.assertFalse(result["hit"])
        self.assertEqual(result["reason"], "corrupt_or_partial")
        self.assertIsNotNone(result["quarantined"])
        self.assertFalse(partial.exists())
        self.assertTrue(Path(result["quarantined"]).exists())

    def test_failed_publish_never_exposes_a_partial_entry(self):
        missing = self.base / "does-not-exist.jsonl"
        key = research_cache.make_cache_key(**_identities())
        with self.assertRaises(research_cache.CacheArtifactError):
            self.cache.publish({"view": missing}, **_identities())

        self.assertFalse((self.cache.entries / key).exists())
        self.assertEqual(list(self.cache.staging.iterdir()), [])
        self.assertEqual(self.cache.lookup(**_identities())["status"], "miss")

    def test_filesystem_key_lock_serializes_another_process(self):
        artifact = self._artifact("locked.jsonl", b"locked\n")
        published = self.cache.publish({"view": artifact}, **_identities())
        lock_path = self.cache.locks / f"{published['key']}.lock"
        descriptor = os.open(lock_path, os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        script = Path(research_cache.__file__).resolve()
        command = [
            sys.executable, str(script), "lookup",
            "--cache-root", str(self.cache_root),
            "--source-identity", _identities()["source_identity"],
            "--config-identity", _identities()["config_identity"],
            "--code-identity", _identities()["code_identity"],
            "--context-identity", _identities()["context_identity"],
        ]
        child = subprocess.Popen(
            command, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                child.wait(timeout=0.1)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        stdout, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, stdout + stderr)
        self.assertEqual(json.loads(stdout)["status"], "hit")

    def test_cli_emits_structured_json_for_error_miss_publish_and_hit(self):
        script = Path(research_cache.__file__).resolve()
        common = [
            "--cache-root", str(self.cache_root),
            "--config-identity", _identities()["config_identity"],
            "--code-identity", _identities()["code_identity"],
            "--context-identity", _identities()["context_identity"],
        ]
        missing_identity = subprocess.run(
            [sys.executable, str(script), "lookup", *common],
            text=True, capture_output=True, check=False)
        self.assertEqual(missing_identity.returncode, 2)
        error = json.loads(missing_identity.stdout)
        self.assertEqual(error["status"], "error")
        self.assertEqual(error["error"], "invalid_identity")

        source_args = [
            "--source-identity", _identities()["source_identity"], *common]
        miss = subprocess.run(
            [sys.executable, str(script), "lookup", *source_args],
            text=True, capture_output=True, check=False)
        self.assertEqual(miss.returncode, 1)
        self.assertEqual(json.loads(miss.stdout)["status"], "miss")

        artifact = self._artifact("cli-view.jsonl", b"cli\n")
        publish = subprocess.run(
            [sys.executable, str(script), "publish", *source_args,
             "--artifact", f"view={artifact}"],
            text=True, capture_output=True, check=False)
        self.assertEqual(publish.returncode, 0, publish.stdout + publish.stderr)
        self.assertEqual(json.loads(publish.stdout)["status"], "published")

        materialized = self.base / "cli-output.jsonl"
        hit = subprocess.run(
            [sys.executable, str(script), "lookup", *source_args,
             "--materialize", f"view={materialized}"],
            text=True, capture_output=True, check=False)
        self.assertEqual(hit.returncode, 0, hit.stdout + hit.stderr)
        self.assertEqual(json.loads(hit.stdout)["status"], "hit")
        self.assertEqual(materialized.read_bytes(), b"cli\n")

    def test_cli_consume_artifacts_removes_source(self):
        script = Path(research_cache.__file__).resolve()
        artifact = self._artifact("cli-consumed.jsonl", b"cli-consume\n")
        identities = _identities(source_identity="cli-consume-source")
        command = [
            sys.executable, str(script), "publish",
            "--cache-root", str(self.cache_root),
            "--source-identity", identities["source_identity"],
            "--config-identity", identities["config_identity"],
            "--code-identity", identities["code_identity"],
            "--context-identity", identities["context_identity"],
            "--artifact", f"view={artifact}",
            "--consume-artifacts",
        ]

        result = subprocess.run(
            command, text=True, capture_output=True, check=False)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "published")
        self.assertFalse(artifact.exists())


if __name__ == "__main__":
    unittest.main()
