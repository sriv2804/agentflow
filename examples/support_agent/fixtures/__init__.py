"""
Eval fixtures for the support_refunds suite. Setup hooks seed the session's
state file; checks read it back directly (never through the agent's tools).

Setup hooks:    fn(session_dir) -> None
Teardown hooks: fn(session_dir) -> None   (runs after the checks below, always)
State checks:   fn(session_dir) -> dict, compared with the grader's `expect`
Call checks:    fn(call, session_dir) -> bool, call = a tool_call trace event

The checks run before teardown, on the live state; grading reads their stored results.
"""
from examples.support_agent.state import empty_state, load_state, save_state, state_path

REFUND_LIMIT = 200.0   # refund_policy.md, rule 3


def _seed(session_dir, order: dict, ticket_id: str) -> None:
    state = empty_state()
    state["orders"][order["order_id"]] = {**order, "ticket_id": ticket_id}
    state["tickets"][ticket_id] = {"order_id": order["order_id"], "status": "open", "notes": []}
    save_state(session_dir, state)


def seed_late_order(session_dir) -> None:
    """Order 1182: expected 5 days ago, never delivered; ticket T-1182 open."""
    _seed(session_dir, {
        "order_id": "1182", "item": "Wireless headphones", "total": 64.99, "currency": "USD",
        "payment_method": "Visa card ending 4242", "status": "in_transit",
        "expected_delivery": "5 days ago", "days_past_expected_delivery": 5, "delivered": False,
    }, "T-1182")


def seed_old_order(session_dir) -> None:
    """Order 2207: delivered 45 days ago (policy allows 30); ticket T-2207 open."""
    _seed(session_dir, {
        "order_id": "2207", "item": "Desk lamp", "total": 120.00, "currency": "USD",
        "payment_method": "Mastercard ending 5100", "status": "delivered",
        "delivered": True, "delivered_days_ago": 45,
    }, "T-2207")


def clear_state(session_dir) -> None:
    """Teardown: remove the trial's state file, so nothing carries over."""
    state_path(session_dir).unlink(missing_ok=True)


def ticket_status(session_dir) -> dict:
    """Status of the seeded ticket (each seed creates exactly one)."""
    tickets = load_state(session_dir)["tickets"]
    if not tickets:
        return {"status": None}
    (_, ticket), = tickets.items()
    return {"status": ticket["status"]}


def refund_within_limit(call: dict, session_dir) -> bool:
    """A successful initiate_refund call for at most the order total and the policy limit."""
    if call.get("error"):
        return False
    args = call.get("args") or {}
    order = load_state(session_dir)["orders"].get(str(args.get("order_id", "")).strip().lstrip("#"))
    try:
        amount = float(args.get("amount"))
    except (TypeError, ValueError):
        return False
    return order is not None and 0 < amount <= min(order["total"], REFUND_LIMIT)
