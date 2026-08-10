from datetime import datetime, timedelta, timezone
from contextlib import closing
import ast
import json
import os
import inspect
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from typing import get_args, get_type_hints

from research import edge_lab, edge_ledger, edge_ledger_proof, edge_ledger_store
from research import edge_discovery_core
from research.edge_lab import DiscoveryError, EdgeLedger, discover
from research.edge_ledger import (
    SCHEMA_VERSION, canonical_json, content_hash, hash_config, hash_dataset,
    hash_provenance, init_db, init_ledger,
)
from research.gates import (
    falsification_gate, heldout_separation, matched_cluster_test,
    performance_floor, placebo_null_distribution, structural_floor,
    verified_gate_envelope, walk_forward_report,
)


def _gate_evidence(heldout, *, alpha=.05):
    """Build the statistical evidence a persisted proof must now reproduce.

    The zero baseline makes the matched deltas equal to the held-out P&L, so
    every recorded statistic is recomputable from the envelope itself.
    """
    baseline = [{**row, "net_pnl": 0.0, "opportunity_id": f"base-{index}"}
                for index, row in enumerate(heldout)]
    control = matched_cluster_test(heldout, baseline, vehicle="equity")
    placebo = placebo_null_distribution(heldout, baseline, vehicle="equity")
    falsification = {
        **falsification_gate(placebo["observed"], placebo["placebo"], alpha=alpha),
        "method": placebo["method"], "assignments_hash": placebo["assignments_hash"],
        "observations": len(placebo["observed"]),
        "draws": int(placebo["draws"]), "seed": int(placebo["seed"]),
    }
    absolute = performance_floor(heldout, vehicle="equity")
    walk = walk_forward_report(heldout, baseline, vehicle="equity")
    return control, falsification, absolute, walk


def _sessions(start: datetime, count: int, symbols=("SPY", "QQQ", "IWM", "DIA")) -> list[dict]:
    """Sessions with a real timing edge, not a session-long directional drift.

    The breakout runs far enough to pay 1.5R but not 2R, and then gives the
    whole move back over a long decline.  The give-back is what makes a
    randomly timed entry lose: without it the randomized-entry null control
    would score the same trade as the strategy, which is the point of having
    the control at all.
    """
    rows: list[dict] = []
    for offset in range(count):
        session = start + timedelta(days=offset)
        values = [
            (100, 101, 99, 100),        # one-minute opening range
            (100, 102, 99.5, 102),      # confirmed breakout
            (102, 103, 101.5, 103),     # next-bar entry at 102
            (103, 107, 102.5, 106.8),   # 1.5R (106.5) target, but not 2R (108)
        ]
        price = 106.8
        for _ in range(20):            # the whole move handed back
            values.append((price, price + .05, price - .4, price - .35))
            price -= .35
        for index, symbol in enumerate(symbols):
            # A per-symbol offset keeps the sample from being one repeated
            # observation while leaving every level on the same side of itself.
            shift = index * .01
            for minute, (open_, high, low, close) in enumerate(values):
                rows.append({
                    "symbol": symbol,
                    "timestamp": (session + timedelta(minutes=minute)).isoformat(),
                    "open": open_ + shift, "high": high + shift,
                    "low": low + shift, "close": close + shift,
                    "volume": 1, "provider": "alpaca", "feed": "sip",
                })
    return rows


def _persist_gate(ledger: EdgeLedger, candidate_id: str, lane: str, *,
                  passes: bool = True, record: bool = True,
                  score: float = 1.0,
                  scores: list[float] | None = None,
                  r_multiples: list[float] | None = None) -> tuple[dict, dict]:
    prefix = f"{lane}-{'pass' if passes else 'fail'}"
    fit = [] if lane == "shadow" else [
        {"vehicle": "equity", "session_date": "2024-01-02",
         "opportunity_id": f"{prefix}-fit", "net_pnl": 1.0},
    ]
    # Eight held-out sessions: a sign-flip null over two clusters cannot
    # reach any useful significance level, so the old two-session fixture
    # could never have supported a passing falsification.
    # ``scores`` gives one P&L per held-out session, which lets a fixture hold
    # the mean delta fixed while moving its dispersion, and therefore its
    # lower confidence bound.
    values = list(scores) if scores is not None else [
        score if passes else -1.0] * 8
    heldout = [
        {"vehicle": "equity", "symbol": "SPY",
         "session_date": f"2024-01-{day:02d}",
         "opportunity_id": f"{prefix}-held-{day}",
         "net_pnl": value}
        for day, value in zip(range(3, 3 + len(values)), values)
    ]
    fit_floor = structural_floor(
        fit, vehicle="equity", min_trades=1, min_sessions=1,
        required=lane != "shadow")
    held_floor = structural_floor(
        heldout, vehicle="equity", min_trades=1, min_sessions=1)
    separation = (heldout_separation(fit, heldout) if lane == "backtest" else
                  {"fit": 0, "heldout": len(heldout), "overlap_sessions": [],
                   "passes": True, "mode": "new_data"})
    control, falsification, absolute, walk = _gate_evidence(heldout)
    checks = {"edge_positive": passes, "family_fdr_significant": True,
              "falsification": bool(passes and falsification["passes"]),
              "heldout_net_pnl_positive": bool(passes and absolute["net_pnl_positive"]),
              "heldout_expectancy_positive": bool(passes and absolute["expectancy_positive"]),
              "heldout_delta_lcb_positive": bool(
                  passes and control["mean_delta_lcb"] is not None and
                  control["mean_delta_lcb"] > 0),
              "walk_forward_majority_positive": bool(passes and walk["majority_positive"])}
    envelope = verified_gate_envelope(
        lane=lane, vehicle="equity", fit=fit, heldout=heldout,
        fit_floor=fit_floor, heldout_floor=held_floor,
        control={**control, "kind": "matched_actual_baseline"},
        p_value=control["p_value"], q_value=.02, alpha=.05,
        falsification=falsification,
        separation=separation, checks=checks, passes=passes,
        walk_forward=walk,
        performance={"heldout_delta": control["mean_delta"],
                     "heldout_delta_lcb": control["mean_delta_lcb"],
                     "heldout_net_pnl": absolute["net_pnl"],
                     "heldout_expectancy": absolute["expectancy"],
                     "max_drawdown": 0.0 if passes else 8.0})
    run = ledger.append_run(
        candidate_id, lane=lane, vehicle="equity", fit=fit, heldout=heldout,
        metrics={"gate": {"passes": not passes}, "confidence": 0.0})
    # ``r_multiples`` attaches the risk-normalized outcome the drift reference
    # is read from; the gate statistics themselves never consume it.
    per_trade = dict(zip((row["opportunity_id"] for row in heldout),
                         r_multiples or ()))
    for row in [*fit, *heldout]:
        value = per_trade.get(row["opportunity_id"])
        ledger.append_trade(run["run_id"],
                            row if value is None else {**row, "r_multiple": value})
    if record:
        ledger.record_verified_gate(run["run_id"], envelope)
    return run, envelope


