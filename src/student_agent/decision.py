from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from .agent_contracts import AgentArtifact
from .domain import money, money_number, order_conflicts, unique_strings

ALLOWED_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
}


def build_output(
    case: dict[str, Any],
    entity: AgentArtifact,
    order: AgentArtifact,
    payment: AgentArtifact,
    policy: AgentArtifact,
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    primary_claim = _primary_claim(case)
    primary_issue = primary_claim if primary_claim in ALLOWED_ISSUES else "insufficient_evidence"
    # Cross-validate with shipment evidence
    if primary_issue == "late_delivery_logistics" and order.verdict == "seller_delay":
        primary_issue = "late_delivery_seller"
    elif primary_issue == "late_delivery_seller" and order.verdict == "logistics_delay":
        primary_issue = "late_delivery_logistics"
    policy_rules = policy.facts.get("rules", {})
    rule = policy_rules.get(primary_issue, {}) if isinstance(policy_rules, dict) else {}
    if not isinstance(rule, dict):
        rule = {}

    selected_order = entity.facts.get("selected_order")
    selected_order = selected_order if isinstance(selected_order, dict) else None
    resolved_order_ids = unique_strings(entity.facts.get("resolved_order_ids", []))
    order_id = resolved_order_ids[0] if resolved_order_ids else None
    refund = money(rule.get("refund_brl"))
    action = str(rule.get("recommended_action") or "manual_investigation")
    case_status = str(rule.get("case_status") or "needs_investigation")
    # If policy says to refund but no amount, derive from evidence
    if refund <= 0 and action in {"issue_refund", "retry_refund"}:
        refund = money(payment.facts.get("refundable_total_brl"))
    if primary_issue == "insufficient_evidence":
        refund = Decimal("0.00")
        action = "manual_investigation"
        case_status = "needs_investigation"
    secondary = _secondary_issues(case, primary_issue, order, payment)

    responsible = rule.get("responsible_parties", [])
    if not isinstance(responsible, list):
        responsible = []
    responsible = [party for party in responsible if _valid_party(party)]
    if not responsible:
        responsible = [{"party_type": "unknown", "party_id": None}]
    responsible = _scope_responsible_parties(responsible, order)

    all_refs = _refs(entity, order, payment, policy)
    relevant_refs = _relevant_refs(primary_issue, entity, order, payment, policy)
    conflicts = order_conflicts(entity.facts.get("authoritative_order"), selected_order)
    conflicts.extend(_timeline_conflicts(order, payment))
    conflicts = conflicts[:5]
    evidence_complete = all(
        artifact.status == "completed" for artifact in (entity, order, payment, policy)
    )
    confidence = _confidence(primary_issue, entity, order, payment, policy, conflicts)

    refund_lines: list[dict[str, Any]] = []
    if refund > 0:
        party_id = next(
            (party.get("party_id") for party in responsible if party.get("party_id")),
            order_id,
        )
        refund_lines.append(
            {
                "reason_code": action,
                "amount_brl": money_number(refund),
                "entity_id": party_id,
            }
        )

    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": secondary,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": resolved_order_ids,
            "item_ids": unique_strings(order.facts.get("item_ids", [])),
            "seller_ids": unique_strings(order.facts.get("seller_ids", [])),
            "payment_references": unique_strings(payment.facts.get("payment_references", [])),
            "shipment_ids": unique_strings(order.facts.get("shipment_ids", [])),
        },
        "claim_assessments": _claim_assessments(
            case,
            primary_issue=primary_issue,
            refund=refund,
            captured=money(payment.facts.get("captured_total_brl")),
            action=action,
            case_status=case_status,
            relevant_refs=relevant_refs,
            policy_refs=list(policy.evidence_refs),
            payment_refs=list(payment.evidence_refs),
            confidence=confidence,
        ),
        "entity_resolution": {
            "status": entity.facts.get("status", "not_found"),
            "resolved_order_ids": resolved_order_ids,
            "rejected_candidates": unique_strings(entity.facts.get("rejected_candidates", [])),
            "confidence": round(entity.confidence, 2),
        },
        "customer_context": {
            "customer_unique_id": entity.facts.get("customer_unique_id"),
            "related_order_ids": unique_strings(entity.facts.get("related_order_ids", [])),
        },
        "shipment_analysis": {
            "verdict": order.verdict or "insufficient_evidence",
            "late_seller_ids": unique_strings(order.facts.get("late_seller_ids", [])),
            "timeline_complete": bool(order.facts.get("timeline_complete", False)),
        },
        "payment_analysis": {
            "verdict": payment.verdict or "insufficient_evidence",
            "captured_total_brl": _optional_number(payment.facts, "captured_total_brl"),
            "refunded_total_brl": _optional_number(payment.facts, "refunded_total_brl"),
            "refundable_total_brl": _optional_number(payment.facts, "refundable_total_brl"),
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}],
            "responsible_parties": responsible,
        },
        "evidence_refs": all_refs,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": money_number(refund),
            "refund_lines": refund_lines,
        },
        "resolution_actions": [action],
    }
    if not evidence_complete:
        output["assessment"]["confidence"] = min(output["assessment"]["confidence"], 0.65)
    return output


