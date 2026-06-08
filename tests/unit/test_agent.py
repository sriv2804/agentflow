import sys
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

sys.modules.setdefault("src.core.channel", MagicMock())
sys.modules.setdefault("src.core.flow", MagicMock())
sys.modules.setdefault("src.tools.common", MagicMock())


def make_agent(name="agent_a"):
    with (
        patch("src.core.agent.LLM"),
        patch("src.core.agent.PromptReader"),
    ):
        from src.core.agent import Agent
        return Agent(
            agent_name=name,
            model_name="test-model",
            tools=[],
            execution_prompt_path=Path("/fake/prompt.txt"),
            resolver="user",
        )


def test_rshift_registers_under_default():
    agent_a = make_agent("agent_a")
    agent_b = make_agent("agent_b")
    agent_a >> agent_b
    assert agent_a.successors["default"] is agent_b


def test_sub_rshift_registers_under_named_action():
    agent_a = make_agent("agent_a")
    agent_b = make_agent("agent_b")
    agent_a - "summarize" >> agent_b
    assert agent_a.successors["summarize"] is agent_b


def test_conditional_transition_rshift_returns_dest_agent():
    agent_a = make_agent("agent_a")
    agent_b = make_agent("agent_b")
    result = agent_a - "summarize" >> agent_b
    assert result is agent_b


def test_invalid_action_type_raises_type_error():
    agent_a = make_agent("agent_a")
    with pytest.raises(TypeError):
        agent_a - 123


def test_parse_llm_output_returns_valid_structure():
    agent = make_agent()
    result = agent.parse_llm_output("some llm response")
    assert "runtime_state" in result
    rs = result["runtime_state"]
    assert rs["should_yield"] is False
    assert rs["yield_action"] is None
    assert rs["yield_output"] is None
    assert rs["pending_tool_call"] is False
    assert rs["tool_call"] is None
    assert rs["needs_clarification"] is False
    assert rs["clarification"] is None
    assert rs["irrecoverable_error"] is False
    assert rs["error_ctx"] is None
