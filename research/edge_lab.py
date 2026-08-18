"""Deterministic discovery and evaluation for Alpaca intraday edges."""

from __future__ import annotations

from datetime import date, datetime
import json
import math
from pathlib import Path
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from .edge_ledger import (
    BACKTEST_PASSED, CANDIDATE, CHAMPION, DEFAULT_DB_PATH, DEMOTED, EdgeLedger,
    LANES, LIFECYCLE, PAPER_DEMOTION_MIN_OUTCOMES, PAPER_DEMOTION_R_FLOOR,
    RETIRED, SCHEMA_VERSION, SHADOW, VALIDATED, VEHICLES, canonical_json,
    content_hash, hash_config, hash_dataset, hash_file, hash_provenance,
    init_db, init_ledger, provenance_hash,
)
from .gates import (
    chronological_split, deterministic_placebo_deltas, falsification_gate,
    expectancy_rejection_report, heldout_separation, matched_cluster_test,
    max_drawdown_of, paired_delta, performance_floor, qualification_report,
    sample_counts, seal_final_window, structural_floor, verified_gate_envelope,
    walk_forward_report, validate_protocol_floor,
    PROTOCOL_SHADOW_MIN_TRADES, PROTOCOL_SHADOW_MIN_SESSIONS,
)
from .costs import CostModel, ReplayPolicy, replay_policy_for_mode
from .ibr import IBRConfig, replay_ibr
from .market_data import (
    OptionSnapshot, UnderlyingBar, normalize_option_snapshot,
    normalize_quote, normalize_underlying_bar,
)
from .stats import benjamini_hochberg


from .edge_discovery_core import (
    DiscoveryError, _bar_session, _read_discovery_rows, _effective_ibr_config,
    _null_reference_rows, _opportunity_rows, _discover_gate, _finalize_gate,
    null_control_account,
)
from .edge_identity import candidate_assumptions


# One regular session of one-minute bars: the randomized-entry null holds to
# the session boundary, which is where the IBR replay is force-flat.
SESSION_BARS = 390


def _null_spec(cfg) -> dict:
    """The geometry the randomized-entry null replays this variant with.

    The null needs one reward-to-risk ratio and one hold horizon.  The ratio
    is the variant's own — explicit ``target_r``, or the percentage pair's
    implied ratio, exactly as :mod:`research.ibr` resolves it.  The horizon is
    a whole regular session because an IBR position is closed by the session
    boundary rather than by a bounded rule's bar count.
    """
    ratio = (float(cfg.target_r) if cfg.target_r is not None
             else float(cfg.target_pct) / float(cfg.stop_pct))
    return {"target_r": ratio, "max_hold_bars": SESSION_BARS}


def _adequate(gate: Mapping) -> bool:
    """Whether this gate was decided on structurally sufficient evidence.

    Floors, a usable rolling-origin walk-forward, and a released final
    qualification window are all sample-size statements.  A corpus that cannot
    supply one of them is underpowered, not failed, and must not retire a
    hypothesis on evidence it never had.
    """
    return bool(gate.get("fit_floor", {}).get("adequate") and
                gate.get("heldout_floor", {}).get("adequate") and
                (gate.get("walk_forward") or {}).get("available") and
                (gate.get("qualification") or {}).get("available"))


def _development_adequate(gate: Mapping) -> bool:
    """Whether the reusable development evidence met its structural floors."""
    return bool(gate.get("fit_floor", {}).get("adequate") and
                gate.get("heldout_floor", {}).get("adequate") and
                (gate.get("walk_forward") or {}).get("available") and
                (gate.get("walk_forward") or {}).get("adequate"))


def _classification(gate: Mapping) -> str:
    if gate.get("passes") is True:
        return "proved"
    if not _development_adequate(gate):
        return "underpowered"
    absolute = gate.get("heldout_performance") or {}
    try:
        if (float(absolute.get("net_pnl")) <= 0.0 and
                float(absolute.get("expectancy")) <= 0.0):
            rejection = gate.get("retirement_evidence") or {}
            return ("adequate_negative_rejection" if
                    rejection.get("rejects_minimum_useful_edge") is True and
                    rejection.get("multi_window_negative") is True else
                    "adequate_negative_inconclusive")
    except (TypeError, ValueError, OverflowError):
        pass
    return "adequate_inconclusive"