def _primary_claim(case: dict[str, Any]) -> str:
    claims = case.get("customer_request", {}).get("claims", [])
    for claim in claims:
        topic = claim.get("topic") if isinstance(claim, dict) else None
        if topic != "requested_full_refund" and isinstance(topic, str):
            return topic
    return "insufficient_evidence"


def _valid_party(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("party_type")
        in {
            "seller",
            "platform",
            "logistics_provider",
            "payment_provider",
            "customer",
            "unknown",
        }
        and (value.get("party_id") is None or isinstance(value.get("party_id"), str))
    )


def _scope_responsible_parties(
    parties: list[dict[str, Any]], order: AgentArtifact
) -> list[dict[str, Any]]:
    seller_ids = unique_strings(
        [*order.facts.get("late_seller_ids", []), *order.facts.get("seller_ids", [])]
    )
    result: list[dict[str, Any]] = []
    for party in parties:
        scoped = dict(party)
        if scoped.get("party_type") == "seller" and seller_ids:
            scoped["party_id"] = seller_ids[0]
        result.append(scoped)
    return result


def _refs(*artifacts: AgentArtifact) -> list[str]:
    return unique_strings(ref for artifact in artifacts for ref in artifact.evidence_refs)


def _relevant_refs(
    issue: str,
    entity: AgentArtifact,
    order: AgentArtifact,
    payment: AgentArtifact,
    policy: AgentArtifact,
) -> list[str]:
    refs = [
        *_tool_refs(entity, "get_order", "get_customer_history"),
        *_tool_refs(policy, "get_policy"),
    ]
    if issue in {"late_delivery_seller", "late_delivery_logistics"}:
        refs.extend(_tool_refs(order, "get_order_items", "get_shipment_summary"))
        if issue == "late_delivery_seller":
            refs.extend(_tool_refs(order, "get_sellers"))
    elif issue in {
        "valid_split_payment",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
    }:
        refs.extend(_tool_refs(payment, "get_payment_timeline", "get_refund_timeline"))
    else:
        refs.extend(_tool_refs(order, "get_order_items", "get_shipment_summary"))
        refs.extend(_tool_refs(payment, "get_payment_timeline", "get_refund_timeline"))
        if issue == "unavailable_order_paid":
            refs.extend(_tool_refs(order, "get_sellers"))
    return unique_strings(refs)


def _tool_refs(artifact: AgentArtifact, *tool_names: str) -> list[str]:
    evidence_by_tool = artifact.facts.get("evidence_by_tool")
    if not isinstance(evidence_by_tool, dict):
        return list(artifact.evidence_refs)
    return unique_strings(evidence_by_tool.get(tool_name) for tool_name in tool_names)


