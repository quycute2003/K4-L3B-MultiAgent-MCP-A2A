from __future__ import annotations

import asyncio
from typing import Any

import pytest

from student_agent.agent_contracts import AgentArtifact
from student_agent.evidence_context import (
    CaseEvidenceContext,
    EvidenceBudgetExceeded,
    ToolPermissionDenied,
)
from student_agent.orchestrator import TaskDispatcher


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        suffix = len(self.calls)
        return {
            "evidence_ref": f"ev_{suffix:024d}",
            "domain": "order",
            "data": {"ok": True},
        }


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


def make_context(*, max_calls: int = 11) -> tuple[CaseEvidenceContext, FakeGateway, FakeTrace]:
    gateway = FakeGateway()
    trace = FakeTrace()
    context = CaseEvidenceContext(
        case_id="L3B_CASE_001",
        gateway=gateway,  # type: ignore[arg-type]
        trace=trace,  # type: ignore[arg-type]
        max_calls=max_calls,
    )
    return context, gateway, trace


def test_case_evidence_context_caches_and_consumes_once() -> None:
    async def scenario() -> tuple[CaseEvidenceContext, FakeGateway, FakeTrace]:
        context, gateway, trace = make_context()
        first = await context.fetch("entity-customer-agent", "get_order", order_id="order-1")
        second = await context.fetch("entity-customer-agent", "get_order", order_id="order-1")
        assert first is second
        context.consume("entity-customer-agent", first, decision_code="ORDER_RESOLVED")
        context.consume("entity-customer-agent", first, decision_code="ORDER_RESOLVED")
        return context, gateway, trace

    context, gateway, trace = asyncio.run(scenario())
    assert len(gateway.calls) == 1
    assert context.call_count == 1
    assert context.consumed_evidence_refs == ("ev_000000000000000000000001",)
    assert [event["event_type"] for event in trace.events] == ["tool_result_consumed"]


def test_case_evidence_context_enforces_permissions_and_budget() -> None:
    async def scenario() -> None:
        context, _, _ = make_context(max_calls=1)
        with pytest.raises(ToolPermissionDenied):
            await context.fetch("verifier-agent", "get_order", order_id="order-1")
        await context.fetch("entity-customer-agent", "get_order", order_id="order-1")
        with pytest.raises(EvidenceBudgetExceeded):
            await context.fetch("entity-customer-agent", "get_order", order_id="order-2")

    asyncio.run(scenario())


def test_dispatcher_emits_assignment_and_handoff() -> None:
    context, _, trace = make_context()
    dispatcher = TaskDispatcher(
        case_id="L3B_CASE_001",
        evidence=context,
        trace=trace,  # type: ignore[arg-type]
    )
    task = dispatcher.assign(
        recipient="order-shipment-agent",
        task_type="INVESTIGATE_SHIPMENT",
        entity_scope=("order-1",),
    )
    dispatcher.handoff(
        AgentArtifact(
            task_id=task.task_id,
            case_id=task.case_id,
            producer=task.recipient,
            status="completed",
            verdict="on_time",
            confidence=0.9,
        )
    )
    assert [event["event_type"] for event in trace.events] == ["task_assigned", "handoff"]
