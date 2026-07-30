"""Registered experimental hypotheses: what `other` should have been.

``setup_type: "other"`` let the model label any idea and bypass the evidence
contract entirely. Two things made it useless for a research programme, and
both are structural rather than fixable by tuning.

**It is an unlabelled bucket.** Every distinct experimental idea the model
ever had was pooled under one string, and ``report.py`` groups by
``setup_type``, so all of them collapsed into a single row. Nothing can be
learned from a category that means "everything else": a row combining four
good ideas and six bad ones reports their average, which describes none of
them.

**It made demo results non-transferable.** ``other`` was demo-only, so the
demo agent traded a population of setups that live would silently refuse, and
every aggregate demo statistic was contaminated by trades that could never
occur in production.

The replacement is a named hypothesis with three things ``other`` never had:
a **mechanism** saying who is on the other side and why they keep losing, a
**falsifier** agreed before any data is seen, and its **own contract** - which
may be permissive, but must be written down. Each is separately attributed,
so ten experiments produce ten rows rather than one average.

A hypothesis that cannot state its mechanism and its falsifier is not ready
to be traded, even on demo. That bar is the entire difference between an
experiment and a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import finite


@dataclass(frozen=True)
class Hypothesis:
    """One named experimental idea, with its own contract and attribution."""

    id: str
    version: str
    claim: str
    mechanism: str
    falsifier: str
    # Returns (ok, reason). A permissive contract is allowed; an absent one
    # is not, because "no contract" is what `other` was.
    contract: object
    prompt_line: str
    # The registered point used by the live/shadow contract. Alternatives
    # live beside it so the hypothesis can be tested as an axis rather than
    # quietly inheriting one hard-coded threshold.
    contract_params: dict = field(default_factory=dict)
    settings: tuple[dict, ...] = ()

    def attribution(self, strategy_id: str) -> tuple[str, str]:
        """Its own strategy id and version, so results never pool."""
        return f"{strategy_id}-hyp-{self.id}", self.version


def _param(params: dict, name: str, default):
    return params.get(name, default)


def _volume_thrust(snapshot: dict, direction: str, cfg: dict,
                   params: dict):
    """Permissive by design: participation, and movement in the direction."""
    relative_volume = finite(snapshot.get("relative_volume_1h"))
    momentum = finite(snapshot.get("mom_1h_pct"))
    if relative_volume is None or momentum is None:
        return False, "volume or momentum unavailable"
    threshold = float(_param(params, "min_relative_volume", 1.5))
    if relative_volume < threshold:
        return False, (f"relative volume {relative_volume:.2f} below "
                       f"{threshold:g}")
    if direction == "long" and momentum <= 0:
        return False, "momentum is not positive"
    if direction == "short" and momentum >= 0:
        return False, "momentum is not negative"
    return True, None


def _oi_divergence(snapshot: dict, direction: str, cfg: dict,
                   params: dict):
    """Price up on falling open interest is short covering, not initiation."""
    change = finite(snapshot.get("oi_change_4h_pct"))
    momentum = finite(snapshot.get("mom_1h_pct"))
    if change is None or momentum is None:
        return False, "open-interest change unavailable"
    threshold = float(_param(params, "max_oi_change_4h_pct", -1.0))
    if direction == "long" and not (momentum < 0 and change < threshold):
        return False, "not a falling-OI decline"
    if direction == "short" and not (momentum > 0 and change < threshold):
        return False, "not a falling-OI advance"
    return True, None


def _basis_stretch(snapshot: dict, direction: str, cfg: dict,
                   params: dict):
    basis = finite(snapshot.get("perp_index_basis_pct"))
    percentile = finite(snapshot.get("funding_percentile_30"))
    samples = finite(snapshot.get("funding_samples_30"), 0.0) or 0.0
    if basis is None or percentile is None:
        return False, "basis or funding percentile unavailable"
    basis_threshold = float(_param(params, "basis_threshold_pct", 0.05))
    high_percentile = float(_param(params, "high_funding_percentile", 80.0))
    low_percentile = float(_param(params, "low_funding_percentile", 20.0))
    min_samples = float(_param(params, "min_funding_samples", 10))
    if samples < min_samples:
        return False, (f"only {samples:.0f} funding samples, "
                       f"{min_samples:g} required")
    if (direction == "short"
            and not (basis > basis_threshold
                     and percentile >= high_percentile)):
        return False, "no long-side crowding stretch"
    if (direction == "long"
            and not (basis < -basis_threshold
                     and percentile <= low_percentile)):
        return False, "no short-side crowding stretch"
    return True, None


REGISTRY: dict = {
    h.id: h for h in (
        Hypothesis(
            id="volume-thrust",
            version="v1",
            claim="A 1h impulse on materially elevated participation "
                  "continues more often than one on ordinary volume.",
            mechanism="Elevated participation means the move is being paid "
                      "for by real size rather than by a thin book drifting; "
                      "the counterparty is whoever is still positioned "
                      "against a move that has already been funded.",
            falsifier="Expectancy conditional on relative_volume_1h meeting "
                      "the registered min_relative_volume does not exceed "
                      "the unconditional expectancy by more than the MDE at "
                      "n >= 100.",
            contract=_volume_thrust,
            prompt_line="volume-thrust: an impulse on relative_volume_1h at "
                        "or above its registered contract threshold, in the "
                        "direction of that impulse.",
            contract_params={"min_relative_volume": 1.5},
            settings=(
                {"id": "registered", "params": {},
                 "claim": "The registered participation threshold."},
                {"id": "lower_participation", "params": {
                    "min_relative_volume": 1.2},
                 "claim": "A lower participation threshold captures more "
                          "impulses without changing the mechanism."},
                {"id": "higher_participation", "params": {
                    "min_relative_volume": 2.0},
                 "claim": "A higher participation threshold isolates only "
                          "the strongest impulses."},
            ),
        ),
        Hypothesis(
            id="oi-divergence",
            version="v1",
            claim="A move against falling open interest is position "
                  "closing rather than initiation, and it exhausts.",
            mechanism="Open interest falling while price moves means "
                      "existing positions are being closed, often forcibly. "
                      "That flow is terminal: it stops when the trapped side "
                      "is done, and there is no new money behind it.",
            falsifier="Expectancy conditional on oi_change_4h_pct below the "
                      "registered max_oi_change_4h_pct does not differ from "
                      "the unconditional case by more than the MDE at n >= "
                      "100.",
            contract=_oi_divergence,
            prompt_line="oi-divergence: a move against the registered 4h "
                        "open-interest decline threshold, faded.",
            contract_params={"max_oi_change_4h_pct": -1.0},
            settings=(
                {"id": "registered", "params": {},
                 "claim": "The registered open-interest decline threshold."},
                {"id": "milder_decline", "params": {
                    "max_oi_change_4h_pct": -0.5},
                 "claim": "A milder open-interest decline captures more "
                          "position-closing events."},
                {"id": "deeper_decline", "params": {
                    "max_oi_change_4h_pct": -2.0},
                 "claim": "A deeper open-interest decline isolates more "
                          "forced-deleveraging events."},
            ),
        ),
        Hypothesis(
            id="basis-stretch",
            version="v1",
            claim="An extreme perp-index basis together with an extreme "
                  "funding percentile marks a crowded side worth fading.",
            mechanism="Basis and funding are the price of carrying a side. "
                      "Both extreme at once means the crowd is paying a lot "
                      "to hold a position it entered for reasons unrelated "
                      "to expected value.",
            falsifier="Expectancy in the crowded-side cell does not exceed "
                      "the unconditional case by more than the MDE at "
                      "n >= 100 per cell.",
            contract=_basis_stretch,
            prompt_line="basis-stretch: basis beyond the registered threshold "
                        "with funding in its registered percentile tail, "
                        "faded.",
            contract_params={
                "basis_threshold_pct": 0.05,
                "high_funding_percentile": 80.0,
                "low_funding_percentile": 20.0,
                "min_funding_samples": 10,
            },
            settings=(
                {"id": "registered", "params": {},
                 "claim": "The registered basis and funding tail thresholds."},
                {"id": "narrower_stretch", "params": {
                    "basis_threshold_pct": 0.03},
                 "claim": "A narrower basis stretch tests a more frequent "
                          "crowding signal."},
                {"id": "wider_stretch", "params": {
                    "basis_threshold_pct": 0.10},
                 "claim": "A wider basis stretch tests only extreme "
                          "crowding conditions."},
            ),
        ),
    )
}


def spec_for(hypothesis_id: str) -> Hypothesis | None:
    return REGISTRY.get(str(hypothesis_id or ""))


def evaluate(hypothesis_id: str, snapshot: dict, direction: str,
             cfg: dict, contract_params: dict | None = None
             ) -> tuple[bool, str | None]:
    """Run one registered hypothesis's contract. Unknown ids never fire."""
    spec = spec_for(hypothesis_id)
    if spec is None:
        return False, (
            f"hypothesis_id {hypothesis_id!r} is not registered; "
            f"registered: {', '.join(sorted(REGISTRY)) or 'none'}")
    params = dict(spec.contract_params)
    params.update(contract_params or {})
    return spec.contract(snapshot, direction, cfg, params)


def settings_for(hypothesis_id: str) -> tuple[dict, ...]:
    """Return the pre-registered settings for one hypothesis."""
    spec = spec_for(hypothesis_id)
    return spec.settings if spec is not None else ()


def prompt_fragment() -> str:
    """The versioned list injected into the prompt.

    Explicit rather than open-ended. The model may only choose from what is
    written here, so every experimental trade is attributable to a named
    claim that was registered before it was taken.
    """
    lines = [f"- {spec.prompt_line}"
             for _, spec in sorted(REGISTRY.items())]
    return "\n".join(lines)
