import asyncio
import json
from pathlib import Path

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import (
    calibrate_confidence,
    payment_analysis,
    scope_snapshot,
    solve_case,
)


class Gateway:
    def __init__(self):
        self.calls = []

    async def call(self, name, *, case_id, **kwargs):
        self.calls.append((name, case_id, kwargs))
        data = {
            "get_order": {
                "order_id": "order-1",
                "order_status": "delivered",
                "customer_id": "customer-row",
            },
            "get_customer_history": {
                "customer_unique_id": "customer-1",
                "orders": [{"order_id": "order-1", "customer_id": "customer-row"}],
            },
            "get_order_items": [
                {
                    "order_item_id": "item-1",
                    "seller_id": "seller-1",
                    "product_id": "product-1",
                    "price": "79",
                    "freight_value": "10",
                }
            ],
            "get_shipment_summary": {
                "order_id": "order-1",
                "order_status": "delivered",
                "delivered_carrier_at": "2018-01-02T00:00:00Z",
                "delivered_customer_at": "2018-01-06T00:00:00Z",
                "estimated_delivery_at": "2018-01-05T00:00:00Z",
                "shipping_limits": [],
                "events": [
                    {
                        "event_type": "delivered_late",
                        "actor": "logistics_provider",
                        "status": "confirmed",
                    }
                ],
            },
            "get_payment_timeline": {
                "payments": [{"payment_value": "89"}],
                "events": [{"event_type": "captured", "amount_brl": "89", "status": "confirmed"}],
            },
            "get_product_context": [],
            "get_policy": {
                "currency": "BRL",
                "rules": {
                    "late_delivery_logistics": {
                        "case_status": "action_required",
                        "recommended_action": "refund_freight",
                        "refund_brl": 10,
                        "responsible_parties": [
                            {"party_type": "logistics_provider", "party_id": None}
                        ],
                    }
                },
            },
        }
        if name == "get_refund_timeline":
            raise RuntimeError("no refund record")
        assert name in data
        number = len(self.calls)
        return {"evidence_ref": "ev_" + str(number).zfill(20), "data": data[name], "domain": name}


def test_solve_case_uses_real_refs_and_builds_valid_l3b_output(tmp_path):
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    case = {
        "case_id": "L3B_CASE_001",
        "policy_version": "EC_POLICY_V2",
        "customer_unique_id_hint": "customer-1",
        "candidate_order_ids": ["order-1", "candidate-1"],
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [{"claim_id": "claim-a", "topic": "late_delivery_logistics"}],
        },
        "investigation_scope": {"include_customer_history": True, "include_product_context": True},
    }
    gateway = Gateway()
    output = asyncio.run(solve_case(case, gateway, trace))
    contracts.validate_output(output, "test output")
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["financial_resolution"]["recommended_refund_brl"] == 10
    assert output["entity_resolution"]["resolved_order_ids"] == ["order-1"]
    assert output["entity_resolution"]["rejected_candidates"] == ["candidate-1"]
    issued = {"ev_" + str(index).zfill(20) for index in range(1, len(gateway.calls) + 1)}
    assert set(output["evidence_refs"]) <= issued
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    consumed = {
        ref
        for event in events
        if event["event_type"] == "tool_result_consumed"
        for ref in event["evidence_refs"]
    }
    assert set(output["evidence_refs"]) <= consumed
    assert {"task_assigned", "handoff", "policy_decided", "verification_completed"} <= {
        event["event_type"] for event in events
    }


def test_duplicate_payment_sequence_without_duplicate_event_is_not_duplicate_charge():
    timeline = {
        "payments": [
            {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "89"},
            {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "16"},
        ],
        "events": [
            {"event_type": "captured", "amount_brl": "89", "status": "confirmed"},
            {"event_type": "captured", "amount_brl": "16", "status": "confirmed"},
        ],
    }
    verdict, *_ = payment_analysis(timeline, {}, [{"price": "95", "freight_value": "10"}])
    assert verdict == "reconciled"


def test_open_reconciliation_mismatch_controls_payment_verdict():
    timeline = {
        "payments": [{"payment_value": "89"}],
        "events": [
            {"event_type": "captured", "amount_brl": "89", "status": "confirmed"},
            {"event_type": "reconciliation_mismatch", "amount_brl": "35", "status": "open"},
        ],
    }
    verdict, *_ = payment_analysis(timeline, {}, [{"price": "79", "freight_value": "10"}])
    assert verdict == "capture_mismatch"