def _claim_assessments(
    case: dict[str, Any],
    *,
    primary_issue: str,
    refund: Decimal,
    captured: Decimal,
    action: str,
    case_status: str,
    relevant_refs: list[str],
    policy_refs: list[str],
    payment_refs: list[str],
    confidence: float,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    claims = case.get("customer_request", {}).get("claims", [])
    for claim in claims[:5]:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            continue
        topic = claim.get("topic")
        if topic == primary_issue:
            verdict = (
                "supported" if primary_issue != "insufficient_evidence" else "insufficient_evidence"
            )
            refs = relevant_refs
            claim_confidence = confidence
        elif topic == "requested_full_refund":
            refs = unique_strings([*payment_refs, *policy_refs])
            if case_status == "needs_investigation":
                verdict = "insufficient_evidence"
                claim_confidence = min(confidence, 0.75)
            elif refund <= 0:
                verdict = "unsupported"
                claim_confidence = confidence
            elif action in {"issue_refund", "retry_refund"} and (
                captured <= 0 or refund >= captured
            ):
                verdict = "supported"
                claim_confidence = confidence
            else:
                verdict = "partially_supported"
                claim_confidence = confidence
        else:
            verdict = "unsupported"
            refs = relevant_refs
            claim_confidence = min(confidence, 0.8)
        result.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": round(claim_confidence, 2),
                "evidence_refs": refs,
            }
        )
    return result


def _timeline_conflicts(order: AgentArtifact, payment: AgentArtifact) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    all_payment_events = payment.facts.get("all_events", [])
    selected_payment_events = payment.facts.get("events", [])
    if isinstance(all_payment_events, list) and len(all_payment_events) > len(
        selected_payment_events
    ):
        result.append(
            {
                "field": "payment_lifecycle",
                "sources": ["get_payment_timeline", "get_customer_history"],
                "selected_source": "get_payment_timeline",
                "resolution_code": "TEMPORAL_CONTEXT_MATCH",
            }
        )
    shipment = order.facts.get("shipment", {})
    events = order.facts.get("events", [])
    if (
        isinstance(shipment, dict)
        and isinstance(shipment.get("events"), list)
        and len(shipment["events"]) > len(events)
    ):
        result.append(
            {
                "field": "shipment_lifecycle",
                "sources": ["get_shipment_summary", "get_customer_history"],
                "selected_source": "get_customer_history",
                "resolution_code": "TEMPORAL_CONTEXT_MATCH",
            }
        )
    return result


def _confidence(
    issue: str,
    entity: AgentArtifact,
    order: AgentArtifact,
    payment: AgentArtifact,
    policy: AgentArtifact,
    conflicts: Iterable[dict[str, Any]],
) -> float:
    relevant = [entity, policy]
    if issue.startswith("late_delivery"):
        relevant.append(order)
    elif issue in {"canceled_order_paid", "unavailable_order_paid", "unsupported_claim"}:
        relevant.extend([order, payment])
    else:
        relevant.append(payment)
    if any(artifact.status == "failed" for artifact in relevant):
        return 0.45
    if any(artifact.status == "partial" for artifact in relevant):
        return 0.68
    return 0.94 if list(conflicts) else 0.98


def _optional_number(facts: dict[str, Any], key: str) -> float | None:
    value = facts.get(key)
    return float(value) if isinstance(value, int | float) else None


def _secondary_issues(
    case: dict[str, Any],
    primary_issue: str,
    order: AgentArtifact,
    payment: AgentArtifact,
) -> list[str]:
    """Derive secondary issues from claims and evidence."""
    issues: list[str] = []
    claims = case.get("customer_request", {}).get("claims", [])
    for claim in claims:
        if isinstance(claim, dict):
            topic = claim.get("topic")
            if isinstance(topic, str) and topic and topic != primary_issue:
                issues.append(topic)
    pv = payment.verdict
    if (
        pv
        and pv not in {"reconciled", "insufficient_evidence"}
        and pv != primary_issue
        and pv not in issues
    ):
        issues.append(pv)
    ov = order.verdict
    if (
        ov
        and ov not in {"on_time", "insufficient_evidence"}
        and ov != primary_issue
        and ov not in issues
    ):
        issues.append(ov)
    return list(dict.fromkeys(issues))[:10]