def _strengthen_gate(gate: dict, baseline: Sequence[Mapping], *, vehicle: str) -> dict:
    """Add absolute profitability, lower-bound and walk-forward requirements.

    Beating a control is necessary but not sufficient: an accepted variant
    must also make money after costs on unseen sessions, keep a positive lower
    confidence bound, and hold up across a majority of rolling-origin folds.
    """
    heldout = gate["_heldout_rows"]
    sessions = {str(row.get("session_date") or "") for row in heldout}
    base_heldout = [row for row in baseline
                    if str(row.get("session_date") or "") in sessions]
    absolute = performance_floor(heldout, vehicle=vehicle)
    minimums = (gate.get("heldout_floor") or {}).get("minimums") or {}
    folds = 3
    walk = walk_forward_report(
        heldout, base_heldout, vehicle=vehicle, folds=folds,
        min_test_sessions=max(1, int(minimums.get("sessions", 1)) // folds),
        min_test_trades=max(1, int(minimums.get("trades", 1)) // (folds * 2)))
    rejection = expectancy_rejection_report(heldout, vehicle=vehicle)
    negative_folds = [item for item in walk.get("results", ())
                      if item.get("adequate") and
                      float(item.get("net_pnl", 0.0)) <= 0.0]
    rejection["negative_forward_folds"] = len(negative_folds)
    rejection["independent_negative_windows"] = [
        list(item.get("test_sessions") or ()) for item in negative_folds]
    rejection["multi_window_negative"] = len(negative_folds) >= 2
    bound = gate["heldout_paired_baseline"].get("mean_delta_lcb")
    gate["checks_without_family"].update({
        "heldout_net_pnl_positive": bool(absolute["net_pnl_positive"]),
        "heldout_expectancy_positive": bool(absolute["expectancy_positive"]),
        "heldout_delta_lcb_positive": bool(bound is not None and float(bound) > 0),
        "walk_forward_majority_positive": bool(walk["available"] and
                                               walk["majority_positive"]),
        "walk_forward_adequate": bool(walk.get("adequate")),
    })
    development_checks = {
        name: value for name, value in gate["checks_without_family"].items()
        if name not in {"qualification_net_positive",
                        "qualification_delta_positive"}
    }
    gate["development_passes_without_family"] = bool(
        all(development_checks.values()) and
        gate["paired_baseline"].get("mean_delta") is not None and
        float(gate["paired_baseline"]["mean_delta"]) > 0)
    gate["passes_without_family"] = bool(
        all(gate["checks_without_family"].values()) and
        gate["paired_baseline"].get("mean_delta") is not None and
        float(gate["paired_baseline"]["mean_delta"]) > 0)
    gate["heldout_performance"] = absolute
    gate["walk_forward"] = walk
    gate["retirement_evidence"] = rejection
    gate["heldout_delta_lcb"] = bound
    return gate


def discover(data: str | Path | Sequence[Mapping], *, db_path: str | Path = DEFAULT_DB_PATH,
             vehicle: str = "equity", lane: str = "auto", config: Mapping | None = None,
             variants_path: str | Path | None = None,
             min_trades: int = 100,
             min_sessions: int = 30, alpha: float = .05,
             backtest_bar_fallback: bool = False) -> dict:
    """Run every bounded IBR variant on one normalized corpus.

    ``backtest`` writes only the fit/held-out replay and can advance a fresh
    candidate to ``backtest_passed``.  ``shadow`` requires an existing
    backtest-passed candidate and consumes only sessions strictly later than
    the latest persisted boundary.  ``auto`` chooses backtest for fresh arms
    and the same forward-only tail for already-qualified arms.  Missing quotes
    remain unpriceable except in the explicit historical bar-fallback lane.
    """
    if vehicle not in VEHICLES:
        raise DiscoveryError("vehicle must be equity or option")
    if lane not in {"auto", "backtest", "shadow"}:
        raise DiscoveryError("lane must be auto, backtest, or shadow")
    if not isinstance(backtest_bar_fallback, bool):
        raise DiscoveryError("backtest_bar_fallback must be true or false")
    # These values are policy inputs, not harmless replay hints.  Reject bools,
    # fractional counts, non-finite values and out-of-range alpha before any
    # corpus is sealed or a candidate is registered.
    # The requested lane is an authorizing write boundary.  Compact samples
    # remain available to local gate helpers, but discovery must reject a
    # lowered protocol before reading/sealing the corpus or touching a ledger.
    floor_lane = "shadow" if lane == "shadow" else "backtest"
    try:
        validate_protocol_floor(lane=floor_lane, min_trades=min_trades,
                                min_sessions=min_sessions)
    except ValueError as exc:
        raise DiscoveryError(str(exc)) from exc
    if (isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or
            not math.isfinite(float(alpha)) or not 0.0 < float(alpha) <= 1.0):
        raise DiscoveryError("alpha must be finite and in (0,1]")
    # Discovery is an authorizing boundary: preserve each equity row's own
    # provider/feed identity and require the configured complete SIP feed.
    broker = (config or {}).get("broker") if isinstance(config, Mapping) else {}
    configured_feed = ((broker or {}).get("data_feed", "sip")
                       if isinstance(broker, Mapping) else "sip")
    raw_rows, bars, snapshots, quotes = _read_discovery_rows(
        data, require_provenance=True,
        expected_equity_feed=str(configured_feed or "sip"))
    from agent.variants import load_registry
    registry_path = variants_path or Path(__file__).with_name("variants.yaml")
    variants = load_registry(registry_path)
    selected = [variant for variant in variants.values()
                if variant.strategy_id == "ibr" and vehicle in variant.vehicles]
    baseline_variant = variants.get("ibr.baseline")
    if baseline_variant is None:
        raise DiscoveryError("research/variants.yaml must register ibr.baseline")
    if baseline_variant not in selected:
        selected.insert(0, baseline_variant)
    candidates = [variant for variant in selected
                  if variant.variant_id != baseline_variant.variant_id]
    ledger = EdgeLedger(db_path)
    code_path = Path(__file__)
    data_hash = content_hash(raw_rows)

    # The latest sessions are sealed before any variant is replayed, so no
    # gate, correction or champion comparison can reach them; the window is
    # opened once, below, after every variant has been scored.
    development_bars, sealed_window = seal_final_window(
        bars, session_of=_bar_session, fraction=.2)
    sealed_sessions = set(sealed_window.session_dates)
    sealed_snapshots = {key: snap for key, snap in snapshots.items()
                        if snap.session_date.isoformat() in sealed_sessions}
    # A disk-backed quote resolver already applies the session predicate at
    # each fill boundary.  Reusing it for both windows avoids copying millions
    # of quote snapshots into separate sealed/development lists; in-memory
    # callers retain the historical exact slices.
    quote_resolver = callable(getattr(quotes, "quote_fill", None))
    if quote_resolver:
        sealed_quotes = development_quotes = quotes
    else:
        sealed_quotes = [quote for quote in quotes
                         if quote.session_date.isoformat() in sealed_sessions]
        development_quotes = [quote for quote in quotes
                              if quote.session_date.isoformat() not in sealed_sessions]
    development_snapshots = {key: snap for key, snap in snapshots.items()
                             if snap.session_date.isoformat() not in sealed_sessions}

    def replay(variant_bars, variant_snapshots, variant_quotes, cfg):
        return replay_ibr(
            variant_bars, config=cfg, vehicle=vehicle,
            option_snapshots=variant_snapshots if vehicle == "option" else None,
            quotes=variant_quotes if vehicle == "equity" else None)

    base_results: dict[str, list[dict]] = {}
    null_results: dict[str, list[dict]] = {}
    effective_configs: dict[str, dict] = {}
    run_configs: dict[str, dict] = {}
    configs: dict[str, object] = {}
    runtime_policy = ReplayPolicy.from_config(config)
    for variant in selected:
        cfg, effective = _effective_ibr_config(config, variant.overrides)
        effective_configs[variant.variant_id] = candidate_assumptions(
            effective, vehicle=vehicle, strategy_id="ibr",
            variant_id=variant.variant_id)
        configs[variant.variant_id] = cfg

    qualification_rows: dict[str, list[dict]] = {}

    def latest_boundary(record: Mapping | None) -> str | None:
        if not record:
            return None
        values = []
        for run in ledger.runs(record["candidate_id"]):
            value = run.get("heldout_end") or run.get("fit_end")
            if value:
                values.append(str(value))
            # A sealed window that was scored was consumed: the forward-only
            # boundary has to clear it, or the next cycle would develop on
            # sessions this one already qualified against.
            window = ((run.get("metrics") or {}).get("gate") or {}).get("qualification") or {}
            for session in (window.get("sessions") or ()):
                values.append(str(session))
        return max(values) if values else None

    existing = {variant.variant_id: ledger.candidate_by_variant(variant.variant_id, vehicle)
                for variant in selected}
    # Candidate rows are immutable.  A legacy row whose stored assumptions do
    # not equal the canonical validated envelope cannot be silently reused or
    # given a new proof under a different config; stop the lane before any run
    # or gate is persisted and require an explicit re-registration.
    for variant in selected:
        prior = existing.get(variant.variant_id)
        if prior is None:
            continue
        expected_hash = content_hash(effective_configs[variant.variant_id])
        if str(prior.get("config_hash") or "") != expected_hash:
            raise DiscoveryError(
                f"candidate {variant.variant_id!r} has immutable assumptions "
                "from an older runtime config; re-register before discovery")
    active = {"backtest_passed", "shadow", "validated", "champion"}
    if lane == "shadow":
        if existing.get(baseline_variant.variant_id) is None:
            raise DiscoveryError(
                "shadow requires a prior backtest baseline; run edge discover --lane backtest first")
        for variant in candidates:
            record = existing.get(variant.variant_id)
            if record is None:
                raise DiscoveryError(
                    f"shadow requires prior backtest candidate {variant.variant_id!r}")
            if record.get("status") not in active:
                raise DiscoveryError(
                    f"candidate {variant.variant_id!r} is {record.get('status')!r}; "
                    "shadow requires backtest_passed or later")

    baseline_boundary = latest_boundary(existing.get(baseline_variant.variant_id))
    baseline_mode = "shadow" if (
        lane == "shadow" or (lane == "auto" and baseline_boundary is not None)
    ) else "backtest"

    def consumed_sessions(*groups: Sequence[Mapping]) -> int:
        """Unseen sessions this lane consumed, development and sealed alike."""
        return len({str(row.get("session_date") or "")
                    for group in groups for row in group})

    def tail(rows: Sequence[Mapping], boundary: str | None) -> list[dict]:
        if boundary is None:
            return [dict(row) for row in rows]
        return [dict(row) for row in rows
                if str(row.get("session_date") or "") > str(boundary)]

    baseline_window_rows: list[dict] = []

    def final_window(rows: Sequence[Mapping], control: Sequence[Mapping], *,
                     candidate_id: str, preselected: bool) -> dict:
        """Score one variant over the sealed sessions: go/no-go, never diagnosis."""
        return qualification_report(
            rows, control, vehicle=vehicle,
            sessions=sorted({str(row.get("session_date") or "") for row in rows}),
            candidate_id=candidate_id, preselected=preselected)

    modes: dict[str, str] = {}
    eval_rows: dict[str, list[dict]] = {}
    window_rows: dict[str, list[dict]] = {}
    window_reports: dict[str, dict] = {}
    for variant in candidates:
        record = existing.get(variant.variant_id)
        if record is not None and record.get("status") == "retired":
            # A failed, adequately-powered evaluation closes this hypothesis
            # for every lane, including an explicitly requested backtest.
            mode = "skip"
        elif record is not None and record.get("status") == "demoted":
            # Demotion is a reversible safety state.  Only a strictly later
            # unseen shadow sample may re-prove it; a repeated backtest cannot.
            mode = "shadow" if lane in {"auto", "shadow"} else "skip"
        elif lane == "backtest":
            mode = "backtest"
        elif lane == "shadow":
            mode = "shadow"
        elif record is not None and record.get("status") in active:
            mode = "shadow"
        else:
            mode = "backtest"
        modes[variant.variant_id] = mode
        boundary = latest_boundary(record)
        eval_rows[variant.variant_id] = []
        window_rows[variant.variant_id] = []
        window_reports[variant.variant_id] = {
            "available": False, "sessions": [], "net_positive": False,
            "delta_positive": False,
            "post_selection": {"preselected": False,
                                "candidate_id": variant.variant_id},
        }
        # Replay rows are populated below after the mode-specific policy is
        # selected; this keeps auto-lane backtests and shadows from sharing a
        # permissive/strict pricing decision accidentally.

    # ``auto`` may resolve to a shadow lane for an existing candidate.  Keep
    # the caller's backtest floor for fresh arms, but raise the effective
    # shadow floor to the immutable live-shadow protocol before replay/gate
    # construction.  This keeps scheduled auto cycles operational while never
    # persisting a shadow proof below 150 trades and 30 sessions.
    effective_minimums = {
        "backtest": (int(min_trades), int(min_sessions)),
        "shadow": (max(int(min_trades), PROTOCOL_SHADOW_MIN_TRADES),
                    max(int(min_sessions), PROTOCOL_SHADOW_MIN_SESSIONS)),
    }

    # Select the mode-specific replay policy only after auto-lane modes are
    # known. Candidate registration retains the stable strict config above;
    # mode-specific policy belongs to the replay run and its provenance.
    mode_policies: dict[str, ReplayPolicy] = {}
    for variant in selected:
        variant_id = variant.variant_id
        mode = (baseline_mode if variant_id == baseline_variant.variant_id
                else modes.get(variant_id, "backtest"))
        mode_policy = replay_policy_for_mode(
            runtime_policy, "shadow" if mode == "shadow" else "backtest",
            backtest_bar_fallback=backtest_bar_fallback)
        mode_policies[variant_id] = mode_policy
        cfg, effective = _effective_ibr_config(
            config, variant.overrides, policy=mode_policy)
        configs[variant_id] = cfg
        # Keep lane-specific policy in provenance only.  The immutable
        # candidate/run config is identical across backtest and shadow.
        run_configs[variant_id] = dict(effective_configs[variant_id])
        if mode == "skip":
            base_results[variant_id] = []
            null_results[variant_id] = []
            continue
        result = replay(development_bars, development_snapshots,
                        development_quotes, cfg)
        base_results[variant_id] = _opportunity_rows(
            result, development_bars, vehicle)
        null_results[variant_id] = null_control_account(
            development_bars, list(development_snapshots.values()),
            _null_spec(cfg), vehicle=vehicle,
            reference_rows=_null_reference_rows(result, development_bars, vehicle),
            account_id=f"ibr:{vehicle}:{variant_id}", costs=cfg.costs,
            quotes=development_quotes, fixed_quantity=cfg.quantity,
            policy=mode_policy)["rows"]

        boundary = latest_boundary(existing.get(variant_id))
        eval_rows[variant_id] = (tail(base_results[variant_id], boundary)
                                 if mode == "shadow" else base_results[variant_id])

    # A mixed auto cycle can contain fresh backtests and forward shadows. Each
    # candidate must be compared with a baseline replayed under its own mode
    # policy, not with whichever policy the baseline arm happened to use.
    baseline_by_mode: dict[str, list[dict]] = {}
    for mode in {"backtest", "shadow"}:
        mode_policy = replay_policy_for_mode(
            runtime_policy, mode, backtest_bar_fallback=backtest_bar_fallback)
        baseline_cfg, _ = _effective_ibr_config(
            config, baseline_variant.overrides, policy=mode_policy)
        baseline_result = replay(development_bars, development_snapshots,
                                 development_quotes, baseline_cfg)
        baseline_by_mode[mode] = _opportunity_rows(
            baseline_result, development_bars, vehicle)

    baseline_rows = base_results[baseline_variant.variant_id]
    baseline_eval = (tail(baseline_rows, baseline_boundary)
                     if baseline_mode == "shadow" else baseline_rows)
    unseen_sealed_sessions = [
        session for session in sealed_window.session_dates
        if baseline_boundary is None or session > baseline_boundary]
    if lane == "shadow" and not baseline_eval and not unseen_sealed_sessions:
        raise DiscoveryError("shadow corpus contains no unseen sessions after the persisted boundary")
    for variant in candidates:
        boundary = latest_boundary(existing.get(variant.variant_id))
        has_unseen_sealed = any(
            boundary is None or session > boundary
            for session in sealed_window.session_dates)
        if lane == "shadow" and not eval_rows[variant.variant_id] and not has_unseen_sealed:
            raise DiscoveryError(
                f"shadow corpus contains no unseen sessions for {variant.variant_id!r}")

    baseline_zero = [{**row, "net_pnl": 0.0, "return_value": 0.0}
                     for row in baseline_eval]
    gates = {}
    for variant in candidates:
        mode = modes[variant.variant_id]
        if mode == "skip":
            continue
        candidate_baseline = (baseline_eval if mode == baseline_mode else
                              tail(baseline_by_mode[mode], baseline_boundary)
                              if mode == "shadow" else baseline_by_mode[mode])
        gates[variant.variant_id] = _strengthen_gate(
            _discover_gate(
                eval_rows[variant.variant_id], candidate_baseline,
                vehicle=vehicle,
                min_trades=effective_minimums[mode][0],
                min_sessions=effective_minimums[mode][1], alpha=alpha,
                shadow=(mode == "shadow"),
                null_rows=null_results[variant.variant_id],
                qualification=window_reports[variant.variant_id]),
            candidate_baseline, vehicle=vehicle)
    corrected = benjamini_hochberg(
        {variant_id: gate["candidate_p_raw"] for variant_id, gate in gates.items()}, alpha=alpha)

    ranked_ids = sorted((
        variant_id for variant_id in gates
        if gates[variant_id].get("development_passes_without_family") and
        corrected.get(variant_id, {}).get("significant")), key=lambda variant_id: (
        float(corrected.get(variant_id, {}).get("p_adjusted", 1.0)),
        -float(gates[variant_id].get("heldout_delta_lcb") or float("-inf")),
        -float((gates[variant_id].get("heldout_performance") or {}).get(
            "net_pnl", 0.0)),
        variant_id))
    selected_test_id = ranked_ids[0] if ranked_ids else None
    # Imported lazily because factory_ledger imports this module's public
    # facades. Historical discovery defers cumulative testing to live shadow.
    from .factory_ledger import deferred_fdr
    confirmatory_scope = f"shadow-confirmation-v2:{vehicle}"
    cumulative = deferred_fdr(
        confirmatory_scope,
        (f"{data_hash}:{lane}:{selected_test_id}:live-shadow"
         if selected_test_id else f"{data_hash}:{lane}:no-selection"))
    qualification_target = selected_test_id

    if qualification_target is not None and sealed_window.session_dates:
        window_bars = sealed_window.release(
            reason=f"post-selection qualification {qualification_target}")
        qualification_mode = modes[qualification_target]
        qualification_policy = replay_policy_for_mode(
            runtime_policy, "shadow" if qualification_mode == "shadow" else "backtest",
            backtest_bar_fallback=backtest_bar_fallback)
        qualification_baseline_cfg, _ = _effective_ibr_config(
            config, baseline_variant.overrides, policy=qualification_policy)
        baseline_all = _opportunity_rows(
            replay(window_bars, sealed_snapshots, sealed_quotes,
                   qualification_baseline_cfg),
            window_bars, vehicle)
        variant_cfg = configs[qualification_target]
        candidate_all = _opportunity_rows(
            replay(window_bars, sealed_snapshots, sealed_quotes, variant_cfg),
            window_bars, vehicle)
        target_boundary = latest_boundary(existing.get(qualification_target))
        baseline_window_rows = tail(
            baseline_all, target_boundary
            if modes[qualification_target] == "shadow" else None)
        candidate_window = tail(
            candidate_all, target_boundary
            if modes[qualification_target] == "shadow" else None)
        qualification_rows[baseline_variant.variant_id] = baseline_window_rows
        qualification_rows[qualification_target] = candidate_window
        window_rows[qualification_target] = candidate_window
        window_reports[qualification_target] = final_window(
            candidate_window, baseline_window_rows,
            candidate_id=qualification_target, preselected=True)
        gate = gates[qualification_target]
        gate["qualification"] = window_reports[qualification_target]
        gate["checks_without_family"]["qualification_net_positive"] = bool(
            gate["qualification"].get("available") and
            gate["qualification"].get("net_positive"))
        gate["checks_without_family"]["qualification_delta_positive"] = bool(
            gate["qualification"].get("available") and
            gate["qualification"].get("delta_positive"))
        gate["passes_without_family"] = bool(
            all(gate["checks_without_family"].values()) and
            gate["paired_baseline"].get("mean_delta") is not None and
            float(gate["paired_baseline"]["mean_delta"]) > 0)

    # Attach the family decision before writing any shadow rows.  A failed or
    # under-powered forward check is not "consumed": the same unseen tail may
    # be reconsidered when the append-only recorder supplies more sessions.
    run_provenances: dict[str, dict] = {}
    for variant in candidates:
        if modes[variant.variant_id] == "skip":
            continue
        gate = gates[variant.variant_id]
        family = corrected.get(variant.variant_id, {"p": gate["candidate_p_raw"],
                                                     "p_adjusted": 1.0,
                                                     "significant": False})
        run_provenance = {
            "lane": lane, "vehicle": vehicle,
            "replay_mode": modes[variant.variant_id],
            "replay_policy": mode_policies[variant.variant_id].as_dict(),
            "backtest_bar_fallback": backtest_bar_fallback,
            "selected_test_id": selected_test_id,
            "qualified_test_id": qualification_target,
            "cumulative_fdr": (dict(cumulative)
                               if variant.variant_id == selected_test_id else None),
        }
        run_provenances[variant.variant_id] = run_provenance
        _finalize_gate(
            gate, lane=modes[variant.variant_id], family=family,
            online_fdr=(cumulative if variant.variant_id == selected_test_id else {}),
            provenance=provenance_hash(
                dataset=raw_rows, config=run_configs[variant.variant_id],
                code=code_path, provenance=run_provenance),
            candidate_id=variant.variant_id,
            costs=configs[variant.variant_id].costs)
    forward_success = any(
        modes[variant.variant_id] == "shadow" and
        gates.get(variant.variant_id, {}).get("passes", False)
        for variant in candidates)

    results = []
    baseline_record = ledger.register_candidate(
        baseline_variant.variant_id, vehicle=vehicle, strategy_id="ibr",
        base_version=baseline_variant.base_version, hypothesis=baseline_variant.hypothesis,
        config=effective_configs[baseline_variant.variant_id], dataset=raw_rows,
        code=code_path, provenance={"lane": lane, "vehicle": vehicle, "role": "baseline"},
        overrides=baseline_variant.overrides)
    # The baseline's own control is the synthetic zero reference, in the final
    # window exactly as in the development one. It is a control arm, not a
    # separately selected hypothesis, so it never receives an authorizing
    # qualification report of its own.
    baseline_window = final_window(
        [], [],
        candidate_id=baseline_variant.variant_id, preselected=False)
    baseline_gate = _strengthen_gate(
        _discover_gate(
            baseline_eval, baseline_zero, vehicle=vehicle,
            min_trades=effective_minimums[baseline_mode][0],
            min_sessions=effective_minimums[baseline_mode][1], alpha=alpha,
            shadow=(baseline_mode == "shadow"), actual_control=False,
            control_kind="synthetic_zero_reference",
            null_rows=null_results[baseline_variant.variant_id],
            qualification=baseline_window),
        baseline_zero, vehicle=vehicle)
    _finalize_gate(
        baseline_gate, lane=baseline_mode,
        family={"p": baseline_gate["candidate_p_raw"],
                "p_adjusted": baseline_gate["candidate_p_raw"],
                "significant": baseline_gate["candidate_p_raw"] <= alpha,
                "family_size": 1},
        online_fdr={},
        provenance=provenance_hash(
            dataset=raw_rows,
            config=run_configs[baseline_variant.variant_id],
            code=code_path,
            provenance={"lane": lane, "vehicle": vehicle,
                        "role": "baseline", "replay_mode": baseline_mode,
                        "replay_policy": mode_policies[baseline_variant.variant_id].as_dict(),
                        "backtest_bar_fallback": backtest_bar_fallback,
                        "selected_test_id": selected_test_id}),
        candidate_id=baseline_variant.variant_id,
        costs=configs[baseline_variant.variant_id].costs)
    baseline_run = None
    # A control arm is never post-selection qualified, so its reusable
    # development run must not be labelled underpowered merely because the
    # candidate-only qualification field is intentionally unavailable.
    baseline_adequate = _development_adequate(baseline_gate)
    if baseline_eval and not baseline_adequate:
        ledger.append_event(
            candidate_id=baseline_record["candidate_id"],
            event_type="insufficient_data", actor="edge_lab",
            reason="baseline sample floor not met; no observations consumed",
            payload={"dataset_hash": data_hash, "mode": baseline_mode,
                     "rows": len(baseline_eval),
                     "trades": baseline_gate.get("floor", {}).get("trades", 0),
                     "sessions": baseline_gate.get("floor", {}).get("sessions", 0),
                     "boundary": baseline_boundary})
    if baseline_eval and baseline_adequate and (baseline_mode != "shadow" or forward_success):
        if baseline_mode == "shadow":
            baseline_fit, baseline_held = [], baseline_eval
        else:
            baseline_fit, baseline_held = chronological_split(baseline_eval, fit_fraction=.7)
        baseline_run = ledger.append_run(
            baseline_record["candidate_id"], lane=baseline_mode, vehicle=vehicle,
            dataset=raw_rows, config=run_configs[baseline_variant.variant_id], code=code_path,
            provenance={"lane": lane, "vehicle": vehicle,
                        "role": "baseline", "replay_mode": baseline_mode,
                        "replay_policy": mode_policies[baseline_variant.variant_id].as_dict(),
                        "backtest_bar_fallback": backtest_bar_fallback},
            fit=baseline_fit, heldout=baseline_held,
            metrics={"gate": baseline_gate, "role": "baseline"})
        for row in baseline_eval:
            ledger.append_trade(baseline_run["run_id"], row)
        ledger.record_verified_gate(baseline_run["run_id"], baseline_gate)
        ledger.append_evidence(baseline_record["candidate_id"], "baseline_control",
                               baseline_gate, run_id=baseline_run["run_id"])

    for variant in candidates:
        mode = modes[variant.variant_id]
        rows = eval_rows[variant.variant_id]
        record = ledger.register_candidate(
            variant.variant_id, vehicle=vehicle, strategy_id="ibr",
            base_version=variant.base_version, hypothesis=variant.hypothesis,
            config=effective_configs[variant.variant_id], dataset=raw_rows,
            code=code_path, provenance={"lane": lane, "vehicle": vehicle},
            overrides=variant.overrides)
        if mode == "skip":
            results.append({"variant_id": variant.variant_id, "vehicle": vehicle,
                            "candidate_id": record["candidate_id"],
                            "status": record.get("status", "retired"),
                            "gate": None, "shadow_gate": None,
                            "run_id": None, "shadow_run_id": None,
                            "classification": "not_retested",
                            "mode": mode, "unseen_sessions": None})
            continue
        gate = gates[variant.variant_id]
        family = gate["multiple_tests"]["candidate"]
        adequate = _adequate(gate)
        selected_for_proof = bool(
            variant.variant_id == qualification_target and
            (gate.get("qualification") or {}).get("available"))
        development_adequate = _development_adequate(gate)
        run = None
        shadow_run = None
        status = record.get("status", "candidate")
        if not rows and mode == "shadow":
            ledger.append_event(
                candidate_id=record["candidate_id"], event_type="insufficient_data",
                actor="edge_lab", reason="no unseen sessions; no observations consumed",
                payload={"dataset_hash": data_hash, "mode": mode,
                         "rows": 0, "trades": 0, "sessions": 0,
                         "boundary": latest_boundary(record)})
        elif rows and not development_adequate:
            ledger.append_event(
                candidate_id=record["candidate_id"], event_type="insufficient_data",
                actor="edge_lab", reason="development sample floor not met; no observations consumed",
                payload={"dataset_hash": data_hash, "mode": mode,
                         "rows": len(rows),
                         "trades": gate.get("floor", {}).get("trades", 0),
                         "sessions": gate.get("floor", {}).get("sessions", 0),
                         "heldout_trades": gate.get("heldout_floor", {}).get("trades", 0),
                         "heldout_sessions": gate.get("heldout_floor", {}).get("sessions", 0),
                         "boundary": latest_boundary(record)})
        elif (variant.variant_id == selected_test_id and
              not sealed_window.session_dates):
            ledger.append_event(
                candidate_id=record["candidate_id"], event_type="insufficient_data",
                actor="edge_lab",
                reason="no sealed qualification window; no observations consumed",
                payload={"dataset_hash": data_hash, "mode": mode,
                         "rows": len(rows), "selected_test_id": selected_test_id})
        elif not selected_for_proof:
            ledger.append_event(
                candidate_id=record["candidate_id"],
                event_type="development_diagnostic", actor="edge_lab",
                reason="candidate was not selected for the sealed qualification window",
                payload={"dataset_hash": data_hash, "mode": mode,
                         "selected_test_id": selected_test_id,
                         "candidate_p": gate.get("candidate_p_raw")})
        elif rows and selected_for_proof:
            if mode == "shadow":
                fit_rows, heldout_rows = [], rows
            else:
                fit_rows, heldout_rows = chronological_split(rows, fit_fraction=.7)
            run = ledger.append_run(
                record["candidate_id"], lane=mode, vehicle=vehicle, dataset=raw_rows,
                config=run_configs[variant.variant_id], code=code_path,
                provenance=run_provenances[variant.variant_id],
                fit=fit_rows, heldout=heldout_rows,
                metrics={"gate": gate, "confidence": 1.0 - family.get("p_adjusted", 1.0),
                         "heldout_delta": gate["heldout_paired_baseline"].get("mean_delta") or 0.0,
                         "max_drawdown": gate["max_drawdown"],
                         "heldout_trades": gate["heldout_trades"],
                         "role": "forward_shadow" if mode == "shadow" else "candidate"})
            for row in rows:
                ledger.append_trade(run["run_id"], row)
            ledger.record_verified_gate(run["run_id"], gate)
            ledger.append_evidence(record["candidate_id"],
                                   "shadow_gate" if mode == "shadow" else "gate_report",
                                   gate, run_id=run["run_id"])
        if adequate and gate["passes"] and mode == "backtest" and lane in {"backtest", "auto"} \
                and status == "candidate":
            ledger.transition(record["candidate_id"], "backtest_passed",
                              reason="pre-registered backtest gates passed")
            status = "backtest_passed"
        if (adequate and gate["passes"] and mode == "shadow" and
                status in {"backtest_passed", "shadow", "demoted"}):
            if status in {"backtest_passed", "demoted"}:
                ledger.transition(record["candidate_id"], "shadow",
                                  reason="later unseen shadow lane started")
                status = "shadow"
            shadow_run = run
            if status == "shadow":
                # Offline forward replay is stability/prerequisite evidence;
                # only the research-side live-shadow ingester may authorize
                # deployment after parity with the runtime shadow decisions.
                status = "shadow"
        absolute = gate.get("heldout_performance") or {}
        terminal_negative = bool(
            adequate and float(absolute.get("net_pnl", 0.0)) <= 0.0 and
            float(absolute.get("expectancy", 0.0)) <= 0.0 and
            (gate.get("retirement_evidence") or {}).get(
                "rejects_minimum_useful_edge") is True and
            (gate.get("retirement_evidence") or {}).get(
                "multi_window_negative") is True)
        if (selected_for_proof and terminal_negative and not gate["passes"] and
                status not in {"retired", "demoted"}):
            target = "demoted" if status in {"shadow", "validated", "champion"} else "retired"
            ledger.transition(record["candidate_id"], target,
                              reason=f"{mode} evidence was adequately powered and terminally negative")
            status = target
        results.append({"variant_id": variant.variant_id, "vehicle": vehicle,
                        "candidate_id": record["candidate_id"], "status": status,
                        "gate": gate,
                        "shadow_gate": gate if mode == "shadow" else None,
                        "run_id": run["run_id"] if run else None,
                        "shadow_run_id": shadow_run["run_id"] if shadow_run else None,
                        "classification": _classification(gate),
                        "mode": mode,
                        "unseen_sessions": (
                            consumed_sessions(rows, window_rows[variant.variant_id])
                            if mode == "shadow" else None)})
    champion = ledger.select_champion(vehicle=vehicle) if lane in {"auto", "shadow"} else None
    classifications: dict[str, int] = {}
    for item in results:
        name = str(item.get("classification") or "unknown")
        classifications[name] = classifications.get(name, 0) + 1
    return {"vehicle": vehicle, "lane": lane, "dataset_hash": data_hash,
            "variants": results, "family_correction": corrected,
            "verdict": {
                "candidate_proved": bool(classifications.get("proved")),
                "classifications": classifications,
                "interpretation": (
                    "candidate_proved" if classifications.get("proved") else
                    "no_candidate_proved"),
            },
            "post_selection": {"selected_test_id": selected_test_id,
                               "qualified_test_id": qualification_target,
                               "cumulative_fdr": cumulative},
            "baseline": {"candidate_id": baseline_record["candidate_id"],
                         "run_id": baseline_run["run_id"] if baseline_run else None,
                         "gate": baseline_gate, "mode": baseline_mode,
                         "unseen_sessions": (
                             consumed_sessions(baseline_eval, baseline_window_rows)
                             if baseline_mode == "shadow" else None)},
            "champion": champion}


__all__ = ["BACKTEST_PASSED", "CANDIDATE", "CHAMPION", "DEFAULT_DB_PATH",
           "DEMOTED", "DiscoveryError", "EdgeLedger", "LANES", "LIFECYCLE",
           "RETIRED", "SHADOW", "VALIDATED", "VEHICLES", "canonical_json",
           "content_hash", "discover", "hash_config", "hash_dataset",
           "hash_file", "hash_provenance", "init_db", "init_ledger",
           "provenance_hash"]
