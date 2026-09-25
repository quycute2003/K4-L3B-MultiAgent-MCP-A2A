from __future__ import annotations

import asyncio
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from .agent_contracts import AgentArtifact, AgentTask, EvidenceRecord
from .domain import (
    captured_events,
    lifecycle_events,
    money,
    money_number,
    parse_timestamp,
    select_contextual_order,
    select_item_rows,
    shipment_events,
    unique_strings,
)
from .evidence_context import CaseEvidenceContext


async def _optional_fetch(
    evidence: CaseEvidenceContext, actor: str, tool: str, **arguments: str
) -> tuple[EvidenceRecord | None, str | None]:
    try:
        return await evidence.fetch(actor, tool, **arguments), None
    except (RuntimeError, TimeoutError, ValueError) as exc:
        return None, f"{tool}: {exc}"


def _artifact(
    task: AgentTask,
    *,
    records: list[EvidenceRecord],
    facts: dict[str, Any],
    verdict: str | None,
    warnings: list[str],
    confidence: float = 0.96,
) -> AgentArtifact:
    status = "completed" if not warnings else ("partial" if records else "failed")
    return AgentArtifact(
        task_id=task.task_id,
        case_id=task.case_id,
        producer=task.recipient,
        status=status,
        facts=facts,
        verdict=verdict,
        confidence=confidence if status == "completed" else min(confidence, 0.7),
        evidence_refs=tuple(record.evidence_ref for record in records),
        warnings=tuple(warnings),
    )


async def investigate_entity(
    task: AgentTask, case: dict[str, Any], evidence: CaseEvidenceContext
) -> AgentArtifact:
    actor = task.recipient
    claimed = str(case["customer_request"].get("claimed_order_id") or "")
    candidates = unique_strings(case.get("candidate_order_ids", []))
    order_result, history_result = await asyncio.gather(
        _optional_fetch(evidence, actor, "get_order", order_id=claimed),
        _optional_fetch(
            evidence,
            actor,
            "get_customer_history",
            customer_unique_id=str(case.get("customer_unique_id_hint") or ""),
        ),
    )
    order_record, order_warning = order_result
    history_record, history_warning = history_result
    records = [record for record in (order_record, history_record) if record is not None]
    warnings = [warning for warning in (order_warning, history_warning) if warning is not None]
    for record in records:
        evidence.consume(actor, record, decision_code="ENTITY_CONTEXT_USED")

    authoritative = (
        order_record.data if order_record and isinstance(order_record.data, dict) else None
    )
    history_data = (
        history_record.data if history_record and isinstance(history_record.data, dict) else {}
    )
    history_orders = history_data.get("orders", [])
    if not isinstance(history_orders, list):
        history_orders = []
    history_orders = [row for row in history_orders if isinstance(row, dict)]
    selected = select_contextual_order(
        order_id=claimed,
        opened_at=str(case.get("opened_at") or ""),
        authoritative=authoritative,
        history_orders=history_orders,
    )
    resolved = [claimed] if selected is not None else []
    rejected = [candidate for candidate in candidates if candidate not in resolved]
    status = "resolved" if selected is not None else ("ambiguous" if candidates else "not_found")
    facts = {
        "status": status,
        "resolved_order_ids": resolved,
        "rejected_candidates": rejected,
        "selected_order": selected,
        "authoritative_order": authoritative,
        "history_orders": history_orders,
        "customer_unique_id": history_data.get("customer_unique_id")
        or case.get("customer_unique_id_hint"),
        "related_order_ids": unique_strings(row.get("order_id") for row in history_orders),
        "evidence_by_tool": {record.tool_name: record.evidence_ref for record in records},
    }
    confidence = 0.98 if selected is not None and history_record is not None else 0.55
    return _artifact(
        task,
        records=records,
        facts=facts,
        verdict=status,
        warnings=warnings,
        confidence=confidence,
    )


