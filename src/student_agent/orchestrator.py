from __future__ import annotations

from dataclasses import replace
from typing import Any

from .agent_contracts import AgentArtifact, AgentTask
from .evidence_context import CaseEvidenceContext
from .trace import TraceWriter


class TaskDispatcher:
    """Create bounded in-process A2A tasks and record observable handoffs."""

    def __init__(self, *, case_id: str, evidence: CaseEvidenceContext, trace: TraceWriter) -> None:
        self.case_id = case_id
        self.evidence = evidence
        self.trace = trace
        self._sequence = 0
        self._open_tasks: dict[str, AgentTask] = {}

    def assign(
        self,
        *,
        recipient: str,
        task_type: str,
        entity_scope: tuple[str, ...] = (),
        payload: dict[str, Any] | None = None,
        sender: str = "coordinator",
    ) -> AgentTask:
        if recipient not in self.evidence.tool_permissions:
            raise ValueError(f"unknown specialist actor: {recipient}")
        self._sequence += 1
        task = AgentTask(
            task_id=f"{self.case_id}:{self._sequence:02d}",
            case_id=self.case_id,
            sender=sender,
            recipient=recipient,
            task_type=task_type,
            entity_scope=entity_scope,
            allowed_tools=tuple(sorted(self.evidence.tool_permissions[recipient])),
            evidence_budget=max(0, self.evidence.max_calls - self.evidence.call_count),
            payload=payload or {},
        )
        self._open_tasks[task.task_id] = task
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor=sender,
            target=recipient,
            decision_code=task_type,
            attributes={"task_id": task.task_id},
        )
        return task

    def handoff(self, artifact: AgentArtifact, *, target: str = "coordinator") -> None:
        task = self._open_tasks.get(artifact.task_id)
        if task is None:
            raise ValueError(f"unknown or completed task: {artifact.task_id}")
        if artifact.case_id != self.case_id or artifact.producer != task.recipient:
            raise ValueError("artifact does not match the assigned task")
        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=artifact.producer,
            target=target,
            decision_code=artifact.status,
            evidence_refs=list(artifact.evidence_refs),
            attributes={"task_id": artifact.task_id},
        )
        self._open_tasks.pop(artifact.task_id)

    def with_remaining_budget(self, task: AgentTask) -> AgentTask:
        """Refresh a task envelope before a bounded repair handoff."""

        remaining = max(0, self.evidence.max_calls - self.evidence.call_count)
        return replace(task, evidence_budget=remaining)
