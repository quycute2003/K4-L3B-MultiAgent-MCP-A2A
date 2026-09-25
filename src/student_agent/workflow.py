from __future__ import annotations

import asyncio
from typing import Any

from .agent_contracts import AgentArtifact
from .decision import build_output
from .evidence_context import CaseEvidenceContext
from .mcp_gateway import EvidenceGateway
from .orchestrator import TaskDispatcher
from .specialists import (
    investigate_entity,
    investigate_order_shipment,
    investigate_payment_refund,
    load_policy,
)
from .trace import TraceWriter
from .verifier import verify_output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the bounded L3B A2A workflow for one complaint case."""

    case_id = str(case["case_id"])
    evidence = CaseEvidenceContext(case_id=case_id, gateway=gateway, trace=trace)
    dispatcher = TaskDispatcher(case_id=case_id, evidence=evidence, trace=trace)

    entity_task = dispatcher.assign(
        recipient="entity-customer-agent",
        task_type="RESOLVE_ENTITY_AND_CUSTOMER",
        entity_scope=tuple(str(value) for value in case.get("candidate_order_ids", [])),
    )
    entity = await investigate_entity(entity_task, case, evidence)
    dispatcher.handoff(entity)

    resolved = entity.facts.get("resolved_order_ids", [])
    claimed = str(case.get("customer_request", {}).get("claimed_order_id") or "")
    order_id = str(resolved[0]) if resolved else claimed
    selected_order = entity.facts.get("selected_order")
    selected_order = selected_order if isinstance(selected_order, dict) else None
    claim_topic = _primary_claim_topic(case)

    order_task = dispatcher.assign(
        recipient="order-shipment-agent",
        task_type="INVESTIGATE_ORDER_SHIPMENT",
        entity_scope=(order_id,),
        payload={"claim_topic": claim_topic},
    )
    payment_task = dispatcher.assign(
        recipient="payment-refund-agent",
        task_type="RECONCILE_PAYMENT_REFUND",
        entity_scope=(order_id,),
        payload={"claim_topic": claim_topic},
    )
    policy_task = dispatcher.assign(
        recipient="policy-agent",
        task_type="LOAD_POLICY_AND_DECIDE",
        entity_scope=(order_id,),
    )
    order, payment, policy = await asyncio.gather(
        investigate_order_shipment(order_task, selected_order, evidence),
        investigate_payment_refund(payment_task, selected_order, evidence),
        load_policy(policy_task, str(case.get("policy_version") or ""), evidence),
    )
    for artifact in (order, payment, policy):
        dispatcher.handoff(artifact)

    output = build_output(case, entity, order, payment, policy)
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="verifier-agent",
        decision_code=str(output["assessment"]["primary_issue"]),
        evidence_refs=list(policy.evidence_refs),
        attributes={
            "case_status": str(output["assessment"]["case_status"]),
            "mcp_calls": evidence.call_count,
        },
    )

    verifier_task = dispatcher.assign(
        recipient="verifier-agent",
        task_type="VERIFY_FINAL_OUTPUT",
        entity_scope=(order_id,),
    )
    errors = verify_output(
        output, case_id=case_id, consumed_evidence_refs=evidence.consumed_evidence_refs
    )
    verifier = AgentArtifact(
        task_id=verifier_task.task_id,
        case_id=case_id,
        producer="verifier-agent",
        status="completed" if not errors else "failed",
        facts={"error_count": len(errors)},
        verdict="verified" if not errors else "rejected",
        confidence=1.0 if not errors else 0.0,
        warnings=tuple(errors),
    )
    dispatcher.handoff(verifier)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        target="coordinator",
        decision_code=verifier.verdict,
        attributes={"error_count": len(errors), "mcp_calls": evidence.call_count},
    )
    if errors:
        raise ValueError(f"verification failed for {case_id}: {'; '.join(errors)}")
    return output


def _primary_claim_topic(case: dict[str, Any]) -> str:
    for claim in case.get("customer_request", {}).get("claims", []):
        if isinstance(claim, dict) and claim.get("topic") != "requested_full_refund":
            return str(claim.get("topic") or "")
    return ""
