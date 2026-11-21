"""
Local stub tools for the support agent. No network: everything reads and
writes the session's support_state.json (see state.py).
"""
from pathlib import Path

from examples.support_agent.state import load_state, save_state
from src.tools.common import tool, ToolContext

POLICY_PATH = Path(__file__).parent / "fixtures" / "refund_policy.md"
REFUND_ETA = "5 to 7 business days"


def _session_dir(ctx: ToolContext) -> Path:
    return Path(ctx.working_dir) / ctx.storage_session_id


def _order_key(order_id) -> str:
    return str(order_id).strip().lstrip("#")


@tool(name="lookup_order", description="Look up an order by its order number: status, dates, total, payment method and its support ticket")
async def lookup_order(order_id: str, _ctx: ToolContext = None) -> str:
    """
    Args:
        order_id: the order number, e.g. "1182"
    """
    state = load_state(_session_dir(_ctx))
    order = state["orders"].get(_order_key(order_id))
    if order is None:
        raise ValueError(f"No order found with number {order_id}")
    return str(order)


@tool(name="get_policy", description="Get the company refund policy")
async def get_policy(_ctx: ToolContext = None) -> str:
    return POLICY_PATH.read_text(encoding="utf-8")


@tool(name="initiate_refund", description="Initiate a refund for an order to a payment method")
async def initiate_refund(order_id: str, amount: float, method: str = "original_payment", _ctx: ToolContext = None) -> str:
    """
    Args:
        order_id: the order number
        amount: amount to refund, in the order's currency
        method: where the refund goes, "original_payment" or "store_credit"
    """
    session_dir = _session_dir(_ctx)
    state = load_state(session_dir)
    key = _order_key(order_id)
    order = state["orders"].get(key)
    if order is None:
        raise ValueError(f"No order found with number {order_id}")
    amount = float(amount)
    if amount <= 0:
        raise ValueError("amount must be positive")
    refund = {"refund_id": f"R-{key}-{len(state['refunds']) + 1}", "order_id": key, "amount": amount,
              "method": method, "eta": REFUND_ETA}
    state["refunds"].append(refund)
    save_state(session_dir, state)
    return f"Refund {refund['refund_id']} of {amount:.2f} {order.get('currency', '')} to {method} initiated; arrives in {REFUND_ETA}."


@tool(name="update_ticket", description="Update a support ticket's status (open, pending, resolved, closed) and add a note")
async def update_ticket(ticket_id: str, status: str, note: str = "", _ctx: ToolContext = None) -> str:
    """
    Args:
        ticket_id: the ticket id, e.g. "T-1182"
        status: one of open, pending, resolved, closed
        note: a short note for the ticket history
    """
    if status not in ("open", "pending", "resolved", "closed"):
        raise ValueError(f"Invalid status {status!r}: use open, pending, resolved or closed")
    session_dir = _session_dir(_ctx)
    state = load_state(session_dir)
    ticket = state["tickets"].get(str(ticket_id).strip())
    if ticket is None:
        raise ValueError(f"No ticket {ticket_id}")
    ticket["status"] = status
    if note:
        ticket.setdefault("notes", []).append(note)
    save_state(session_dir, state)
    return f"Ticket {ticket_id} is now {status}."
