from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

MONEY_QUANTUM = Decimal("0.01")


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def money(value: Any) -> Decimal:
    if value is None:
        return Decimal("0.00")
    try:
        return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0.00")


def money_number(value: Decimal) -> float:
    return float(value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP))


def unique_strings(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def select_contextual_order(
    *,
    order_id: str,
    opened_at: str,
    authoritative: Mapping[str, Any] | None,
    history_orders: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Select the lifecycle instance closest to, but not after, complaint opening."""

    opened = parse_timestamp(opened_at)
    candidates = [row for row in history_orders if row.get("order_id") == order_id]
    if authoritative and authoritative.get("order_id") == order_id:
        candidates.append(dict(authoritative))
    if not candidates:
        return None

    def timestamp(row: Mapping[str, Any]) -> datetime | None:
        return parse_timestamp(row.get("order_purchase_timestamp"))

    prior = [
        row
        for row in candidates
        if opened is not None and timestamp(row) is not None and timestamp(row) <= opened
    ]
    if prior:
        return dict(max(prior, key=lambda row: timestamp(row) or datetime.min.astimezone()))
    if opened is not None:
        dated = [row for row in candidates if timestamp(row) is not None]
        if dated:
            return dict(min(dated, key=lambda row: abs(timestamp(row) - opened)))
    return dict(candidates[0])


def select_item_rows(
    rows: list[dict[str, Any]], selected_order: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    if not rows or selected_order is None:
        return []
    purchase = parse_timestamp(selected_order.get("order_purchase_timestamp"))
    if purchase is None:
        return rows
    expected_limit = purchase + timedelta(days=3)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        item_id = str(row.get("order_item_id") or f"row-{index}")
        grouped.setdefault(item_id, []).append(row)

    def distance(row: Mapping[str, Any]) -> float:
        limit = parse_timestamp(row.get("shipping_limit_date") or row.get("shipping_limit_at"))
        return abs((limit - expected_limit).total_seconds()) if limit else float("inf")

    return [min(group, key=distance) for group in grouped.values()]


def lifecycle_events(
    events: list[dict[str, Any]],
    selected_order: Mapping[str, Any] | None,
    *,
    days_before: int = 1,
    days_after: int = 60,
) -> list[dict[str, Any]]:
    if selected_order is None:
        return []
    purchase = parse_timestamp(selected_order.get("order_purchase_timestamp"))
    if purchase is None:
        return []
    lower = purchase - timedelta(days=days_before)
    upper = purchase + timedelta(days=days_after)
    return [
        event
        for event in events
        if (stamp := parse_timestamp(event.get("event_at"))) is not None and lower <= stamp <= upper
    ]


def shipment_events(
    events: list[dict[str, Any]], selected_order: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """Select shipment events belonging to the resolved lifecycle instance."""

    if selected_order is None:
        return []
    status = selected_order.get("order_status")
    delivered = parse_timestamp(selected_order.get("order_delivered_customer_date"))
    if status == "delivered" and delivered is not None:
        return [
            event
            for event in events
            if (stamp := parse_timestamp(event.get("event_at"))) is not None
            and abs(stamp - delivered) <= timedelta(hours=48)
        ]
    scoped = lifecycle_events(events, selected_order, days_after=30)
    return [
        event
        for event in scoped
        if event.get("event_type") not in {"delivered_late", "delivered", "out_for_delivery"}
    ]


def captured_events(
    events: list[dict[str, Any]], selected_order: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    if selected_order is None:
        return []
    approved = parse_timestamp(selected_order.get("order_approved_at"))
    captures = [
        event
        for event in events
        if event.get("event_type") == "captured" and event.get("status") == "confirmed"
    ]
    if approved is None:
        return lifecycle_events(captures, selected_order, days_after=2)
    close = [
        event
        for event in captures
        if (stamp := parse_timestamp(event.get("event_at"))) is not None
        and abs(stamp - approved) <= timedelta(hours=36)
    ]
    if close or not captures:
        return close
    dated = [event for event in captures if parse_timestamp(event.get("event_at")) is not None]
    if not dated:
        return []
    nearest = min(
        dated,
        key=lambda event: abs(parse_timestamp(event["event_at"]) - approved),  # type: ignore[operator]
    )
    return [nearest]


def order_conflicts(
    authoritative: Mapping[str, Any] | None,
    selected: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if not authoritative or not selected or dict(authoritative) == dict(selected):
        return []
    conflicts: list[dict[str, Any]] = []
    groups = {
        "order_status": ("order_status",),
        "order_purchase_timestamp": ("order_purchase_timestamp", "order_approved_at"),
        "order_delivery_timeline": (
            "order_delivered_carrier_date",
            "order_delivered_customer_date",
            "order_estimated_delivery_date",
        ),
    }
    for field, keys in groups.items():
        if any(authoritative.get(key) != selected.get(key) for key in keys):
            conflicts.append(
                {
                    "field": field,
                    "sources": ["get_order", "get_customer_history"],
                    "selected_source": "get_customer_history",
                    "resolution_code": "TEMPORAL_CONTEXT_MATCH",
                }
            )
    return conflicts