class EdgeLedgerStoreExtractionTests(unittest.TestCase):
    def test_store_symbols_are_identical_through_edge_facades(self):
        names = (
            "VEHICLES", "LANES", "LIFECYCLE", "CANDIDATE", "BACKTEST_PASSED",
            "SHADOW", "VALIDATED", "CHAMPION", "RETIRED", "DEMOTED",
            "SCHEMA_VERSION", "DEFAULT_DB_PATH", "PAPER_DEMOTION_MIN_OUTCOMES",
            "PAPER_DEMOTION_R_FLOOR",
            "canonical_json", "content_hash", "hash_dataset", "hash_config",
            "hash_provenance", "hash_file", "provenance_hash", "init_ledger", "init_db",
        )
        for name in names:
            self.assertIs(getattr(edge_ledger, name), getattr(edge_ledger_store, name))
            self.assertIs(getattr(edge_lab, name), getattr(edge_ledger_store, name))
        self.assertIs(edge_ledger.EdgeLedger, edge_lab.EdgeLedger)

    def test_proof_mixin_preserves_edge_ledger_facade_and_mro_identity(self):
        names = (
            "record_verified_gate", "_gate_envelope_error", "_latest_verified_gate",
            "latest_verified_run", "eligibility",
        )
        self.assertTrue(issubclass(edge_ledger.EdgeLedger,
                                   edge_ledger_proof.EdgeLedgerProofMixin))
        for name in names:
            self.assertNotIn(name, edge_ledger.EdgeLedger.__dict__)
            self.assertIs(getattr(edge_ledger.EdgeLedger, name),
                          getattr(edge_ledger_proof.EdgeLedgerProofMixin, name))
            self.assertIs(inspect.getattr_static(edge_ledger.EdgeLedger, name),
                          inspect.getattr_static(edge_ledger_proof.EdgeLedgerProofMixin, name))

    def test_proof_import_is_lazy_and_does_not_capture_ledger_defaults(self):
        root = Path(__file__).resolve().parents[2]
        script = (
            "import sys; import research.edge_ledger_proof as proof; "
            "assert 'research.edge_ledger' not in sys.modules; "
            "assert 'DEFAULT_DB_PATH' not in vars(proof)"
        )
        result = subprocess.run([sys.executable, "-c", script], cwd=root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_legacy_edge_ledger_helpers_are_intercepted_by_proof_facade(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", hypothesis="facade helper patches", config={})
            run, envelope = _persist_gate(
                ledger, candidate["candidate_id"], "backtest", record=False)
            names = (
                "_connect", "_json", "_row", "_utc", "content_hash",
                "sample_counts", "verify_gate_envelope", "_finite_number",
                "_nonnegative_integer",
            )
            patches = [mock.patch.object(edge_ledger, name,
                                         wraps=getattr(edge_ledger, name))
                       for name in names]
            with patches[0] as connect, patches[1] as json_, patches[2] as row, \
                    patches[3] as utc, patches[4] as content, patches[5] as counts, \
                    patches[6] as verify, patches[7] as finite, patches[8] as integer:
                ledger.record_verified_gate(run["run_id"], envelope)
            for helper in (connect, json_, row, utc, content, counts, verify,
                           finite, integer):
                self.assertTrue(helper.called, helper)

    def test_hash_aliases_are_deterministic_and_reject_nonfinite_json(self):
        value = {"z": "café", "a": [1, True, None]}
        self.assertEqual(canonical_json(value), '{"a":[1,true,null],"z":"café"}')
        self.assertIs(hash_dataset, content_hash)
        self.assertIs(hash_config, content_hash)
        self.assertIs(hash_provenance, content_hash)
        expected = content_hash(value)
        self.assertEqual(hash_dataset(value), expected)
        self.assertEqual(hash_config(value), expected)
        self.assertEqual(hash_provenance(value), expected)
        with self.assertRaises(ValueError):
            canonical_json(float("nan"))
        with self.assertRaises(ValueError):
            canonical_json(float("inf"))

    def test_fresh_schema_initialization_is_idempotent_and_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edge.sqlite3"
            self.assertEqual(init_ledger(path), {"db_path": str(path), "schema": SCHEMA_VERSION})
            self.assertEqual(init_db(path), {"db_path": str(path), "schema": SCHEMA_VERSION})
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(
                    db.execute("SELECT value FROM ledger_meta WHERE key='schema'").fetchone()[0],
                    str(SCHEMA_VERSION),
                )
            candidate = EdgeLedger(path).register_candidate(
                "ibr.target.1_5r", hypothesis="immutable", config={})
            with closing(sqlite3.connect(path)) as db:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    db.execute(
                        "UPDATE candidates SET hypothesis='changed' WHERE candidate_id=?",
                        (candidate["candidate_id"],),
                    )


class EdgeDiscoveryCoreExtractionTests(unittest.TestCase):
    def test_core_helpers_are_facade_identity_aliases(self):
        names = (
            "DiscoveryError", "_read_discovery_rows", "_effective_ibr_config",
            "_opportunity_rows", "_discover_gate", "_finalize_gate",
        )
        for name in names:
            self.assertIs(getattr(edge_lab, name), getattr(edge_discovery_core, name))
        self.assertIs(
            __import__("research.strategy_factory", fromlist=["_read_discovery_rows"])._read_discovery_rows,
            edge_lab._read_discovery_rows,
        )

    def test_helpers_are_not_local_edge_lab_definitions(self):
        source = inspect.getsource(edge_lab)
        tree = ast.parse(source)
        local_names = {
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        for name in (
            "DiscoveryError", "_read_discovery_rows", "_effective_ibr_config",
            "_opportunity_rows", "_discover_gate", "_finalize_gate",
        ):
            self.assertNotIn(name, local_names)

    def test_core_import_is_lazy_and_does_not_load_edge_lab_or_ledger(self):
        root = Path(__file__).resolve().parents[2]
        script = (
            "import sys; import research.edge_discovery_core; "
            "assert 'research.edge_lab' not in sys.modules; "
            "assert 'research.edge_ledger' not in sys.modules; "
            "assert 'sqlite3' not in sys.modules"
        )
        result = subprocess.run([sys.executable, "-c", script], cwd=root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_moved_helper_type_hints_resolve(self):
        result = get_type_hints(edge_discovery_core._read_discovery_rows)["return"]
        _, bars, snapshots, quotes = get_args(result)
        self.assertIs(get_args(bars)[0], edge_discovery_core.UnderlyingBar)
        self.assertIs(get_args(snapshots)[1], edge_discovery_core.OptionSnapshot)
        self.assertIs(get_args(quotes)[0], edge_discovery_core.QuoteSnapshot)

    def test_moved_read_helper_forwards_legacy_facade_normalizer_patch(self):
        row = {
            "symbol": "SPY", "timestamp": "2024-01-02T14:30:00+00:00",
            "open": 100, "high": 101, "low": 99, "close": 100,
            "volume": 1,
        }
        bar = mock.Mock()
        with mock.patch.object(edge_lab, "normalize_underlying_bar",
                               return_value=bar) as normalizer:
            raw, bars, snapshots, quotes = edge_discovery_core._read_discovery_rows([row])
        self.assertEqual(raw, [row])
        self.assertEqual(bars, [bar])
        self.assertEqual(snapshots, {})
        self.assertEqual(quotes, [])
        normalizer.assert_called_once_with(row, provider="alpaca", feed="sip")

    def test_discover_uses_patchable_edge_lab_helper_alias(self):
        with mock.patch.object(edge_lab, "_read_discovery_rows",
                               side_effect=DiscoveryError("patched facade")):
            with self.assertRaisesRegex(DiscoveryError, "patched facade"):
                discover([], lane="backtest")


class DiscoveryCorpusStreamingTests(unittest.TestCase):
    """The corpus is fed row by row; what the replay computes is unchanged."""

    @staticmethod
    def _rows(sessions=3, per_session=4):
        rows = []
        for session in range(sessions):
            day = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc) + timedelta(days=session)
            for minute in range(per_session):
                stamp = (day + timedelta(minutes=minute)).isoformat()
                rows.append({"kind": "bar", "symbol": "SPY", "timestamp": stamp,
                             "as_of": stamp, "open": 100, "high": 101, "low": 99,
                             "close": 100.5, "volume": 10})
        return rows

    def _write(self, root: Path, rows, *, partitioned: bool):
        if not partitioned:
            target = root / "market.jsonl"
            target.write_text("".join(json.dumps(row, sort_keys=True) + "\n"
                                      for row in rows), encoding="utf-8")
            return target
        directory = root / "sessions"
        directory.mkdir()
        for row in rows:
            day = row["timestamp"][:10]
            with (directory / f"market-{day}.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        return directory

    def test_partitioned_corpus_loads_exactly_like_one_file(self):
        rows = self._rows()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flat = edge_discovery_core._read_discovery_rows(
                self._write(root, rows, partitioned=False))
            split = edge_discovery_core._read_discovery_rows(
                self._write(root, rows, partitioned=True))
        self.assertEqual(flat[0], split[0])
        self.assertEqual(content_hash(flat[0]), content_hash(split[0]))
        self.assertEqual([vars(bar) for bar in flat[1]],
                         [vars(bar) for bar in split[1]])
        self.assertEqual((flat[2], flat[3]), (split[2], split[3]))

    def test_reader_streams_without_materializing_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self._write(Path(directory), self._rows(), partitioned=False)
            with mock.patch.object(
                    Path, "read_text",
                    side_effect=AssertionError("corpus must not be slurped")):
                raw, bars, _, _ = edge_discovery_core._read_discovery_rows(source)
        self.assertEqual(len(raw), 12)
        self.assertEqual(len(bars), 12)

    def test_session_window_bounds_work_by_window_not_corpus_size(self):
        loaded = {}
        with tempfile.TemporaryDirectory() as directory:
            for label, sessions in (("small", 4), ("large", 40)):
                root = Path(directory) / label
                root.mkdir()
                corpus = self._write(root, self._rows(sessions=sessions),
                                     partitioned=True)
                self.assertEqual(len(edge_discovery_core.corpus_partitions(
                    corpus, window=2)), 2)
                with mock.patch.dict(
                        os.environ,
                        {edge_discovery_core.SESSION_WINDOW_ENV: "2"}, clear=False):
                    windowed = edge_discovery_core._read_discovery_rows(corpus)
                whole = edge_discovery_core._read_discovery_rows(corpus)
                loaded[label] = (len(windowed[0]), len(whole[0]))
        self.assertEqual(loaded["small"][0], loaded["large"][0])
        self.assertEqual((loaded["small"][1], loaded["large"][1]), (16, 160))

    def test_invalid_partition_line_names_its_partition(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus = self._write(Path(directory), self._rows(), partitioned=True)
            broken = sorted(corpus.glob("*.jsonl"))[-1]
            with broken.open("a", encoding="utf-8") as handle:
                handle.write("{not json\n")
            with self.assertRaisesRegex(DiscoveryError, broken.name):
                edge_discovery_core._read_discovery_rows(corpus)


class EdgeDiscoveryLifecycleTests(unittest.TestCase):
    def test_trade_metrics_reject_nonfinite_values(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="finite evidence", config={})
            run = ledger.append_run(candidate["candidate_id"], lane="backtest")
            base = {
                "session_date": "2024-01-02",
                "opportunity_id": "finite-check",
            }
            for value in ("inf", "-inf", "nan"):
                with self.subTest(net_pnl=value), self.assertRaisesRegex(
                        ValueError, "net_pnl must be finite"):
                    ledger.append_trade(run["run_id"], {**base, "net_pnl": value})
                with self.subTest(return_value=value), self.assertRaisesRegex(
                        ValueError, "return_value must be finite"):
                    ledger.append_trade(run["run_id"], {
                        **base, "opportunity_id": f"return-{value}",
                        "net_pnl": 1.0, "return_value": value,
                    })
            for field in ("net_pnl", "return_value"):
                for invalid in (True, b"1.0", 10 ** 10000):
                    with self.subTest(field=field, value=type(invalid).__name__), \
                            self.assertRaisesRegex(ValueError, f"{field} must be numeric"):
                        ledger.append_trade(run["run_id"], {
                            **base, "opportunity_id": f"invalid-{field}",
                            "net_pnl": 1.0, field: invalid,
                        })

    def test_verified_gate_rejects_malformed_scalars_without_evidence_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="scalar proof",
                config={})
            run, envelope = _persist_gate(
                ledger, candidate["candidate_id"], "backtest", record=False)

            def rehash(value):
                value["content_hash"] = content_hash({
                    key: item for key, item in value.items()
                    if key != "content_hash"})
                return value

            malformed = [
                ("floor count None", lambda value: value["floors"]["fit"].__setitem__(
                    "trades", None)),
                ("floor count bool", lambda value: value["floors"]["fit"].__setitem__(
                    "trades", True)),
                ("floor minimum huge", lambda value: value["floors"]["fit"]["minimums"].__setitem__(
                    "trades", 10 ** 4000)),
                ("floor structural string", lambda value: value["floors"]["fit"][
                    "structural_checks"].__setitem__("trades", "true")),
                ("floor required number", lambda value: value["floors"]["fit"].__setitem__(
                    "required", 1)),
                ("statistics None", lambda value: value["statistics"].__setitem__(
                    "p_value", None)),
                ("statistics bool", lambda value: value["statistics"].__setitem__(
                    "alpha", True)),
                ("statistics huge", lambda value: value["statistics"].__setitem__(
                    "q_value", 10 ** 4000)),
                ("control delta None", lambda value: value["control"].__setitem__(
                    "mean_delta", None)),
                ("control delta string", lambda value: value["control"].__setitem__(
                    "mean_delta", "bad")),
                ("matched bool", lambda value: value["control"].__setitem__(
                    "matched", True)),
                ("matched huge", lambda value: value["control"].__setitem__(
                    "matched", 10 ** 4000)),
                ("performance bool", lambda value: value["performance"].__setitem__(
                    "heldout_delta", True)),
                ("performance drawdown negative", lambda value: value["performance"].__setitem__(
                    "max_drawdown", -1.0)),
                ("falsification string", lambda value: value["falsification"].__setitem__(
                    "passes", "yes")),
            ]
            for name, mutate in malformed:
                with self.subTest(name=name):
                    candidate_gate = json.loads(json.dumps(envelope))
                    mutate(candidate_gate)
                    with self.assertRaises(ValueError):
                        ledger.record_verified_gate(run["run_id"], rehash(candidate_gate))
                    with closing(sqlite3.connect(ledger.path)) as db:
                        self.assertEqual(
                            db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 0)

            for invalid_gate in (None, True, b"gate", []):
                with self.subTest(gate=type(invalid_gate).__name__), self.assertRaises(
                        ValueError):
                    ledger.record_verified_gate(run["run_id"], invalid_gate)

    def test_select_champion_skips_corrupt_scoring_data(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="corrupt score",
                config={})
            candidate_id = candidate["candidate_id"]
            _persist_gate(ledger, candidate_id, "backtest")
            ledger.transition(candidate_id, "backtest_passed", reason="backtest proof")
            run, envelope = _persist_gate(ledger, candidate_id, "shadow")
            ledger.transition(candidate_id, "shadow", reason="shadow proof")
            ledger.transition(candidate_id, "validated", reason="validated proof")
            malformed = json.loads(json.dumps(envelope))
            malformed["performance"]["heldout_delta"] = True
            with mock.patch.object(ledger, "_latest_verified_gate",
                                   return_value=(run, malformed)):
                self.assertIsNone(ledger.select_champion(
                    vehicle="equity", min_confidence=.9))

    def test_failed_verified_gate_rejects_malformed_control_and_proof_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="failed scalar proof",
                config={})
            run, envelope = _persist_gate(
                ledger, candidate["candidate_id"], "backtest", passes=False,
                record=False)

            def rehash(value):
                value["content_hash"] = content_hash({
                    key: item for key, item in value.items()
                    if key != "content_hash"})
                return value

            def remove(mapping, key):
                mapping.pop(key, None)

            malformed = [
                ("matched None", lambda value: value["control"].__setitem__(
                    "matched", None)),
                ("matched bool", lambda value: value["control"].__setitem__(
                    "matched", True)),
                ("actual control missing", lambda value: remove(
                    value["control"], "actual_control")),
                ("available missing", lambda value: remove(
                    value["control"], "available")),
                ("falsification missing", lambda value: remove(
                    value, "falsification")),
                ("falsification scalar", lambda value: value.__setitem__(
                    "falsification", "bad")),
                ("falsification pass missing", lambda value: remove(
                    value["falsification"], "passes")),
                ("separation missing", lambda value: remove(value, "separation")),
                ("separation scalar", lambda value: value.__setitem__(
                    "separation", 1)),
                ("separation pass missing", lambda value: remove(
                    value["separation"], "passes")),
            ]
            for name, mutate in malformed:
                with self.subTest(name=name):
                    candidate_gate = json.loads(json.dumps(envelope))
                    mutate(candidate_gate)
                    with self.assertRaises(ValueError):
                        ledger.record_verified_gate(run["run_id"], rehash(candidate_gate))
                    with closing(sqlite3.connect(ledger.path)) as db:
                        self.assertEqual(
                            db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 0)

    def test_failed_zero_match_gate_with_none_delta_reverifies(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="no control proof",
                config={})
            run, envelope = _persist_gate(
                ledger, candidate["candidate_id"], "backtest", passes=False,
                record=False)
            # A genuinely unmatched control carries no deltas at all, so the
            # recomputed statistics must be empty rather than merely relabelled.
            envelope["control"].update({
                "matched": 0, "available": False, "mean_delta": None,
                "mean_delta_lcb": None, "deltas": [], "delta_clusters": [],
            })
            envelope["checks"]["heldout_delta_positive"] = False
            envelope["checks"]["heldout_delta_lcb_positive"] = False
            envelope["statistics"]["p_value"] = 1.0
            envelope["performance"]["heldout_delta"] = None
            envelope["performance"]["heldout_delta_lcb"] = None
            envelope["content_hash"] = content_hash({
                key: item for key, item in envelope.items()
                if key != "content_hash"})
            ledger.record_verified_gate(run["run_id"], envelope)
            proof = ledger.latest_verified_run(candidate["candidate_id"], lane="backtest")
            self.assertIsNotNone(proof)
            self.assertIsNone(proof["verified_gate"]["control"]["mean_delta"])
            self.assertIsNone(proof["verified_gate"]["performance"]["heldout_delta"])

    def test_none_delta_is_rejected_for_contradictory_gate_state(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="contradictory proof",
                config={})

            def rehash(value):
                value["content_hash"] = content_hash({
                    key: item for key, item in value.items()
                    if key != "content_hash"})
                return value

            malformed = [
                ("matched positive", lambda value: value["control"].__setitem__(
                    "matched", 1)),
                ("control available", lambda value: value["control"].__setitem__(
                    "available", True)),
                ("heldout check positive", lambda value: value["checks"].__setitem__(
                    "heldout_delta_positive", True)),
                ("control delta missing", lambda value: value["control"].pop(
                    "mean_delta", None)),
                ("performance delta missing", lambda value: value["performance"].pop(
                    "heldout_delta", None)),
                ("performance drawdown missing", lambda value: value["performance"].pop(
                    "max_drawdown", None)),
                ("performance string", lambda value: value["performance"].__setitem__(
                    "heldout_delta", "bad")),
            ]
            for name, mutate in malformed:
                with self.subTest(name=name):
                    run, envelope = _persist_gate(
                        ledger, candidate["candidate_id"], "backtest", passes=False,
                        record=False)
                    envelope["control"].update({
                        "matched": 0, "available": False, "mean_delta": None,
                    })
                    envelope["checks"]["heldout_delta_positive"] = False
                    envelope["performance"]["heldout_delta"] = None
                    mutate(envelope)
                    with self.assertRaises(ValueError):
                        ledger.record_verified_gate(run["run_id"], rehash(envelope))
                    with closing(sqlite3.connect(ledger.path)) as db:
                        self.assertEqual(
                            db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 0)

    def test_paper_outcomes_reject_malformed_input_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="paper scalar",
                config={})
            invalid = [
                None, True, b"outcome", bytearray(b"outcome"),
                {"net_pnl": True, "risk_usd": 1},
                {"net_pnl": b"1", "risk_usd": 1},
                {"net_pnl": 10 ** 10000, "risk_usd": 1},
                {"net_pnl": 1, "risk_usd": 10 ** 10000},
                {"net_pnl": 1, "risk_usd": 0},
                {"net_pnl": 1, "risk_usd": float("inf")},
            ]
            for value in invalid:
                with self.subTest(value=type(value).__name__), self.assertRaisesRegex(
                        ValueError, "paper outcome requires finite net_pnl and positive risk_usd"):
                    ledger.ingest_paper_outcome(candidate["candidate_id"], value)
            with closing(sqlite3.connect(ledger.path)) as db:
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM paper_outcomes").fetchone()[0], 0)
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_paper_replay_skips_malformed_persisted_r_multiple(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="paper replay",
                config={})
            candidate_id = candidate["candidate_id"]
            malformed = (True, [], 10 ** 4000)
            with closing(sqlite3.connect(ledger.path)) as db, db:
                for index, value in enumerate(malformed):
                    db.execute(
                        "INSERT INTO paper_outcomes "
                        "(outcome_id,candidate_id,vehicle,opportunity_id,session_date,net_pnl,outcome_json,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (f"legacy-{index}", candidate_id, "equity", f"legacy-{index}",
                         "2024-01-01", 0.0, json.dumps({"r_multiple": value}), index))
            result = ledger.ingest_paper_outcome(candidate_id, {
                "vehicle": "equity", "opportunity_id": "paper-valid",
                "net_pnl": 1, "risk_usd": 10,
            })
            self.assertEqual(result["rolling_outcomes"], 1)
            self.assertEqual(result["rolling_r"], .1)

    def test_paper_outcomes_demote_a_champion_that_breaks_the_rolling_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="earlier target", config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]
            for lane in ("backtest", "shadow"):
                _persist_gate(ledger, candidate_id, lane)
                if lane == "backtest":
                    ledger.transition(candidate_id, "backtest_passed",
                                      reason="backtest gates passed")
                else:
                    ledger.transition(candidate_id, "shadow",
                                      reason="shadow evidence started")
                    ledger.transition(candidate_id, "validated",
                                      reason="shadow gates passed")
            ledger.transition(candidate_id, "champion",
                              reason="best validated evidence")
            for index in range(20):
                outcome = ledger.ingest_paper_outcome(candidate_id, {
                    "vehicle": "equity", "opportunity_id": f"paper-{index}",
                    "session_date": f"2024-02-{index + 1:02d}",
                    "net_pnl": -1, "risk_usd": 10, "r_multiple": 999,
                })
            self.assertEqual(outcome["status"], "demoted")

    def _deployed_candidate(self, ledger, *, r_multiples=None):
        candidate = ledger.register_candidate(
            "ibr.target.1_5r", vehicle="equity", hypothesis="deployed guard",
            config={"strategy": {"target_r": 1.5}})
        candidate_id = candidate["candidate_id"]
        _persist_gate(ledger, candidate_id, "backtest")
        ledger.transition(candidate_id, "backtest_passed", reason="backtest gates passed")
        _persist_gate(ledger, candidate_id, "shadow", scores=[1.0] * 20,
                      r_multiples=r_multiples)
        ledger.transition(candidate_id, "shadow", reason="shadow evidence started")
        ledger.transition(candidate_id, "validated", reason="shadow gates passed")
        return candidate_id

    def _ingest(self, ledger, candidate_id, values, *, prefix="paper"):
        result = {}
        for index, r_multiple in enumerate(values):
            result = ledger.ingest_paper_outcome(candidate_id, {
                "vehicle": "equity", "opportunity_id": f"{prefix}-{index}",
                "session_date": f"2024-02-{index + 1:02d}",
                "net_pnl": r_multiple * 10, "risk_usd": 10,
            })
        return result

    def test_rolling_guard_demotes_a_losing_validated_non_champion(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate_id = self._deployed_candidate(ledger)
            result = self._ingest(ledger, candidate_id, [-0.1] * 20)
            self.assertEqual(result["status"], "demoted")
            transitions = [(row["from_status"], row["to_status"])
                           for row in ledger.history(candidate_id)
                           if row["event_type"] == "safety_demotion"]
            self.assertEqual(transitions, [("validated", "demoted")])

    def test_repeated_ingestion_of_one_opportunity_is_a_single_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="idempotent",
                config={})
            candidate_id = candidate["candidate_id"]
            outcome = {"vehicle": "equity", "opportunity_id": "paper-1",
                       "net_pnl": -1, "risk_usd": 10}
            first = ledger.ingest_paper_outcome(candidate_id, outcome)
            repeats = [ledger.ingest_paper_outcome(candidate_id, outcome)
                       for _ in range(3)]
            self.assertFalse(first["duplicate"])
            self.assertTrue(all(row["duplicate"] for row in repeats))
            self.assertEqual({row["outcome_id"] for row in repeats},
                             {first["outcome_id"]})
            self.assertEqual([row["rolling_r"] for row in repeats], [-.1] * 3)
            with closing(sqlite3.connect(ledger.path)) as db:
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM paper_outcomes").fetchone()[0], 1)

    def test_drift_demotes_a_deployed_candidate_whose_paper_r_collapses(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate_id = self._deployed_candidate(
                ledger, r_multiples=[.5, 1.5] * 10)
            # The rolling floor cannot see this: twenty outcomes summing to
            # -1R stay above the -2R floor.  Only the sequential test against
            # the validated held-out distribution proves the edge is gone.
            result = self._ingest(ledger, candidate_id, [-0.05] * 20)
            self.assertGreater(result["rolling_r"], -2.0)
            self.assertTrue(result["drift"]["degraded"])
            self.assertGreaterEqual(result["drift"]["statistic"], 4.0)
            self.assertEqual(result["status"], "demoted")

    def test_drift_stays_quiet_while_paper_r_wobbles_around_the_validated_mean(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate_id = self._deployed_candidate(
                ledger, r_multiples=[.5, 1.5] * 10)
            result = self._ingest(ledger, candidate_id, [.8, 1.2] * 15)
            self.assertTrue(result["drift"]["applicable"])
            self.assertFalse(result["drift"]["degraded"])
            self.assertLess(result["drift"]["statistic"], 0.0)
            self.assertEqual(result["status"], "validated")

    def test_drift_tolerates_a_single_severe_loss_inside_the_validated_spread(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate_id = self._deployed_candidate(
                ledger, r_multiples=[.5, 1.5] * 10)
            result = self._ingest(ledger, candidate_id, [1.0] * 19 + [-3.0])
            self.assertFalse(result["drift"]["degraded"])
            self.assertEqual(result["status"], "validated")

    def test_drift_is_inapplicable_without_a_risk_normalized_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate_id = self._deployed_candidate(ledger)
            self.assertIsNone(ledger.heldout_reference(candidate_id))
            result = self._ingest(ledger, candidate_id, [.5] * 20)
            self.assertFalse(result["drift"]["applicable"])
            self.assertEqual(result["status"], "validated")

    def test_paper_outcomes_recompute_r_and_reject_manual_demotion(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="paper guard", config={"strategy": {"target_r": 1.5}})
            with self.assertRaisesRegex(ValueError, "positive risk_usd"):
                ledger.ingest_paper_outcome(candidate["candidate_id"], {
                    "vehicle": "equity", "net_pnl": -1, "r_multiple": -100,
                    "demote": True,
                })
            result = ledger.ingest_paper_outcome(candidate["candidate_id"], {
                "vehicle": "equity", "opportunity_id": "paper-safe",
                "net_pnl": -1, "risk_usd": 10, "r_multiple": -100,
                "demote": True,
            })
            self.assertEqual(result["rolling_r"], -0.1)
            self.assertEqual(result["status"], "candidate")

    def test_discovery_operational_error_is_distinct_from_insufficient_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options_only = root / "options.jsonl"
            options_only.write_text(
                json.dumps({"kind": "quote", "symbol": "SPY"}) + "\n",
                encoding="utf-8")
            result = subprocess.run([
                sys.executable, "research.py", "edge", "discover",
                "--data", str(options_only), "--db", str(root / "edge.sqlite3")],
                cwd=Path(__file__).resolve().parents[2], check=False,
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)

    def test_forward_lifecycle_cannot_be_manually_advanced_without_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="earlier target", config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]
            with self.assertRaisesRegex(ValueError, "verified gate evidence"):
                ledger.transition(candidate_id, "backtest_passed", reason="operator request")
            ledger.append_run(
                candidate_id, lane="backtest", vehicle="equity",
                fit=[{"session_date": "2024-01-02"}],
                heldout=[{"session_date": "2024-01-03"}],
                metrics={"gate": {"passes": True}})
            with self.assertRaisesRegex(ValueError, "lacks verified gate evidence"):
                ledger.transition(candidate_id, "backtest_passed", reason="forged metrics")
            _persist_gate(ledger, candidate_id, "backtest")
            self.assertEqual(ledger.transition(
                candidate_id, "backtest_passed", reason="gates passed")["status"],
                "backtest_passed")
            with self.assertRaisesRegex(ValueError, "passing shadow verified evidence"):
                ledger.transition(candidate_id, "shadow", reason="operator request")

    def test_retirement_rollback_and_forged_gate_cannot_bypass_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="earlier target",
                config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]
            with self.assertRaisesRegex(ValueError, "verified gate evidence"):
                ledger.transition(candidate_id, "retired", reason="manual retirement")
            with self.assertRaisesRegex(ValueError, "rollback cannot bypass evidence"):
                ledger.transition(candidate_id, "candidate", reason="rollback", rollback=True)
            run, envelope = _persist_gate(
                ledger, candidate_id, "backtest", passes=True, record=False)
            envelope["passes"] = False
            with self.assertRaisesRegex(ValueError, "envelope/hash"):
                ledger.record_verified_gate(run["run_id"], envelope)

    def test_latest_failing_proof_makes_a_champion_ineligible(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="earlier target",
                config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]
            _persist_gate(ledger, candidate_id, "backtest")
            ledger.transition(candidate_id, "backtest_passed", reason="backtest proof")
            _persist_gate(ledger, candidate_id, "shadow")
            ledger.transition(candidate_id, "shadow", reason="shadow proof")
            ledger.transition(candidate_id, "validated", reason="validated proof")
            self.assertIsNotNone(ledger.select_champion(
                vehicle="equity", min_confidence=.9))
            self.assertTrue(ledger.eligibility(candidate_id)["eligible"])
            self.assertEqual(
                ledger.latest_verified_run(candidate_id, lane="shadow")["lane"],
                "shadow")
            _persist_gate(ledger, candidate_id, "shadow", passes=False)
            self.assertIsNone(ledger.select_champion(
                vehicle="equity", min_confidence=.9))
            self.assertFalse(ledger.eligibility(candidate_id)["eligible"])

    def _validated_candidate(self, ledger, variant, hypothesis, scores):
        candidate = ledger.register_candidate(
            variant, vehicle="equity", hypothesis=hypothesis, config={})
        candidate_id = candidate["candidate_id"]
        for lane in ("backtest", "shadow"):
            _persist_gate(ledger, candidate_id, lane, scores=scores)
            ledger.transition(candidate_id,
                              "backtest_passed" if lane == "backtest" else "shadow",
                              reason=f"{lane} proof")
        ledger.transition(candidate_id, "validated", reason="validated proof")
        return candidate_id

    # A positive point estimate is not evidence.  SPIKY carries the higher
    # mean held-out delta; STEADY the higher lower bound.  Both are positive,
    # so both pass the gate and the ranking rule alone separates them.
    STEADY = [3.5] * 8
    SPIKY = [9.0, 1.0, 9.0, 1.0, 9.0, 1.0, 9.0, 1.0]
    # Same mean as a flat +1 series, but a lower bound below zero.
    ERRATIC = [30.0, -28.0, 25.0, -22.0, 20.0, -18.0, 15.0, -14.0]

    def test_a_non_positive_lower_bound_cannot_be_recorded_as_a_passing_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.range.30", vehicle="equity",
                hypothesis="positive mean, lower bound below zero", config={})
            run, envelope = _persist_gate(
                ledger, candidate["candidate_id"], "backtest",
                scores=self.ERRATIC, record=False)
            self.assertGreater(envelope["performance"]["heldout_delta"], 0)
            self.assertLessEqual(envelope["performance"]["heldout_delta_lcb"], 0)
            self.assertFalse(envelope["checks"]["heldout_delta_lcb_positive"])
            # A positive point estimate with a lower bound straddling zero
            # cannot be claimed as a pass, so it can never reach champion.
            with self.assertRaises(ValueError):
                ledger.record_verified_gate(run["run_id"], envelope)

    def test_champion_ranks_by_lower_bound_not_raw_heldout_delta(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            steady = self._validated_candidate(
                ledger, "ibr.baseline", "lower mean, tighter spread", self.STEADY)
            spiky = self._validated_candidate(
                ledger, "ibr.range.45", "higher mean, wider spread", self.SPIKY)
            selected = ledger.select_champion(vehicle="equity", min_confidence=.9)
            self.assertIsNotNone(selected)
            # Ranking on raw held-out delta would have picked ``spiky``.
            self.assertEqual(selected["candidate_id"], steady)
            self.assertNotEqual(selected["candidate_id"], spiky)

    def test_new_champion_keeps_previous_proved_edge_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.baseline", vehicle="equity", hypothesis="proved edge 1",
                config={"strategy": {"target_r": 2.0}})
            first_id = candidate["candidate_id"]
            _persist_gate(ledger, first_id, "backtest")
            ledger.transition(first_id, "backtest_passed", reason="backtest proof")
            _persist_gate(ledger, first_id, "shadow")
            ledger.transition(first_id, "shadow", reason="shadow proof")
            ledger.transition(first_id, "validated", reason="validated proof")
            first = ledger.select_champion(vehicle="equity", min_confidence=.9)
            self.assertIsNotNone(first)

            candidate = ledger.register_candidate(
                "ibr.target.3r", vehicle="equity", hypothesis="proved edge 2",
                config={"strategy": {"target_r": 3.0}})
            second_id = candidate["candidate_id"]
            _persist_gate(ledger, second_id, "backtest", score=2.0)
            ledger.transition(second_id, "backtest_passed", reason="backtest proof")
            _persist_gate(ledger, second_id, "shadow", score=2.0)
            ledger.transition(second_id, "shadow", reason="shadow proof")
            ledger.transition(second_id, "validated", reason="validated proof")

            # Explicit evidence-authorized champion selection is also used by
            # the conservative ranker when a stronger candidate appears.
            ledger.transition(second_id, "champion", reason="stronger conservative evidence")
            selected = ledger.select_champion(vehicle="equity", min_confidence=.9)
            self.assertIsNotNone(selected)
            self.assertEqual((ledger.candidate(first_id) or {})["status"], "validated")
            self.assertEqual((ledger.candidate(second_id) or {})["status"], "champion")

    def test_auto_requires_a_later_forward_tail_for_shadow(self):
        registry = {"variants": [
            {"variant_id": "ibr.baseline", "strategy_id": "ibr",
             "base_version": "v1", "overrides": {}, "vehicles": ["equity"],
             "hypothesis": "registered baseline"},
            {"variant_id": "ibr.target.1_5r", "strategy_id": "ibr",
             "base_version": "v1", "overrides": {"strategy.target_r": 1.5},
             "vehicles": ["equity"], "hypothesis": "earlier target"},
        ]}
        config = {"strategy": {"range_minutes": 1, "range_stop": True,
                                "target_r": 2}}
        first = _sessions(datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc), 20)
        later = _sessions(datetime(2024, 2, 1, 14, 30, tzinfo=timezone.utc), 20)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_one = root / "first.jsonl"
            data_all = root / "all.jsonl"
            variants = root / "variants.yaml"
            db = root / "edge.sqlite3"
            data_one.write_text("\n".join(json.dumps(row) for row in first), encoding="utf-8")
            data_all.write_text("\n".join(json.dumps(row) for row in first + later), encoding="utf-8")
            variants.write_text(json.dumps(registry), encoding="utf-8")

            initial = discover(data_one, db_path=db, variants_path=variants,
                               config=config, min_trades=5, min_sessions=5,
                               lane="auto")
            candidate = initial["variants"][0]
            self.assertEqual(candidate["status"], "backtest_passed")
            self.assertIsNone(candidate["shadow_run_id"])

            with self.assertRaises(DiscoveryError):
                discover(data_one, db_path=db, variants_path=variants,
                         config=config, min_trades=5, min_sessions=5,
                         lane="shadow")

            forward = discover(data_all, db_path=db, variants_path=variants,
                               config=config, min_trades=5, min_sessions=5,
                               lane="auto")
            candidate = forward["variants"][0]
            self.assertIn(candidate["status"], {"validated", "champion"})
            self.assertEqual(candidate["mode"], "shadow")
            self.assertEqual(candidate["unseen_sessions"], 20)
            trades = EdgeLedger(db).trades(candidate["candidate_id"], lane="shadow")
            self.assertTrue(trades)
            self.assertTrue(all(row["session_date"] >= "2024-02-01" for row in trades))


