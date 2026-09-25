from __future__ import annotations

import asyncio
from collections.abc import Mapping

from .agent_contracts import EvidenceRecord
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

TOOL_PERMISSIONS: Mapping[str, frozenset[str]] = {
    "entity-customer-agent": frozenset({"get_order", "get_customer_history"}),
    "order-shipment-agent": frozenset(
        {"get_order_items", "get_shipment_summary", "get_sellers", "get_product_context"}
    ),
    "payment-refund-agent": frozenset({"get_payment_timeline", "get_refund_timeline"}),
    "policy-agent": frozenset({"get_policy"}),
    "verifier-agent": frozenset(),
}


class EvidenceBudgetExceeded(RuntimeError):
    pass


class ToolPermissionDenied(RuntimeError):
    pass


class CaseEvidenceContext:
    """Case-scoped MCP broker with permissions, de-duplication and provenance tracking."""

    def __init__(
        self,
        *,
        case_id: str,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        max_calls: int = 8,
        timeout_seconds: float = 45.0,
        timeout_retries: int = 1,
        tool_permissions: Mapping[str, frozenset[str]] = TOOL_PERMISSIONS,
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if timeout_retries < 0:
            raise ValueError("timeout_retries cannot be negative")
        self.case_id = case_id
        self.gateway = gateway
        self.trace = trace
        self.max_calls = max_calls
        self.timeout_seconds = timeout_seconds
        self.timeout_retries = timeout_retries
        self.tool_permissions = tool_permissions
        self.call_count = 0
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], EvidenceRecord] = {}
        self._inflight: dict[
            tuple[str, tuple[tuple[str, str], ...]], asyncio.Task[EvidenceRecord]
        ] = {}
        self._records: dict[str, EvidenceRecord] = {}
        self._consumed: set[tuple[str, str]] = set()

    async def fetch(self, actor: str, tool_name: str, **arguments: str) -> EvidenceRecord:
        """Fetch evidence without marking it consumed by a conclusion."""

        self._check_permission(actor, tool_name)
        if "case_id" in arguments:
            raise ValueError("case_id is owned by CaseEvidenceContext")
        normalized_arguments = tuple(sorted(arguments.items()))
        key = (tool_name, normalized_arguments)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch_uncached(tool_name, normalized_arguments))
            self._inflight[key] = task
        try:
            record = await task
        finally:
            if self._inflight.get(key) is task and task.done():
                self._inflight.pop(key, None)
        self._cache[key] = record
        self._records[record.evidence_ref] = record
        return record

    def consume(
        self, actor: str, evidence: EvidenceRecord, *, decision_code: str | None = None
    ) -> None:
        """Link evidence to an observable agent decision exactly once per actor."""

        known = self._records.get(evidence.evidence_ref)
        if known != evidence or evidence.case_id != self.case_id:
            raise ValueError("evidence does not belong to this case context")
        marker = (actor, evidence.evidence_ref)
        if marker in self._consumed:
            return
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            decision_code=decision_code,
            tool_name=evidence.tool_name,
            evidence_refs=[evidence.evidence_ref],
        )
        self._consumed.add(marker)

    @property
    def consumed_evidence_refs(self) -> tuple[str, ...]:
        return tuple(sorted({evidence_ref for _, evidence_ref in self._consumed}))

    def _check_permission(self, actor: str, tool_name: str) -> None:
        allowed = self.tool_permissions.get(actor)
        if allowed is None or tool_name not in allowed:
            raise ToolPermissionDenied(f"{actor} cannot call {tool_name}")

    async def _fetch_uncached(
        self, tool_name: str, arguments: tuple[tuple[str, str], ...]
    ) -> EvidenceRecord:
        attempts = self.timeout_retries + 1
        last_timeout: TimeoutError | None = None
        for _ in range(attempts):
            if self.call_count >= self.max_calls:
                raise EvidenceBudgetExceeded(
                    f"MCP call budget exceeded for {self.case_id}: {self.max_calls}"
                )
            self.call_count += 1
            try:
                response = await asyncio.wait_for(
                    self.gateway.call(tool_name, case_id=self.case_id, **dict(arguments)),
                    timeout=self.timeout_seconds,
                )
            except TimeoutError as exc:
                last_timeout = exc
                continue
            return EvidenceRecord(
                case_id=self.case_id,
                evidence_ref=response["evidence_ref"],
                domain=response["domain"],
                tool_name=tool_name,
                arguments=arguments,
                data=response["data"],
                warnings=tuple(response.get("warnings", ())),
            )
        raise TimeoutError(f"MCP tool {tool_name} timed out for {self.case_id}") from last_timeout
