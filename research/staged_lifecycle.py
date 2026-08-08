"""Small policy surface for bounded staged-configuration lifecycles."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LifecyclePolicy:
    """Time/observation limits that prevent inactive work owning a lane forever."""

    never_fired_decisions: int = 2_000
    never_fired_max_age_seconds: float = 7 * 24 * 60 * 60
    no_observation_max_age_seconds: float = 3 * 24 * 60 * 60
    starved_max_age_seconds: float = 3 * 24 * 60 * 60
    starved_fraction: float = 0.5

    def __post_init__(self):
        if self.never_fired_decisions < 1:
            raise ValueError("never_fired_decisions must be positive")
        for name in (
                "never_fired_max_age_seconds", "no_observation_max_age_seconds",
                "starved_max_age_seconds"):
            if float(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        if not 0.0 < float(self.starved_fraction) <= 1.0:
            raise ValueError("starved_fraction must be in (0, 1]")


DEFAULT_POLICY = LifecyclePolicy()


def aggregate_mechanism_evidence(entries: list[dict]) -> dict:
    """Reduce configuration verdicts without treating one loser as the idea."""
    if not entries:
        return {
            "code": "COLLECTING", "configuration_count": 0,
            "mechanism_falsified": False,
            "supported_configuration_ids": [],
            "failed_configuration_ids": [],
            "collecting_configuration_ids": [],
        }
    by_configuration: dict[str, list[dict]] = {}
    for entry in entries:
        by_configuration.setdefault(
            str(entry.get("configuration_id") or entry["contract_id"]), []).append(entry)

    supported, failed, collecting, starved = [], [], [], []
    retiring = {
        "NEGATIVE_EXPECTANCY", "DIED_OUT_OF_SAMPLE", "NEVER_FIRED",
        "STARVED_EXPIRED", "INACTIVE_EXPIRED",
    }
    for configuration_id, rows in sorted(by_configuration.items()):
        codes = {str(row.get("code")) for row in rows}
        # Support is global only when every observed scope agrees.  A positive
        # account must not hide a negative or still-collecting population.
        if codes == {"SUPPORTED"}:
            supported.append(configuration_id)
        elif codes and codes <= retiring:
            failed.append(configuration_id)
        elif codes & {"STARVED_OF_DATA"}:
            starved.append(configuration_id)
        else:
            collecting.append(configuration_id)

    if supported:
        code = "SUPPORTED"
    elif collecting or starved:
        code = "COLLECTING"
    else:
        # This is a configuration-family result, not a falsification of the
        # causal mechanism.  A bounded refinement policy decides whether one
        # more preregistered configuration may be attempted.
        code = "CONFIGURATIONS_FAILED"
    return {
        "code": code,
        "mechanism_falsified": False,
        "configuration_count": len(by_configuration),
        "supported_configuration_ids": supported,
        "failed_configuration_ids": failed,
        "collecting_configuration_ids": collecting,
        "starved_configuration_ids": starved,
    }
