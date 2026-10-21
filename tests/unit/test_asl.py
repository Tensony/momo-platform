"""Static checks on the Step Functions definition: it cannot silently lose a compensation route."""
import json
import pathlib

ASL = json.loads(
    (pathlib.Path(__file__).resolve().parents[2] / "statemachine" / "transfer_saga.asl.json").read_text()
    .replace("${steps_function_arn}", "arn:aws:lambda:x:1:function:steps")
    .replace("${fraud_function_arn}", "arn:aws:lambda:x:1:function:fraud")
)
STATES = ASL["States"]


def targets(state):
    out = []
    if "Next" in state:
        out.append(state["Next"])
    out += [c["Next"] for c in state.get("Catch", [])]
    out += [c["Next"] for c in state.get("Choices", [])]
    if "Default" in state:
        out.append(state["Default"])
    return out


def test_all_transitions_point_at_real_states_and_all_states_reachable():
    assert ASL["StartAt"] in STATES
    seen, todo = set(), [ASL["StartAt"]]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        for t in targets(STATES[name]):
            assert t in STATES, f"{name} -> missing {t}"
            todo.append(t)
    assert seen == set(STATES), f"unreachable: {set(STATES) - seen}"


def test_every_forward_task_routes_errors_into_compensation():
    forward = ["Reserve", "FraudCheck", "ReleaseForFraud", "Credit", "ReleaseForCreditRejection", "Finalize"]
    for name in forward:
        catches = STATES[name]["Catch"]
        assert catches[0]["ErrorEquals"] == ["States.ALL"] and catches[0]["Next"] == "ReverseCredit", name


def test_compensation_order_and_escalation():
    assert STATES["ReverseCredit"]["Next"] == "ReleaseOnError"      # reverse credit BEFORE release
    assert STATES["ReleaseOnError"]["Next"] == "MarkFailed"
    for name in ("ReverseCredit", "ReleaseOnError"):
        assert STATES[name]["Catch"][0]["Next"] == "MarkCompensationFailed"
        assert any(r["MaxAttempts"] >= 5 for r in STATES[name]["Retry"])
    assert STATES["CompensationFailed"]["Type"] == "Fail"            # surfaces as ExecutionsFailed alarm


def test_fraud_decline_releases_before_rejecting():
    assert STATES["FraudDecision"]["Choices"][0]["Next"] == "ReleaseForFraud"
    assert STATES["ReleaseForFraud"]["Next"] == "MarkRejectedFraud"


def test_terminal_states_emit_outcome():
    for name in ("RejectedReserve", "RejectedFraud", "RejectedCredit", "Completed", "Failed"):
        assert "outcome" in STATES[name]["Parameters"]
