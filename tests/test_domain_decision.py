from __future__ import annotations

from pathlib import Path

from student_agent.agent_contracts import AgentArtifact
from student_agent.contracts import Contracts
from student_agent.decision import build_output
from student_agent.domain import (
    captured_events,
    select_contextual_order,
    select_item_rows,
    shipment_events,
)
from student_agent.verifier import verify_output


def test_temporal_resolution_selects_latest_lifecycle_before_opening() -> None:
    future = {
        "order_id": "order-1",
        "order_purchase_timestamp": "2018-05-01T09:00:00-03:00",
    }
    relevant = {
        "order_id": "order-1",
        "order_purchase_timestamp": "2017-12-20T09:00:00-03:00",
    }
    selected = select_contextual_order(
        order_id="order-1",
        opened_at="2018-01-01T09:00:00-03:00",
        authoritative=future,
        history_orders=[future, relevant],
    )
    assert selected == relevant


def test_lifecycle_filters_items_and_captures_from_colliding_order_id() -> None:
    order = {
        "order_purchase_timestamp": "2017-12-20T09:00:00-03:00",
        "order_approved_at": "2017-12-20T10:00:00-03:00",
    }
    items = [
        {"order_item_id": "item-1", "shipping_limit_date": "2018-05-04T09:00:00-03:00"},
        {"order_item_id": "item-1", "shipping_limit_date": "2017-12-23T09:00:00-03:00"},
    ]
    events = [
        {
            "event_type": "captured",
            "status": "confirmed",
            "event_at": "2018-05-01T10:00:00-03:00",
            "amount_brl": "89.00",
        },
        {
            "event_type": "captured",
            "status": "confirmed",
            "event_at": "2017-12-20T10:00:00-03:00",
            "amount_brl": "16.00",
        },
    ]
    assert select_item_rows(items, order) == [items[1]]
    assert captured_events(events, order) == [events[1]]


def test_shipment_events_match_resolved_delivery_not_broad_order_window() -> None:
    order = {
        "order_status": "delivered",
        "order_purchase_timestamp": "2018-06-25T09:00:00-03:00",
        "order_delivered_customer_date": "2018-07-04T09:00:00-03:00",
    }
    relevant = {
        "event_at": "2018-07-04T09:00:00-03:00",
        "event_type": "delivered",
    }
    collision = {
        "event_at": "2018-08-20T09:00:00-03:00",
        "event_type": "delivered_late",
        "actor": "logistics_provider",
    }
    assert shipment_events([relevant, collision], order) == [relevant]

    canceled = {
        "order_status": "canceled",
        "order_purchase_timestamp": "2018-07-27T09:00:00-03:00",
        "order_delivered_customer_date": None,
    }
    assert shipment_events([collision], canceled) == []


def test_decision_output_passes_schema_and_invariants() -> None:
    def artifact(producer: str, facts: dict, verdict: str, suffix: str) -> AgentArtifact:
        return AgentArtifact(
            task_id=f"case:task:{suffix}",
            case_id="L3B_CASE_001",
            producer=producer,
            status="completed",
            facts=facts,
            verdict=verdict,
            confidence=0.98,
            evidence_refs=(f"ev_{suffix * 24}",),
        )

    selected_order = {
        "order_id": "order-1",
        "order_status": "delivered",
        "order_purchase_timestamp": "2017-12-20T09:00:00-03:00",
    }
    entity = artifact(
        "entity-customer-agent",
        {
            "status": "resolved",
            "resolved_order_ids": ["order-1"],
            "rejected_candidates": ["candidate-1"],
            "selected_order": selected_order,
            "authoritative_order": selected_order,
            "customer_unique_id": "customer-1",
            "related_order_ids": ["order-1"],
        },
        "resolved",
        "a",
    )
    order = artifact(
        "order-shipment-agent",
        {
            "item_ids": ["item-1"],
            "seller_ids": ["seller-1"],
            "shipment_ids": [],
            "late_seller_ids": [],
            "timeline_complete": True,
            "shipment": {"events": []},
            "events": [],
        },
        "logistics_delay",
        "b",
    )
    payment = artifact(
        "payment-refund-agent",
        {
            "captured_total_brl": 16.0,
            "refunded_total_brl": 0.0,
            "refundable_total_brl": 16.0,
            "payment_references": [],
            "all_events": [],
            "events": [],
        },
        "reconciled",
        "c",
    )
    policy = artifact(
        "policy-agent",
        {
            "rules": {
                "late_delivery_logistics": {
                    "case_status": "action_required",
                    "recommended_action": "refund_freight",
                    "refund_brl": 16.0,
                    "responsible_parties": [{"party_type": "logistics_provider", "party_id": None}],
                }
            }
        },
        "policy_loaded",
        "d",
    )
    case = {
        "case_id": "L3B_CASE_001",
        "customer_request": {
            "claims": [
                {"claim_id": "claim-1", "topic": "late_delivery_logistics"},
                {"claim_id": "claim-2", "topic": "requested_full_refund"},
            ]
        },
    }
    output = build_output(case, entity, order, payment, policy)
    root = Path(__file__).resolve().parents[1]
    Contracts(root / "contracts" / "schemas").validate_output(output, "test output")
    consumed = tuple(output["evidence_refs"])
    assert verify_output(output, case_id=case["case_id"], consumed_evidence_refs=consumed) == []
    assert output["claim_assessments"][1]["verdict"] == "partially_supported"
