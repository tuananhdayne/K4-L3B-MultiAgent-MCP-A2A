import pytest

from student_agent.submission import validate_case_provenance


def test_rejects_output_reference_without_matching_consumed_trace():
    ref = "ev_" + "a" * 20
    outputs = {"L3B_CASE_001": {"evidence_refs": [ref], "claim_assessments": []}}
    events = [
        {"case_id": "L3B_CASE_001", "event_type": "case_received", "actor": "coordinator"},
        {"case_id": "L3B_CASE_001", "event_type": "task_assigned", "actor": "coordinator"},
        {
            "case_id": "L3B_CASE_001",
            "event_type": "tool_result_consumed",
            "actor": "order-agent",
            "evidence_refs": [],
        },
        {"case_id": "L3B_CASE_001", "event_type": "handoff", "actor": "order-agent"},
        {"case_id": "L3B_CASE_001", "event_type": "policy_decided", "actor": "policy-agent"},
        {
            "case_id": "L3B_CASE_001",
            "event_type": "verification_completed",
            "actor": "verifier-agent",
        },
        {"case_id": "L3B_CASE_001", "event_type": "case_finalized", "actor": "coordinator"},
    ]
    with pytest.raises(ValueError, match="not consumed"):
        validate_case_provenance(outputs, events)


def test_rejects_missing_required_lifecycle_event():
    ref = "ev_" + "a" * 20
    outputs = {"L3B_CASE_001": {"evidence_refs": [ref], "claim_assessments": []}}
    events = [
        {"case_id": "L3B_CASE_001", "event_type": "case_received", "actor": "coordinator"},
        {
            "case_id": "L3B_CASE_001",
            "event_type": "tool_result_consumed",
            "actor": "order-agent",
            "evidence_refs": [ref],
        },
        {"case_id": "L3B_CASE_001", "event_type": "case_finalized", "actor": "coordinator"},
    ]
    with pytest.raises(ValueError, match="missing lifecycle"):
        validate_case_provenance(outputs, events)