def test_split_payment_uses_independent_payment_types_despite_stale_rows():
    timeline = {
        "payments": [
            {"payment_type": "credit_card", "payment_value": "52"},
            {"payment_type": "credit_card", "payment_value": "44.50"},
            {"payment_type": "voucher", "payment_value": "44.50"},
        ],
        "events": [{"event_type": "captured", "amount_brl": "141", "status": "confirmed"}],
    }
    verdict, *_ = payment_analysis(timeline, {}, [{"price": "79", "freight_value": "10"}])
    assert verdict == "reconciled"


def test_two_equal_captures_above_item_total_are_duplicate_charge():
    timeline = {
        "payments": [
            {"payment_type": "credit_card", "payment_value": "64"},
            {"payment_type": "voucher", "payment_value": "64"},
        ],
        "events": [
            {"event_type": "captured", "amount_brl": "64", "status": "confirmed"},
            {"event_type": "captured", "amount_brl": "64", "status": "confirmed"},
        ],
    }
    verdict, *_ = payment_analysis(timeline, {}, [{"price": "79", "freight_value": "10"}])
    assert verdict == "duplicate_capture"


def test_snapshot_uses_order_active_when_case_opened():
    case = {"opened_at": "2018-02-02T09:00:00-03:00"}
    history = {
        "orders": [
            {
                "order_id": "order-1",
                "order_status": "delivered",
                "order_purchase_timestamp": "2018-04-10T09:00:00-03:00",
            },
            {
                "order_id": "order-1",
                "order_status": "processing",
                "order_purchase_timestamp": "2018-01-21T09:00:00-03:00",
            },
        ]
    }
    order = history["orders"][0]
    items = [
        {
            "order_item_id": "item-1",
            "price": "79",
            "freight_value": "10",
            "shipping_limit_date": "2018-04-13T09:00:00-03:00",
        },
        {
            "order_item_id": "item-1",
            "price": "79",
            "freight_value": "10",
            "shipping_limit_date": "2018-01-24T09:00:00-03:00",
        },
    ]
    shipment = {"events": [], "shipping_limits": []}
    payment = {
        "payments": [
            {"payment_type": "credit_card", "payment_value": "52"},
            {"payment_type": "credit_card", "payment_value": "44.50"},
            {"payment_type": "voucher", "payment_value": "44.50"},
        ],
        "events": [
            {
                "event_type": "captured",
                "amount_brl": "52",
                "event_at": "2018-04-10T10:00:00-03:00",
                "status": "confirmed",
            },
            {
                "event_type": "captured",
                "amount_brl": "44.50",
                "event_at": "2018-01-21T10:00:00-03:00",
                "status": "confirmed",
            },
            {
                "event_type": "captured",
                "amount_brl": "44.50",
                "event_at": "2018-01-21T11:00:00-03:00",
                "status": "confirmed",
            },
        ],
    }
    chosen, scoped_items, _, scoped_payment, _ = scope_snapshot(
        case, history, order, items, shipment, payment, {}
    )
    assert chosen["order_status"] == "processing"
    assert len(scoped_items) == 1
    assert scoped_items[0]["shipping_limit_date"].startswith("2018-01")
    assert len(scoped_payment["events"]) == 2
    assert {row["payment_type"] for row in scoped_payment["payments"]} == {"credit_card", "voucher"}


def test_resolved_snapshot_conflict_does_not_reduce_confidence():
    conflict = {
        "field": "delivered_customer_at",
        "sources": ["order", "shipment"],
        "selected_source": "customer_history",
        "resolution_code": "case_time_window",
    }
    assert calibrate_confidence("late_delivery_logistics", True, True, True, [conflict]) == 0.85


def test_unresolved_conflict_reduces_confidence():
    conflict = {
        "field": "customer_order_link",
        "sources": ["customer", "order"],
        "selected_source": None,
        "resolution_code": "unresolved",
    }
    assert calibrate_confidence("late_delivery_logistics", True, True, True, [conflict]) == 0.65
