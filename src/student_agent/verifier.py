from __future__ import annotations

from decimal import Decimal
from typing import Any

from .domain import money


def verify_output(
    output: dict[str, Any], *, case_id: str, consumed_evidence_refs: tuple[str, ...]
) -> list[str]:
    errors: list[str] = []
    if output.get("case_id") != case_id:
        errors.append("case_id mismatch")
    submitted_refs = output.get("evidence_refs", [])
    if not isinstance(submitted_refs, list) or not submitted_refs:
        errors.append("missing evidence_refs")
    elif not set(submitted_refs).issubset(set(consumed_evidence_refs)):
        errors.append("output contains unconsumed evidence")

    financial = output.get("financial_resolution", {})
    lines = financial.get("refund_lines", []) if isinstance(financial, dict) else []
    line_total = sum(
        (money(line.get("amount_brl")) for line in lines if isinstance(line, dict)),
        Decimal("0"),
    )
    recommended = (
        money(financial.get("recommended_refund_brl"))
        if isinstance(financial, dict)
        else Decimal("0")
    )
    if line_total != recommended:
        errors.append("refund lines do not sum to recommended refund")
    status = output.get("assessment", {}).get("case_status")
    actions = output.get("resolution_actions", [])
    if status == "no_action" and recommended > 0:
        errors.append("no_action cannot recommend a refund")
    if len(actions) != len(set(actions)):
        errors.append("duplicate resolution actions")
    shipment = output.get("shipment_analysis", {})
    if shipment.get("verdict") != "seller_delay" and shipment.get("late_seller_ids"):
        errors.append("late_seller_ids require seller_delay")
    entity = output.get("entity_resolution", {})
    if set(entity.get("resolved_order_ids", [])) & set(entity.get("rejected_candidates", [])):
        errors.append("resolved and rejected candidates overlap")
    return errors
