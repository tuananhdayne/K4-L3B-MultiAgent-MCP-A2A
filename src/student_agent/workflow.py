"""Evidence-backed L3B investigation with per-case MCP state."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def date(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except (ValueError, AttributeError):
        return None


def ids(rows: Any, key: str) -> list[str]:
    return (
        list(
            dict.fromkeys(
                row[key]
                for row in rows
                if isinstance(row, dict) and isinstance(row.get(key), str) and row[key]
            )
        )[:20]
        if isinstance(rows, list)
        else []
    )


class Investigator:
    def __init__(self, case_id: str, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case_id = case_id
        self.gateway = gateway
        self.trace = trace
        self.cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any] | None] = {}
        self.refs: dict[str, str] = {}

    async def call(self, actor: str, name: str, **kwargs: str) -> dict[str, Any] | None:
        key = (name, tuple(sorted(kwargs.items())))
        if key in self.cache:
            return self.cache[key]
        try:
            evidence = await self.gateway.call(name, case_id=self.case_id, **kwargs)
        except (RuntimeError, ValueError, TimeoutError):
            self.cache[key] = None
            return None
        self.cache[key] = evidence
        ref = evidence["evidence_ref"]
        self.refs[name] = ref
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=name,
            evidence_refs=[ref],
        )
        return evidence


def data(evidence: dict[str, Any] | None, default: Any) -> Any:
    return evidence.get("data", default) if evidence else default


def scope_snapshot(
    case: dict[str, Any],
    history: dict[str, Any],
    order: dict[str, Any],
    items: list[dict[str, Any]],
    shipment: dict[str, Any],
    payment: dict[str, Any],
    refund: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Select one temporal order record when MCP joins contain reused IDs."""
    rows = [
        row
        for row in history.get("orders", [])
        if isinstance(row, dict)
        and row.get("order_id") == order.get("order_id")
        and date(row.get("order_purchase_timestamp"))
    ]
    rows.sort(key=lambda row: date(row["order_purchase_timestamp"]))
    opened = date(case.get("opened_at"))
    eligible = [row for row in rows if opened and date(row["order_purchase_timestamp"]) <= opened]
    chosen = eligible[-1] if eligible else rows[0] if rows else order
    start = date(chosen.get("order_purchase_timestamp"))
    later = [
        date(row["order_purchase_timestamp"])
        for row in rows
        if start and date(row["order_purchase_timestamp"]) > start
    ]
    end = min(later) if later else None

    def inside(value: Any) -> bool:
        timestamp = date(value)
        return bool(
            timestamp and (start is None or timestamp >= start) and (end is None or timestamp < end)
        )

    scoped_items: list[dict[str, Any]] = []
    by_id: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if isinstance(item, dict):
            by_id.setdefault(str(item.get("order_item_id", len(by_id))), []).append(item)
    for group in by_id.values():
        matching = [item for item in group if inside(item.get("shipping_limit_date"))]
        scoped_items.append(
            min(matching, key=lambda item: date(item["shipping_limit_date"]))
            if matching
            else group[0]
        )

    scoped_shipment = dict(shipment)
    if chosen is not order:
        scoped_shipment.update(
            order_status=chosen.get("order_status"),
            delivered_carrier_at=chosen.get("order_delivered_carrier_date"),
            delivered_customer_at=chosen.get("order_delivered_customer_date"),
            estimated_delivery_at=chosen.get("order_estimated_delivery_date"),
        )
    scoped_shipment["events"] = [
        e
        for e in shipment.get("events", [])
        if isinstance(e, dict) and (not start or inside(e.get("event_at")))
    ]
    scoped_shipment["shipping_limits"] = [
        e
        for e in shipment.get("shipping_limits", [])
        if isinstance(e, dict) and (not start or inside(e.get("shipping_limit_at")))
    ]

    scoped_payment = dict(payment)
    scoped_payment["events"] = [
        e
        for e in payment.get("events", [])
        if isinstance(e, dict) and (not start or inside(e.get("event_at")))
    ]
    capture_amounts = [
        money(e.get("amount_brl"))
        for e in scoped_payment["events"]
        if e.get("event_type") in {"captured", "capture_succeeded"}
    ]
    remaining = list(capture_amounts)
    matched: list[dict[str, Any]] = []
    for row in payment.get("payments", []):
        if isinstance(row, dict) and money(row.get("payment_value")) in remaining:
            matched.append(row)
            remaining.remove(money(row.get("payment_value")))
    scoped_payment["payments"] = matched if matched else payment.get("payments", [])
    scoped_refund = dict(refund)
    scoped_refund["events"] = [
        e
        for e in refund.get("events", [])
        if isinstance(e, dict) and (not start or inside(e.get("event_at")))
    ]
    return chosen, scoped_items, scoped_shipment, scoped_payment, scoped_refund