async def investigate_order_shipment(
    task: AgentTask, selected_order: Mapping[str, Any] | None, evidence: CaseEvidenceContext
) -> AgentArtifact:
    actor = task.recipient
    order_id = str(task.entity_scope[0])
    requests = [
        _optional_fetch(evidence, actor, "get_order_items", order_id=order_id),
        _optional_fetch(evidence, actor, "get_shipment_summary", order_id=order_id),
    ]
    calls = await asyncio.gather(*requests)
    records = [record for record, _ in calls if record is not None]
    warnings = [warning for _, warning in calls if warning is not None]
    for record in records:
        evidence.consume(actor, record, decision_code="ORDER_SHIPMENT_FACT_USED")
    by_tool = {record.tool_name: record for record in records}
    item_data = by_tool.get("get_order_items")
    rows = item_data.data if item_data and isinstance(item_data.data, list) else []
    rows = [row for row in rows if isinstance(row, dict)]
    selected_items = select_item_rows(rows, selected_order)
    shipment_record = by_tool.get("get_shipment_summary")
    shipment = (
        shipment_record.data if shipment_record and isinstance(shipment_record.data, dict) else {}
    )
    raw_events = shipment.get("events", []) if isinstance(shipment, dict) else []
    events = shipment_events(
        [event for event in raw_events if isinstance(event, dict)], selected_order
    )
    verdict = _shipment_verdict(selected_order, selected_items, events)
    late_seller_ids = (
        unique_strings(item.get("seller_id") for item in selected_items)
        if verdict == "seller_delay"
        else []
    )
    timeline_complete = _shipment_timeline_complete(selected_order)
    expected_total = sum(
        (money(item.get("price")) + money(item.get("freight_value")) for item in selected_items),
        Decimal("0.00"),
    )
    facts = {
        "items": selected_items,
        "all_items": rows,
        "shipment": shipment,
        "events": events,
        "item_ids": unique_strings(item.get("order_item_id") for item in selected_items),
        "seller_ids": unique_strings(item.get("seller_id") for item in selected_items),
        "shipment_ids": unique_strings(event.get("shipment_id") for event in events),
        "late_seller_ids": late_seller_ids,
        "timeline_complete": timeline_complete,
        "expected_total_brl": money_number(expected_total),
        "evidence_by_tool": {record.tool_name: record.evidence_ref for record in records},
    }
    return _artifact(task, records=records, facts=facts, verdict=verdict, warnings=warnings)


