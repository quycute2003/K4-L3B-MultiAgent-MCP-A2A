from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

AgentStatus = Literal["completed", "partial", "failed"]


@dataclass(frozen=True, slots=True)
class AgentTask:
    """Typed in-process A2A task handed from the coordinator to one specialist."""

    task_id: str
    case_id: str
    sender: str
    recipient: str
    task_type: str
    entity_scope: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    evidence_budget: int = 0
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """Validated MCP evidence plus the observable metadata needed for provenance."""

    case_id: str
    evidence_ref: str
    domain: str
    tool_name: str
    arguments: tuple[tuple[str, str], ...]
    data: Any
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentArtifact:
    """Structured specialist result returned to the coordinator."""

    task_id: str
    case_id: str
    producer: str
    status: AgentStatus
    facts: dict[str, Any] = field(default_factory=dict)
    verdict: str | None = None
    confidence: float = 0.0
    evidence_refs: tuple[str, ...] = ()
    conflicts: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("artifact confidence must be between 0 and 1")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("artifact evidence_refs must be unique")
