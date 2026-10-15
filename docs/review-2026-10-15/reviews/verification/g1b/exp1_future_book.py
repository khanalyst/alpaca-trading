"""G1b exp1: a later session's open virtual book changes the decisions that
ShadowRunner re-evaluates for an already matched historical gate session, and
those re-evaluated decisions (not the persisted ones) are what _replay gets."""
import sys
sys.path.insert(0, "/home/user/alpaca-trading")
from unittest.mock import patch

from research.live_shadow import _digest
from tests.research import test_shadow_replay_memo as memo_tests

case = memo_tests.ShadowReplayMemoRegressionTests(methodName="tearDown")
case.setUp()
try:
    runner = case._runner()
    candidate = case._candidate()
    cid = candidate["candidate_id"]
    session = "2026-01-02"
    closing = case._seed_replay_only(runner, candidate, session)
    # Persisted live decision for the historical SPY bar (seeded by the helper).
    persisted = runner.store.decisions_for_session(cid, session)
    print("persisted decisions:", [(r["symbol"], r["kind"], r["reason"]) for r in persisted])

    def opening(cand, event, bars, quotes, options):
        payload = {"session_date": session, "strategy_id": cand.get("strategy_id")}
        plan = {"symbol": "SPY", "risk_usd": 10.0, "notional": 100.0,
                "entry_price": 100.0, "shares": 1}
        return ("open_incomplete", "virtual open", payload, plan)

    captured = []

    def fake_replay(cand, sess, bars, quotes, decisions, *a, **k):
        captured.append((sess, [(d.get("symbol"), d.get("kind"), d.get("reason"))
                                for d in decisions]))
        return True

    with patch.object(runner, "_evaluate", side_effect=opening), \
            patch.object(runner, "_replay", side_effect=fake_replay):
        runner.run_once()
    print("poll 1 decisions given to _replay:", captured)

    # A later session (2026-01-03) opens a SPY virtual book.
    future_key = "future-open-event"
    runner.store.decision(candidate_id=cid, event_key=future_key,
                          session_date="2026-01-03", symbol="SPY",
                          kind="open_incomplete", reason="future book",
                          payload={"session_date": "2026-01-03"}, max_decisions=100)
    runner.store.virtual_open(candidate_id=cid,
                              decision_id=_digest({"candidate_id": cid, "event_key": future_key}),
                              symbol="SPY",
                              plan={"symbol": "SPY", "quantity": 1.0, "entry_price": 100.0,
                                    "risk_usd": 10.0, "notional": 100.0})
    captured.clear()
    with patch.object(runner, "_evaluate", side_effect=opening) as ev, \
            patch.object(runner, "_replay", side_effect=fake_replay):
        runner.run_once()
    print("poll 2 _evaluate calls:", ev.call_count)
    print("poll 2 decisions given to _replay:", captured)
    print("persisted decisions after poll 2:",
          [(r["symbol"], r["kind"], r["reason"]) for r in runner.store.decisions_for_session(cid, session)])
finally:
    case.tearDown()