def _shipment_verdict(
    selected_order: Mapping[str, Any] | None,
    selected_items: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> str:
    for event in events:
        event_type = event.get("event_type")
        actor = event.get("actor")
        if event_type in {"lost", "shipment_lost"}:
            return "lost"
        if event_type in {"returned", "shipment_returned"}:
            return "returned"
        if event_type == "delivered_late" and actor == "seller":
            return "seller_delay"
        if event_type == "delivered_late" and actor == "logistics_provider":
            return "logistics_delay"
    if selected_order is None:
        return "insufficient_evidence"
    status = selected_order.get("order_status")
    if status != "delivered":
        return "insufficient_evidence"
    delivered = parse_timestamp(selected_order.get("order_delivered_customer_date"))
    estimated = parse_timestamp(selected_order.get("order_estimated_delivery_date"))
    carrier = parse_timestamp(selected_order.get("order_delivered_carrier_date"))
    limits = [parse_timestamp(item.get("shipping_limit_date")) for item in selected_items]
    if carrier is not None and any(limit is not None and carrier > limit for limit in limits):
        return "seller_delay"
    if delivered is not None and estimated is not None:
        return "logistics_delay" if delivered > estimated else "on_time"
    return "insufficient_evidence"


def _shipment_timeline_complete(selected_order: Mapping[str, Any] | None) -> bool:
    if selected_order is None:
        return False
    required = [
        "order_purchase_timestamp",
        "order_approved_at",
        "order_estimated_delivery_date",
    ]
    if selected_order.get("order_status") == "delivered":
        required.extend(["order_delivered_carrier_date", "order_delivered_customer_date"])
    return all(parse_timestamp(selected_order.get(field)) is not None for field in required)


async def investigate_payment_refund(
    task: AgentTask,
    selected_order: Mapping[str, Any] | None,
    evidence: CaseEvidenceContext,
) -> AgentArtifact:
    actor = task.recipient
    order_id = str(task.entity_scope[0])
    claim_topic = str(task.payload.get("claim_topic") or "")
    requests = [
        _optional_fetch(evidence, actor, "get_payment_timeline", order_id=order_id),
    ]
    if claim_topic in {"refund_pending", "refund_failed"}:
        requests.append(_optional_fetch(evidence, actor, "get_refund_timeline", order_id=order_id))
    calls = await asyncio.gather(*requests)
    records = [record for record, _ in calls if record is not None]
    warnings = [warning for _, warning in calls if warning is not None]
    for record in records:
        evidence.consume(actor, record, decision_code="PAYMENT_REFUND_FACT_USED")
    by_tool = {record.tool_name: record for record in records}
    payment_record = by_tool.get("get_payment_timeline")
    timeline = (
        payment_record.data if payment_record and isinstance(payment_record.data, dict) else {}
    )
    all_events = [event for event in timeline.get("events", []) if isinstance(event, dict)]
    captures = captured_events(all_events, selected_order)
    lifecycle = lifecycle_events(all_events, selected_order)
    refund_record = by_tool.get("get_refund_timeline")
    refund_data = (
        refund_record.data if refund_record and isinstance(refund_record.data, dict) else {}
    )
    refund_events = lifecycle_events(
        [event for event in refund_data.get("events", []) if isinstance(event, dict)],
        selected_order,
    )
    captured_total = sum((money(event.get("amount_brl")) for event in captures), Decimal("0"))
    completed_statuses = {"confirmed", "completed", "refunded", "succeeded", "success"}
    refunded_total = sum(
        (
            money(event.get("amount_brl"))
            for event in refund_events
            if event.get("status") in completed_statuses
            and event.get("event_type") in {"refunded", "refund_completed", "refund_issued"}
        ),
        Decimal("0"),
    )
    verdict = _payment_verdict(claim_topic, lifecycle, refund_events, captures)
    payments_list = timeline.get("payments", [])
    if not isinstance(payments_list, list):
        payments_list = []
    payment_refs = unique_strings(
        p.get("payment_id") or p.get("payment_sequential") or p.get("reference")
        for p in payments_list
        if isinstance(p, dict)
    )
    if not payment_refs:
        payment_refs = unique_strings(
            event.get("payment_id") or event.get("reference_id")
            for event in all_events
            if isinstance(event, dict)
        )
    facts = {
        "payments": payments_list,
        "all_events": all_events,
        "events": lifecycle,
        "captures": captures,
        "refund_events": refund_events,
        "captured_total_brl": money_number(captured_total),
        "refunded_total_brl": money_number(refunded_total),
        "refundable_total_brl": money_number(max(captured_total - refunded_total, Decimal("0"))),
        "payment_references": payment_refs,
        "evidence_by_tool": {record.tool_name: record.evidence_ref for record in records},
    }
    return _artifact(task, records=records, facts=facts, verdict=verdict, warnings=warnings)


def _payment_verdict(
    claim_topic: str,
    events: list[dict[str, Any]],
    refund_events: list[dict[str, Any]],
    captures: list[dict[str, Any]],
) -> str:
    statuses = {str(event.get("status")) for event in refund_events}
    if "failed" in statuses:
        return "refund_failed"
    if "pending" in statuses:
        return "refund_pending"
    if any(status in {"completed", "refunded", "succeeded", "success"} for status in statuses):
        return "refunded"
    if any(event.get("event_type") == "reconciliation_mismatch" for event in events):
        return "capture_mismatch"
    if claim_topic == "duplicate_charge" and len(captures) >= 2:
        return "duplicate_capture"
    return "reconciled" if captures else "insufficient_evidence"


async def load_policy(
    task: AgentTask, policy_version: str, evidence: CaseEvidenceContext
) -> AgentArtifact:
    actor = task.recipient
    record, warning = await _optional_fetch(
        evidence, actor, "get_policy", policy_version=policy_version
    )
    records = [record] if record is not None else []
    warnings = [warning] if warning is not None else []
    if record is not None:
        evidence.consume(actor, record, decision_code="POLICY_RULE_USED")
    facts = record.data if record is not None and isinstance(record.data, dict) else {}
    facts = dict(facts)
    facts["evidence_by_tool"] = (
        {record.tool_name: record.evidence_ref} if record is not None else {}
    )
    return _artifact(
        task,
        records=records,
        facts=facts,
        verdict="policy_loaded" if record is not None else "policy_unavailable",
        warnings=warnings,
        confidence=0.99,
    )
