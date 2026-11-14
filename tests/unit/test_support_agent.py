import asyncio
from types import SimpleNamespace

import pytest

from examples.support_agent.state import load_state
from examples.support_agent.tools import get_policy, initiate_refund, lookup_order, update_ticket
from src.evals.spec import load_callable
from pathlib import Path

FIXTURES_BASE = Path("examples/support_agent")


def fx(name):
    return load_callable(f"fixtures.{name}", FIXTURES_BASE)


@pytest.fixture
def session(tmp_path):
    ctx = SimpleNamespace(working_dir=str(tmp_path), storage_session_id="s1")
    return ctx, tmp_path / "s1"


def call(t, ctx, **kw):
    return asyncio.run(t(**kw, _ctx=ctx))


def test_late_order_refund_flow_updates_state(session):
    ctx, session_dir = session
    fx("seed_late_order")(session_dir)
    assert "1182" in call(lookup_order, ctx, order_id="#1182")
    assert "30 days" in call(get_policy, ctx)
    assert "5 to 7 business days" in call(initiate_refund, ctx, order_id="1182", amount="64.99")
    assert fx("ticket_status")(session_dir) == {"status": "open"}
    call(update_ticket, ctx, ticket_id="T-1182", status="resolved", note="refunded")
    assert fx("ticket_status")(session_dir) == {"status": "resolved"}
    assert load_state(session_dir)["refunds"][0]["amount"] == 64.99


def test_tool_errors(session):
    ctx, session_dir = session
    fx("seed_old_order")(session_dir)
    with pytest.raises(ValueError):
        call(lookup_order, ctx, order_id="9999")
    with pytest.raises(ValueError):
        call(update_ticket, ctx, ticket_id="T-2207", status="done")


def test_refund_within_limit(session):
    _, session_dir = session
    fx("seed_late_order")(session_dir)
    ok = fx("refund_within_limit")
    assert ok({"args": {"order_id": "1182", "amount": 64.99}, "error": None}, session_dir)
    assert not ok({"args": {"order_id": "1182", "amount": 100}, "error": None}, session_dir)   # above total
    assert not ok({"args": {"order_id": "1182", "amount": 10}, "error": "boom"}, session_dir)  # failed call
    assert not ok({"args": {"order_id": "7"}, "error": None}, session_dir)
