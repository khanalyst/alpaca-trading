"""Turn staged mechanisms into evaluable proposals on one shared harness.

A staged contract states when to enter and nothing else. Giving each proposal
its own exits would mean two authored mechanisms differ in both the entry rule
and the exit policy at once, so a winner could as easily be a lucky stop
distance as a real edge. Every staged mechanism is therefore measured on
``forward_models.STAGED_HARNESS``: same entry, stop, target, costs, horizon.

The emitted proposals are the same shape the registered contracts emit, so a
staged mechanism flows through the existing paper-trading path rather than a
parallel one. A mechanism that does not fire still emits a proposal carrying
its refusal reason, because a contract that declined is evidence and a
contract that was never evaluated is not - that distinction is what made the
depth-ladder starvation visible at all.
"""

from __future__ import annotations

from .contract_dsl import ProposedContract, compile_contract
from .forward_models import STAGED_HARNESS, _field, _finite

# Staged mechanisms share one setup family. The contract id travels on the
# proposal instead, so results stay attributable per mechanism while the
# outcome contract stays identical across all of them.
STAGED_SETUP_TYPE = "staged_mechanism"


def candidate_variant_id(contract_id: str) -> str:
    """Stable staged candidate identity shared by proposal and paper lanes."""
    import re

    slug = re.sub(r"[^a-z0-9]+", "_", str(contract_id).lower()).strip("_")
    return f"staged.{slug}"


def baseline_variant_id(contract_id: str) -> str:
    """Stable neutral baseline identity paired with one staged contract."""
    return f"{candidate_variant_id(contract_id)}.baseline"


def proposals_for(contracts: list[ProposedContract], snapshot: dict,
                  cfg: dict, *, baseline: bool = False) -> list[dict]:
    """Emit candidate or paired-baseline proposals per symbol/direction.

    Both directions are emitted for a two-sided contract even when only one
    can fire, matching what the registered contracts do: a baseline and a
    candidate must see the same proposal identity so the comparison stays
    paired when one side vetoes. ``baseline=True`` makes the same opportunity
    unconditional while retaining all market-data starvation checks.
    """
    del cfg  # thresholds live on the contract, not in configuration
    model = STAGED_HARNESS
    out: list[dict] = []
    compiled = {contract.contract_id: compile_contract(contract)
                for contract in contracts}
    for symbol in sorted(snapshot or {}):
        if symbol.startswith("_") or not isinstance(snapshot[symbol], dict):
            continue
        row = snapshot[symbol]
        missing = model.missing_fields(row)
        observation_error = str(_field(
            row, "_enrichment.book_observation_error") or "").strip()
        signal_ts = _finite(_field(row, model.signal_timestamp_field))
        for contract in contracts:
            directions = (("long", "short") if contract.direction == "both"
                          else (contract.direction,))
            for direction in directions:
                proposal = {
                    "action": "open",
                    "symbol": symbol,
                    "direction": direction,
                    "setup_type": STAGED_SETUP_TYPE,
                    "confidence": 1.0,
                    "invalidation_anchor": model.invalidation_anchor,
                    "exit_policy": model.exit_policy,
                    "carry_exit_funding_percentile": None,
                    "execution_choice": "normal",
                    "proposal_source": "staged_contract",
                    "strategy_id": "staged",
                    "contract_id": contract.contract_id,
                    "model_id": model.model_id,
                    "signal_ts": signal_ts,
                }
                candidate_id = candidate_variant_id(contract.contract_id)
                target_variant_id = (baseline_variant_id(contract.contract_id)
                                     if baseline else candidate_id)
                proposal["target_variant_id"] = target_variant_id
                # Data problems outrank the contract's own opinion: a
                # mechanism that "declined" on a snapshot it could not read
                # has not been tested, and recording it as a decline would
                # count starvation as evidence against the claim.
                if observation_error:
                    proposal["research_refusal_reason"] = (
                        "market data invalid: " + observation_error)
                elif missing:
                    proposal["research_refusal_reason"] = (
                        "data missing: " + ", ".join(missing))
                elif baseline:
                    # The neutral arm is deliberately unconditional on the
                    # same eligible opportunity. It receives the same
                    # proposal identity and fixed exit policy as the
                    # candidate, but never evaluates the candidate's signal.
                    pass
                else:
                    fired, reason = compiled[contract.contract_id](
                        row, direction, {}, {})
                    if not fired:
                        proposal["research_refusal_reason"] = (
                            f"contract declined: {reason}")
                out.append(proposal)
    return out


def coverage(proposals: list[dict]) -> dict:
    """Split proposals into fired, declined and starved, per mechanism.

    Kept separate from the proposals themselves so a nightly report can tell
    "this mechanism never fires" from "this mechanism never got data", which
    are opposite conclusions about whether the claim was tested.
    """
    per: dict[str, dict] = {}
    for proposal in proposals:
        # The paired neutral account is persisted for inference, but lane
        # coverage is reported for the authored mechanism only.
        if str(proposal.get("target_variant_id") or "").endswith(".baseline"):
            continue
        contract_id = str(proposal.get("contract_id") or "unknown")
        bucket = per.setdefault(
            contract_id, {"fired": 0, "declined": 0, "starved": 0})
        reason = str(proposal.get("research_refusal_reason") or "")
        if not reason:
            bucket["fired"] += 1
        elif reason.startswith("contract declined"):
            bucket["declined"] += 1
        else:
            bucket["starved"] += 1
    return per
