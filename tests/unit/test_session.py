import sys
import pytest
from unittest.mock import MagicMock
from pathlib import Path

# Stub missing/broken modules before importing session
sys.modules.setdefault("src.core.channel", MagicMock())
sys.modules.setdefault("src.core.flow", MagicMock())
sys.modules.setdefault("src.tools.common", MagicMock())

from src.core.session import SessionContext, AgentContext


def make_session(session_id="s1"):
    channel = MagicMock()
    logger = MagicMock()
    return SessionContext(
        session_id=session_id,
        channel=channel,
        logger=logger,
        workspace_dir=Path("/tmp"),
    )


def test_get_agent_creates_new_context():
    ctx = make_session()
    agent_ctx = ctx.get_agent("agent_a")
    assert isinstance(agent_ctx, AgentContext)
    assert agent_ctx.agent_id == "agent_a"


def test_get_agent_returns_same_object():
    ctx = make_session()
    first = ctx.get_agent("agent_a")
    second = ctx.get_agent("agent_a")
    assert first is second


def test_data_dict_independent_per_session():
    ctx1 = make_session("s1")
    ctx2 = make_session("s2")
    ctx1.data["key"] = "value"
    assert "key" not in ctx2.data
