"""Deployable-artifact identity is deterministic and fail-closed."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import config as config_mod, provider, state
from research import artifact, findings


class ArtifactManifestTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "agent" / "contracts").mkdir(parents=True)
        (self.root / "research").mkdir(parents=True)
        (self.root / "main.py").write_text("print('runtime')\n", encoding="utf-8")
        (self.root / "agent" / "engine.py").write_text(
            "ENGINE = 1\n", encoding="utf-8")
        (self.root / "agent" / "contracts" / "momentum.py").write_text(
            "CONTRACT = 1\n", encoding="utf-8")
        (self.root / "research" / "artifact.py").write_text(
            "ARTIFACT_RUNTIME = 1\n", encoding="utf-8")
        (self.root / "research" / "findings.py").write_text(
            "FINDINGS_RUNTIME = 1\n", encoding="utf-8")

    def _build(self, *, config=None, definition=None, prompt_hash=None,
               prompt_inputs_hash=None):
        definition = definition or {
            "variant_id": "momentum.baseline",
            "strategy_id": "momentum",
            "base_version": "phase1-v3",
        }
        variant_hash = artifact.sha256_json(definition)
        config = config or {
            "strategy": {
                "id": "momentum", "version": "phase1-v3",
                "fixed_reward_risk": 2.0,
            },
        }
        config.setdefault(
            "llm", {"provider": "anthropic", "model": "test-model"})
        return artifact.build_manifest(
            strategy_id="momentum",
            strategy_version="phase1-v3",
            variant_id="momentum.baseline",
            variant_definition_hash=variant_hash,
            variant_definition=definition,
            config=config,
            strategy_config_version="b" * 16,
            forward_model_id="momentum.fixed_rr.15m.v2",
            forward_model_assumptions_hash="c" * 64,
            prompt_hash=prompt_hash or "d" * 64,
            llm_provider="anthropic",
            llm_model="test-model",
            prompt_inputs_hash=prompt_inputs_hash or "e" * 64,
            deployment_config_hash=state.deployment_config_hash(config),
            root=self.root,
        )

    def test_canonical_manifest_and_hash_are_deterministic(self):
        first = self._build(config={
            "strategy": {"id": "momentum", "version": "phase1-v3"},
            "b": 2, "a": [1, 2],
            "llm": {"provider": "anthropic", "model": "test-model"}})
        second = self._build(config={
            "a": [1, 2], "b": 2,
            "strategy": {"version": "phase1-v3", "id": "momentum"},
            "llm": {"provider": "anthropic", "model": "test-model"}})
        self.assertEqual(first, second)
        manifest, digest = first
        self.assertEqual(manifest["schema"], artifact.ARTIFACT_SCHEMA)
        self.assertEqual(len(digest), 64)
        self.assertEqual(artifact.artifact_sha256(manifest), digest)
        config = {
            "strategy": {"id": "momentum", "version": "phase1-v3"},
            "b": 2, "a": [1, 2],
            "llm": {"provider": "anthropic", "model": "test-model"},
        }
        self.assertEqual(
            artifact.validate_manifest(
                manifest, digest, config=config,
                root=self.root)["runtime_source_hash"],
            manifest["runtime_source_hash"],
        )

    def test_config_and_variant_drift_changes_artifact(self):
        _, original = self._build()
        _, changed_config = self._build(
            config={"strategy": {
                "id": "momentum", "version": "phase1-v3",
                "fixed_reward_risk": 2.5,
            }, "llm": {"provider": "anthropic", "model": "test-model"}})
        _, changed_variant = self._build(
            definition={
                "variant_id": "momentum.baseline",
                "strategy_id": "momentum",
                "base_version": "phase1-v3",
                "drift": True,
            })
        self.assertNotEqual(original, changed_config)
        self.assertNotEqual(original, changed_variant)

    def test_deployment_hash_normalizes_demo_evidence_to_live(self):
        demo = {
            "mode": "demo",
            "strategy": {"id": "momentum", "version": "phase1-v3"},
            "llm": {"provider": "anthropic", "model": "test-model"},
        }
        live = dict(demo)
        live["mode"] = "live"
        demo_manifest, _ = self._build(config=demo)
        live_manifest, _ = self._build(config=live)
        self.assertEqual(
            demo_manifest["deployment_config_hash"],
            live_manifest["deployment_config_hash"])
        self.assertNotEqual(
            demo_manifest["validated_config_hash"],
            live_manifest["validated_config_hash"])
        drift = dict(live)
        drift["strategy"] = dict(live["strategy"])
        drift["strategy"]["fixed_reward_risk"] = 2.5
        drift_manifest, _ = self._build(config=drift)
        self.assertNotEqual(
            live_manifest["deployment_config_hash"],
            drift_manifest["deployment_config_hash"])

    def test_runtime_and_contract_source_drift_invalidates_manifest(self):
        manifest, digest = self._build()
        (self.root / "agent" / "contracts" / "momentum.py").write_text(
            "CONTRACT = 2\n", encoding="utf-8")
        with self.assertRaises(artifact.ArtifactValidationError):
            artifact.validate_manifest(
                manifest, digest, config={"strategy": {
                    "id": "momentum", "version": "phase1-v3",
                    "fixed_reward_risk": 2.0,
                }, "llm": {"provider": "anthropic", "model": "test-model"}},
                root=self.root)

    def test_artifact_and_findings_source_drift_invalidates_manifest(self):
        for relative in ("research/artifact.py", "research/findings.py"):
            with self.subTest(relative=relative):
                manifest, digest = self._build()
                path = self.root / relative
                path.write_text(path.read_text() + "\nDRIFT = 2\n",
                                encoding="utf-8")
                with self.assertRaises(artifact.ArtifactValidationError):
                    artifact.validate_manifest(
                        manifest, digest, config={"strategy": {
                            "id": "momentum", "version": "phase1-v3",
                            "fixed_reward_risk": 2.0,
                        }, "llm": {"provider": "anthropic",
                                  "model": "test-model"}},
                        root=self.root)

    def test_unrelated_research_source_does_not_change_runtime_identity(self):
        manifest, digest = self._build()
        (self.root / "research" / "offline_report.py").write_text(
            "OFFLINE_ONLY = True\n", encoding="utf-8")
        artifact.validate_manifest(
            manifest, digest, config={"strategy": {
                "id": "momentum", "version": "phase1-v3",
                "fixed_reward_risk": 2.0,
            }, "llm": {"provider": "anthropic", "model": "test-model"}},
            root=self.root)

    def test_runtime_source_scope_is_versioned_and_exact(self):
        manifest, digest = self._build()
        self.assertEqual(
            manifest["runtime_source_scope"],
            list(artifact.RUNTIME_SOURCE_SCOPE))
        self.assertEqual(
            manifest["runtime_source_scope_version"],
            artifact.RUNTIME_SOURCE_SCOPE_VERSION)
        for field, value in (
                ("runtime_source_scope", ["main.py"]),
                ("runtime_source_scope_version", "v1")):
            with self.subTest(field=field):
                forged = dict(manifest)
                forged[field] = value
                forged["artifact_hash"] = artifact.artifact_sha256(forged)
                with self.assertRaises(artifact.ArtifactValidationError):
                    artifact.validate_manifest(
                        forged, forged["artifact_hash"], config={
                            "strategy": {
                                "id": "momentum", "version": "phase1-v3",
                                "fixed_reward_risk": 2.0,
                            }, "llm": {"provider": "anthropic",
                                      "model": "test-model"}},
                        root=self.root)

    def test_provider_endpoint_defaults_and_strips_secret_url_parts(self):
        self.assertEqual(
            provider.provider_endpoint_identity(
                "openai", {}, environ={}),
            "https://api.openai.com/v1")
        identity = provider.provider_endpoint_identity(
            "openai", {}, environ={
                "OPENAI_BASE_URL":
                    "https://user:secret@example.test/v1/?sig=private"})
        self.assertEqual(identity, "https://example.test/v1")
        self.assertNotIn("secret", identity)
        self.assertNotIn("sig", identity)

    def test_config_rejects_secret_bearing_endpoint_before_persistence(self):
        for value in (
                "https://user:secret@example.test/v1",
                "https://example.test/v1?api_key=secret",
                "https://example.test/v1#fragment"):
            with self.subTest(value=value):
                cfg = {
                    "mode": "demo",
                    "llm": {"provider": "openai", "model": "test",
                            "temperature": 0.2, "max_tokens": 128,
                            "base_url": value},
                    "strategy": {"id": "momentum", "version": "phase1-v3"},
                }
                with self.assertRaises(config_mod.ConfigError):
                    config_mod.validate_config(cfg)

    def test_endpoint_drift_invalidates_manifest(self):
        with patch.dict(os.environ, {
                "ANTHROPIC_BASE_URL": "https://review.example.test/v1"},
                clear=False):
            manifest, digest = self._build()
        self.assertEqual(manifest["llm_endpoint"],
                         "https://review.example.test/v1")
        self.assertEqual(
            manifest["llm_endpoint_hash"],
            provider.provider_endpoint_hash(
                "anthropic", {}, environ={
                    "ANTHROPIC_BASE_URL": "https://review.example.test/v1"}))
        with patch.dict(os.environ, {
                "ANTHROPIC_BASE_URL": "https://production.example.test/v1"},
                clear=False):
            with self.assertRaises(artifact.ArtifactValidationError):
                artifact.validate_manifest(
                    manifest, digest, config={"strategy": {
                        "id": "momentum", "version": "phase1-v3",
                        "fixed_reward_risk": 2.0,
                    }, "llm": {"provider": "anthropic",
                              "model": "test-model"}},
                    root=self.root)

    def test_same_safe_endpoint_with_different_query_hashes_differ_and_drift(self):
        with patch.dict(os.environ, {
                "ANTHROPIC_BASE_URL": "https://review.example.test/v1?tenant=a"},
                clear=False):
            first, first_digest = self._build()
        with patch.dict(os.environ, {
                "ANTHROPIC_BASE_URL": "https://review.example.test/v1?tenant=b"},
                clear=False):
            second, second_digest = self._build()
            self.assertEqual(first["llm_endpoint"], second["llm_endpoint"])
            self.assertNotEqual(first["llm_endpoint_hash"],
                                second["llm_endpoint_hash"])
            with self.assertRaises(artifact.ArtifactValidationError):
                artifact.validate_manifest(
                    first, first_digest, config={"strategy": {
                        "id": "momentum", "version": "phase1-v3",
                    }, "llm": {"provider": "anthropic",
                              "model": "test-model"}},
                    root=self.root)

    def test_manifest_never_contains_endpoint_query_or_userinfo(self):
        with patch.dict(os.environ, {
                "ANTHROPIC_BASE_URL":
                    "https://user:secret@review.example.test/v1?sig=private"},
                clear=False):
            manifest, _digest = self._build()
        encoded = artifact.canonical_json(manifest)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("private", encoded)
        self.assertNotIn("?sig", encoded)

    def test_incomplete_or_fake_artifact_is_rejected(self):
        manifest, digest = self._build()
        missing = dict(manifest)
        missing.pop("forward_model_id")
        with self.assertRaises(artifact.ArtifactValidationError):
            artifact.validate_manifest(
                missing, digest, config={"strategy": {
                    "id": "momentum", "version": "phase1-v3",
                    "fixed_reward_risk": 2.0,
                }, "llm": {"provider": "anthropic", "model": "test-model"}},
                root=self.root)
        with self.assertRaises(artifact.ArtifactValidationError):
            artifact.validate_manifest(
                manifest, "0" * 64, config={"strategy": {
                    "id": "momentum", "version": "phase1-v3",
                    "fixed_reward_risk": 2.0,
                }, "llm": {"provider": "anthropic", "model": "test-model"}},
                root=self.root)

    def test_unicode_hash_matches_findings_identity_convention(self):
        value = {"hypothesis": "μomentum — continuation", "nested": ["é"]}
        self.assertEqual(artifact.sha256_json(value), findings._content_hash(value))

    def test_config_and_variant_identity_are_required(self):
        with self.assertRaises(artifact.ArtifactValidationError):
            self._build(config={"strategy": {
                "id": "other", "version": "phase1-v3"}})
        with self.assertRaises(artifact.ArtifactValidationError):
            artifact.build_manifest(
                strategy_id="momentum", strategy_version="phase1-v3",
                variant_id="momentum.baseline",
                variant_definition_hash="a" * 64,
                variant_definition={
                    "variant_id": "momentum.baseline",
                    "strategy_id": "momentum",
                    "base_version": "phase1-v3",
                },
                config={"strategy": {
                    "id": "momentum", "version": "phase1-v3"}},
                strategy_config_version="b" * 16,
                forward_model_id="model",
                forward_model_assumptions_hash="c" * 64,
                prompt_hash="d" * 64,
                llm_provider="anthropic", llm_model="test-model",
                prompt_inputs_hash="e" * 64,
                deployment_config_hash=state.deployment_config_hash({
                    "strategy": {
                        "id": "momentum", "version": "phase1-v3",
                    },
                    "llm": {
                        "provider": "anthropic", "model": "test-model",
                    },
                }),
                root=self.root)

    def test_unrelated_inner_variant_definition_is_rejected(self):
        forged = {
            "variant_id": "other.baseline",
            "strategy_id": "other",
            "base_version": "other-v1",
        }
        with self.assertRaises(artifact.ArtifactValidationError):
            self._build(
                definition=forged,
            )
        manifest, _ = self._build()
        forged_manifest = dict(manifest)
        forged_manifest["variant_definition"] = forged
        forged_manifest["variant_definition_hash"] = artifact.sha256_json(
            forged)
        forged_manifest["artifact_hash"] = artifact.artifact_sha256(
            forged_manifest)
        with self.assertRaises(artifact.ArtifactValidationError):
            artifact.validate_manifest(
                forged_manifest, forged_manifest["artifact_hash"],
                config={
                    "strategy": {
                        "id": "momentum", "version": "phase1-v3",
                    },
                    "llm": {
                        "provider": "anthropic", "model": "test-model",
                    },
                },
                root=self.root)

    def test_prompt_hashes_must_be_full_lowercase_sha256(self):
        for field, value in (
                ("prompt_hash", "d" * 16),
                ("prompt_inputs_hash", "g" * 64)):
            with self.subTest(field=field):
                kwargs = {field: value}
                with self.assertRaises(artifact.ArtifactValidationError):
                    self._build(**kwargs)

    def test_unsupported_manifest_values_are_rejected(self):
        with self.assertRaises(TypeError):
            artifact.sha256_json({"not_json": {1, 2}})

    def test_outside_symlink_is_not_in_runtime_source_set(self):
        outside = self.root.parent / "outside-runtime.py"
        outside.write_text("OUTSIDE = True\n", encoding="utf-8")
        link = self.root / "agent" / "outside.py"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertNotIn(link, artifact.runtime_source_files(self.root))


if __name__ == "__main__":
    unittest.main()
