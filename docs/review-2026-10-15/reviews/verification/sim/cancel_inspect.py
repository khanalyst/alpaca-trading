"""After an audited operator cancellation, does read-only inspection still read the state?"""
import sys, tempfile, json
from pathlib import Path
sys.path.insert(0, "/home/user/alpaca-trading")
sys.path.append("/home/user/alpaca-trading/tests")
from agent import state
from agent.paper_trial_operator import cancel_paper_trial
from agent.runtime_inspection import inspect_runtime
import test_paper_trial_operator as t

tmp = Path(tempfile.mkdtemp(prefix="c-review-"))
state.RUNTIME_BASE = tmp / "runtime"
state.configure_runtime("paper")
state.ensure_ready()
fp = state.account_fingerprint("paper", "paper-key\0paper-account")
case = t.PaperTrialOperatorTests("test_cancel_is_audited_and_preserves_runtime_and_old_verdict")
case.root = tmp
case.fingerprint = fp
# Reuse the fixture trial body from the repository test (setUp minus cleanup registration).
case.setUp = None
trial = {
    "schema": "paper-incumbent-trial.v1", "state": "running", "trial_id": "trial-old",
    "candidate_id": "candidate-old", "variant_id": "variant-old", "family": "rule", "role": "control",
    "incumbent_identity": "identity-old", "policy_identity": "policy-old", "spec_identity": "spec-old",
    "code_identity": "code-old", "cohort_identity": "cohort-old", "include_ibr": False,
    "report_identities": {"deployment": None, "code": "code-old", "cohort": "cohort-old", "activation": None},
    "activation_confirmed": True, "activation_account_fingerprint": fp, "started_on": "2026-09-01",
    "accepted_session_report_root": str(tmp / "reports"), "max_review_sessions": 60,
    "required_sessions": 20, "required_trades": 20, "accepted_sessions": [], "outcomes": [],
    "verdict": {"state": "running", "sessions": 0, "trades": 0, "sessions_required": 20, "trades_required": 20},
    "blockers": [], "authorizing": False, "proof_authority": False,
}
state.save_state({**state.load_state(), "state": state.PAUSED, "runtime_mode": "paper",
                  "account_fingerprint": fp, "operator_pause": True, "active_trades": {},
                  "protection": {}, "orders": {}, "paper_trial": trial})
result = cancel_paper_trial(t._config(), confirm_trial_id="trial-old",
                            confirm_incumbent_identity="identity-old", reason="review",
                            provider_factory=lambda _c: t._Provider())
print("cancel:", result["status"])
print("same-process load_state ok:", state.load_state()["paper_trial"]["state"])
# A fresh CLI process (main.py check/status/inspect-deployment) never calls configure_runtime.
state.configure_runtime("_unconfigured")
print("state.JOURNAL_FILE now:", state.JOURNAL_FILE)
cfg = {"mode": "paper", "broker": {"paper": True, "allow_live": False}}
local = inspect_runtime(cfg, authenticated=False)
print("runtime_state_ready:", local["runtime_state_ready"])
print("readiness_blockers:", local["readiness_blockers"])
print("state_error:", local.get("state_error"))