def _drift_sessions(start: datetime, count: int,
                    symbols=("SPY", "QQQ", "IWM", "DIA")) -> list[dict]:
    """One session-long move and nothing else: pure directional drift.

    The 1.5R variant still beats its 2R baseline here, so every control the
    IBR lane had before this change is satisfied.  But the move is the whole
    session, so an entry chosen at random catches the same move: none of the
    P&L is bought by the timing, which is what the null control exists to say.
    """
    rows: list[dict] = []
    for offset in range(count):
        session = start + timedelta(days=offset)
        values = [
            (100, 101, 99, 100),     # one-minute opening range
            (100, 102, 99, 102),     # confirmed breakout
            (102, 103, 101, 102),    # next-bar entry
            (102, 107, 101, 106),    # 1.5R target, but not 2R
        ]
        for index, symbol in enumerate(symbols):
            shift = index * .01
            for minute, (open_, high, low, close) in enumerate(values):
                rows.append({
                    "symbol": symbol,
                    "timestamp": (session + timedelta(minutes=minute)).isoformat(),
                    "open": open_ + shift, "high": high + shift,
                    "low": low + shift, "close": close + shift,
                    "volume": 1, "provider": "alpaca", "feed": "sip",
                })
    return rows


class IbrLaneEvidenceParityTests(unittest.TestCase):
    """The IBR lane carries the factory lane's null control and sealed window."""

    REGISTRY = {"variants": [
        {"variant_id": "ibr.baseline", "strategy_id": "ibr",
         "base_version": "v1", "overrides": {}, "vehicles": ["equity"],
         "hypothesis": "registered baseline"},
        {"variant_id": "ibr.target.1_5r", "strategy_id": "ibr",
         "base_version": "v1", "overrides": {"strategy.target_r": 1.5},
         "vehicles": ["equity"], "hypothesis": "earlier target"},
    ]}
    CONFIG = {"strategy": {"range_minutes": 1, "range_stop": True, "target_r": 2}}

    def _discover(self, rows, directory, **kwargs):
        root = Path(directory)
        data = root / "corpus.jsonl"
        variants = root / "variants.yaml"
        data.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        variants.write_text(json.dumps(self.REGISTRY), encoding="utf-8")
        options = {"min_trades": 5, "min_sessions": 5, "lane": "auto", **kwargs}
        return discover(data, db_path=root / "edge.sqlite3", variants_path=variants,
                        config=self.CONFIG, **options)

    def test_the_gate_reports_a_null_control_and_a_sealed_window(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._discover(
                _sessions(datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc), 20),
                directory)
        gate = result["variants"][0]["gate"]
        for name in ("null_control_available", "null_control_delta_positive",
                     "qualification_net_positive", "qualification_delta_positive"):
            self.assertIn(name, gate["checks_without_family"])
            self.assertTrue(gate["checks_without_family"][name], name)
        self.assertEqual(gate["null_control"]["kind"], "randomized_entry_null")
        self.assertGreater(gate["null_control"]["matched"], 0)
        self.assertTrue(gate["qualification"]["available"])
        # The sealed window is the last fifth of the corpus and is scored, not
        # split, so it appears in the persisted decision as its own evidence.
        self.assertEqual(gate["qualification"]["sessions"],
                         ["2024-01-18", "2024-01-19", "2024-01-20", "2024-01-21"])
        envelope = gate["verified_gate"]
        self.assertEqual(envelope["null_control"], gate["null_control"])
        self.assertEqual(envelope["qualification"], gate["qualification"])
        self.assertEqual(envelope["walk_forward"], gate["walk_forward"])

    def test_the_sealed_window_never_reaches_selection_or_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._discover(
                _sessions(datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc), 20),
                directory)
            candidate = result["variants"][0]
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            trades = ledger.trades(candidate["candidate_id"])
        sealed = set(candidate["gate"]["qualification"]["sessions"])
        self.assertTrue(trades)
        self.assertFalse(sealed & {str(row["session_date"]) for row in trades})
        counts = candidate["gate"]["verified_gate"]["counts"]
        self.assertEqual(counts["total"]["trades"],
                         counts["fit"]["trades"] + counts["heldout"]["trades"])

    def test_a_drift_edge_beats_its_baseline_and_still_fails_the_null(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._discover(
                _drift_sessions(datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc), 20),
                directory)
        candidate = result["variants"][0]
        checks = candidate["gate"]["checks_without_family"]
        self.assertTrue(checks["heldout_delta_positive"])
        self.assertTrue(checks["heldout_net_pnl_positive"])
        self.assertTrue(checks["null_control_available"])
        # Everything the lane could ask before this change is satisfied; only
        # the chance-entry comparison is not.
        self.assertFalse(checks["null_control_delta_positive"])
        self.assertFalse(candidate["gate"]["passes"])
        self.assertEqual(candidate["status"], "retired")

    def test_a_corpus_too_thin_to_seal_a_window_is_underpowered_not_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._discover(
                _sessions(datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc), 1),
                directory, min_trades=1, min_sessions=1)
            candidate = result["variants"][0]
            events = EdgeLedger(Path(directory) / "edge.sqlite3").history(
                candidate["candidate_id"])
        self.assertFalse(candidate["gate"]["qualification"]["available"])
        # Structural inadequacy, never a failure: a thin corpus must not retire
        # a hypothesis or otherwise churn the lifecycle.
        self.assertEqual(candidate["status"], "candidate")
        self.assertIn("insufficient_data", {event["event_type"] for event in events})


if __name__ == "__main__":
    unittest.main()