def shipment_analysis(shipment: dict[str, Any]) -> tuple[str, bool, list[str]]:
    events = [
        e
        for e in shipment.get("events", [])
        if isinstance(e, dict) and e.get("status") == "confirmed"
    ]
    actor = next(
        (
            e.get("actor")
            for e in events
            if e.get("event_type") in {"delivered_late", "late_delivery"}
        ),
        None,
    )
    kinds = {e.get("event_type") for e in events}
    carrier = date(shipment.get("delivered_carrier_at"))
    delivered = date(shipment.get("delivered_customer_at"))
    estimated = date(shipment.get("estimated_delivery_at"))
    late_sellers = list(
        dict.fromkeys(
            row["seller_id"]
            for row in shipment.get("shipping_limits", [])
            if isinstance(row, dict)
            and row.get("seller_id")
            and carrier
            and date(row.get("shipping_limit_at"))
            and carrier > date(row["shipping_limit_at"])
        )
    )[:20]
    if shipment.get("order_status") == "returned" or "returned" in kinds:
        verdict = "returned"
    elif "lost" in kinds:
        verdict = "lost"
    elif actor == "seller" or (late_sellers and actor != "logistics_provider"):
        verdict = "seller_delay"
    elif actor == "logistics_provider" or (delivered and estimated and delivered > estimated):
        verdict = "logistics_delay"
    elif delivered and estimated:
        verdict = "on_time"
    else:
        verdict = "insufficient_evidence"
    return verdict, bool(carrier and delivered and estimated), late_sellers


def payment_analysis(
    timeline: dict[str, Any], refund: dict[str, Any], items: list[dict[str, Any]]
) -> tuple[str, Decimal, Decimal, Decimal]:
    events = [e for e in timeline.get("events", []) if isinstance(e, dict)]
    payments = [p for p in timeline.get("payments", []) if isinstance(p, dict)]
    captures = [
        e
        for e in events
        if e.get("event_type") in {"captured", "capture_succeeded"}
        and e.get("status") in {None, "confirmed", "succeeded"}
    ]
    captured = (
        sum((money(e.get("amount_brl")) for e in captures), Decimal(0))
        if captures
        else sum((money(p.get("payment_value")) for p in payments), Decimal(0))
    )
    refund_events = [e for e in refund.get("events", []) if isinstance(e, dict)]
    refunded = sum(
        (
            money(e.get("amount_brl"))
            for e in refund_events
            if e.get("event_type") in {"refunded", "refund_succeeded"}
            or e.get("status") in {"completed", "succeeded"}
        ),
        Decimal(0),
    )
    kinds = {e.get("event_type") for e in [*events, *refund_events]}
    expected = sum(
        (money(i.get("price")) + money(i.get("freight_value")) for i in items), Decimal(0)
    )
    if "refund_failed" in kinds or any(e.get("status") == "failed" for e in refund_events):
        verdict = "refund_failed"
    elif "refund_pending" in kinds or any(e.get("status") == "pending" for e in refund_events):
        verdict = "refund_pending"
    elif refunded > 0:
        verdict = "refunded"
    elif "duplicate_capture" in kinds or (
        len(captures) > 1
        and len({money(e.get("amount_brl")) for e in captures}) == 1
        and expected > 0
        and captured > expected
    ):
        verdict = "duplicate_capture"
    elif "reconciliation_mismatch" in kinds:
        verdict = "capture_mismatch"
    elif len({p.get("payment_type") for p in payments}) > 1 and captured > 0:
        verdict = "reconciled"
    else:
        verdict = (
            "capture_mismatch"
            if expected and captured and abs(expected - captured) > Decimal("0.01")
            else "reconciled"
            if captured
            else "insufficient_evidence"
        )
    return verdict, captured, refunded, max(Decimal(0), captured - refunded)


def choose_issue(
    order: dict[str, Any], shipment: str, payment: str, topics: list[str], rules: dict[str, Any]
) -> str:
    status = order.get("order_status")
    if status == "canceled" and payment != "insufficient_evidence":
        return "canceled_order_paid"
    if status in {"unavailable", "unavailable_order"} and payment != "insufficient_evidence":
        return "unavailable_order_paid"
    observed = {
        "refund_failed": "refund_failed",
        "refund_pending": "refund_pending",
        "duplicate_capture": "duplicate_charge",
        "capture_mismatch": "payment_mismatch",
        "seller_delay": "late_delivery_seller",
        "logistics_delay": "late_delivery_logistics",
    }
    for value, issue in observed.items():
        if value in {shipment, payment} and issue in topics and issue in rules:
            return issue
    if (
        payment == "reconciled"
        and "valid_split_payment" in topics
        and "valid_split_payment" in rules
    ):
        return "valid_split_payment"
    if "unsupported_claim" in topics and "unsupported_claim" in rules:
        return "unsupported_claim"
    return "insufficient_evidence"


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    inv = Investigator(case_id, gateway, trace)
    request = case.get("customer_request") or {}
    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator", target="entity-agent"
    )
    candidates = list(
        dict.fromkeys([request.get("claimed_order_id"), *(case.get("candidate_order_ids") or [])])
    )
    candidates = [value for value in candidates if isinstance(value, str) and value]
    hint = case.get("customer_unique_id_hint")
    history_ev = (
        await inv.call("entity-agent", "get_customer_history", customer_unique_id=hint)
        if hint
        else None
    )
    history = data(history_ev, {})
    history_orders = ids(history.get("orders", []), "order_id") if isinstance(history, dict) else []
    selected = next(
        (value for value in candidates if value in history_orders),
        candidates[0] if candidates else None,
    )
    order_ev = await inv.call("entity-agent", "get_order", order_id=selected) if selected else None
    if not order_ev:
        for candidate in candidates:
            if candidate != selected:
                order_ev = await inv.call("entity-agent", "get_order", order_id=candidate)
                if order_ev:
                    selected = candidate
                    break
    order = data(order_ev, {})
    if not isinstance(order, dict) or order.get("order_id") != selected:
        selected, order = None, {}
    rejected = [value for value in candidates if value != selected][:20]
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity-agent",
        target="coordinator",
        decision_code="entity_resolved" if selected else "entity_not_found",
    )
    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator", target="specialist-agents"
    )
    items_ev = (
        await inv.call("order-agent", "get_order_items", order_id=selected) if selected else None
    )
    shipment_ev = (
        await inv.call("shipment-agent", "get_shipment_summary", order_id=selected)
        if selected
        else None
    )
    payment_ev = (
        await inv.call("payment-agent", "get_payment_timeline", order_id=selected)
        if selected
        else None
    )
    topics = [claim.get("topic") for claim in request.get("claims", []) if isinstance(claim, dict)]
    refund_ev = (
        await inv.call("payment-agent", "get_refund_timeline", order_id=selected)
        if selected
        and any(
            t in topics
            for t in (
                "refund_pending",
                "refund_failed",
                "canceled_order_paid",
                "unavailable_order_paid",
            )
        )
        else None
    )
    if selected and case.get("investigation_scope", {}).get("include_product_context"):
        await inv.call("order-agent", "get_product_context", order_id=selected)
    policy_ev = await inv.call(
        "policy-agent", "get_policy", policy_version=case.get("policy_version", "EC_POLICY_V2")
    )
    items = data(items_ev, [])
    items = items if isinstance(items, list) else []
    shipment = data(shipment_ev, {})
    shipment = shipment if isinstance(shipment, dict) else {}
    payment = data(payment_ev, {})
    payment = payment if isinstance(payment, dict) else {}
    refund = data(refund_ev, {})
    refund = refund if isinstance(refund, dict) else {}
    raw_order = order
    raw_shipment = shipment
    order, items, shipment, payment, refund = scope_snapshot(
        case, history if isinstance(history, dict) else {}, order, items, shipment, payment, refund
    )
    policy = data(policy_ev, {})
    rules = policy.get("rules", {}) if isinstance(policy, dict) else {}
    shipment_verdict, complete, late_sellers = shipment_analysis(shipment)
    payment_verdict, captured, refunded, refundable = payment_analysis(payment, refund, items)
    issue = choose_issue(order, shipment_verdict, payment_verdict, topics, rules)
    rule = rules.get(issue, {}) if isinstance(rules, dict) else {}
    amount = min(refundable, money(rule.get("refund_brl"))) if rule else Decimal(0)
    status = rule.get("case_status", "needs_investigation") if rule else "needs_investigation"
    action = rule.get("recommended_action") if rule else None
    parties = rule.get("responsible_parties", []) if rule else []
    trace.emit(
        case_id=case_id, event_type="handoff", actor="specialist-agents", target="policy-agent"
    )
    trace.emit(
        case_id=case_id, event_type="policy_decided", actor="policy-agent", decision_code=issue
    )
    relevant = {"get_order", "get_policy", "get_customer_history", "get_order_items"}
    if issue.startswith("late_delivery"):
        relevant.add("get_shipment_summary")
    if (
        issue
        in {
            "payment_mismatch",
            "duplicate_charge",
            "valid_split_payment",
            "canceled_order_paid",
            "unavailable_order_paid",
            "refund_pending",
            "refund_failed",
        }
        or amount > 0
    ):
        relevant.add("get_payment_timeline")
    if issue.startswith("refund"):
        relevant.add("get_refund_timeline")
    refs = list(dict.fromkeys(ref for name, ref in inv.refs.items() if name in relevant))[:30]
    conflicts: list[dict[str, Any]] = []
    if raw_order.get("order_status") != order.get("order_status"):
        conflicts.append(
            {
                "field": "order_status",
                "sources": ["order", "customer"],
                "selected_source": "customer",
                "resolution_code": "case_time_window",
            }
        )
    if (
        raw_shipment.get("delivered_customer_at")
        and shipment.get("delivered_customer_at")
        and raw_shipment["delivered_customer_at"] != shipment["delivered_customer_at"]
    ):
        conflicts.append(
            {
                "field": "delivered_customer_at",
                "sources": ["order", "shipment"],
                "selected_source": "shipment",
                "resolution_code": "shipment_timeline_precedence",
            }
        )
    if history_orders and selected and selected not in history_orders:
        conflicts.append(
            {
                "field": "customer_order_link",
                "sources": ["customer", "order"],
                "selected_source": None,
                "resolution_code": "unresolved",
            }
        )
    confidence = 0.85 if selected and issue != "insufficient_evidence" else 0.3
    if conflicts:
        confidence = min(confidence, 0.65)
    if not policy_ev or not order_ev:
        confidence = min(confidence, 0.35)
    claims = []
    for claim in request.get("claims", [])[:5]:
        topic = claim.get("topic")
        verdict = (
            (
                "supported"
                if amount > 0 and amount >= captured - refunded
                else "unsupported"
                if selected
                else "insufficient_evidence"
            )
            if topic == "requested_full_refund"
            else (
                "supported"
                if topic == issue
                else "unsupported"
                if issue != "insufficient_evidence"
                else "insufficient_evidence"
            )
        )
        claims.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": refs,
            }
        )
    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": [],
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [selected] if selected else [],
            "item_ids": ids(items, "order_item_id"),
            "seller_ids": ids(items, "seller_id"),
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claims,
        "entity_resolution": {
            "status": "resolved" if selected else "not_found",
            "resolved_order_ids": [selected] if selected else [],
            "rejected_candidates": rejected,
            "confidence": 0.9
            if selected and selected in history_orders
            else 0.65
            if selected
            else 0.1,
        },
        "customer_context": {
            "customer_unique_id": history.get("customer_unique_id")
            if isinstance(history, dict)
            else None,
            "related_order_ids": [value for value in history_orders if value != selected],
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": late_sellers,
            "timeline_complete": complete,
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": float(captured) if payment_ev else None,
            "refunded_total_brl": float(refunded) if refund_ev else None,
            "refundable_total_brl": float(refundable) if payment_ev else None,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}]
            if issue != "insufficient_evidence"
            else [],
            "responsible_parties": parties if isinstance(parties, list) else [],
        },
        "evidence_refs": refs,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(amount),
            "refund_lines": [
                {"reason_code": issue, "amount_brl": float(amount), "entity_id": selected}
            ]
            if amount > 0
            else [],
        },
        "resolution_actions": [action] if isinstance(action, str) and action else [],
    }
    trace.emit(case_id=case_id, event_type="handoff", actor="policy-agent", target="verifier-agent")
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        decision_code="validated",
    )
    return output
