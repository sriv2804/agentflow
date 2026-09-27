import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.flow import Edge, FlowContext
from src.core.session import AgentContext


def make_agent(name, llm_responses):
    with (
        patch("src.core.agent.LLM") as llm_cls,
        patch("src.core.agent.PromptReader") as reader,
        patch("src.core.agent.SkillStore"),
        patch("src.core.agent.RecallStore") as recall_cls,
        patch("src.core.agent.FactStore"),
    ):
        reader.read_prompt.return_value = "{conversation_history}\n{scratchpad}"
        reader.read_execution_prompt.return_value = ""
        llm_cls.return_value.invoke = AsyncMock(side_effect=llm_responses)
        recall_cls.return_value.append = AsyncMock()
        from src.core.agent import Agent
        return Agent(
            agent_name=name,
            model_name="test-model",
            tool_grps=[],
            always_on_tools=[],
            execution_prompt_path=Path("/fake/prompt.md"),
            resolver="orchestrator",
        )


def yield_json(summary, action, output):
    return json.dumps({"summary": summary, "action": "yield", "yield_action": action, "yield_output": output})


def run(agent, agent_ctx, callee, data):
    session = MagicMock(session_id="s1")
    session.channel.send_to_client = AsyncMock()
    flow_ctx = FlowContext.__new__(FlowContext)
    flow_ctx.flow_description = ""
    flow_ctx.agents_description = {}
    return asyncio.run(agent.execute(session, agent_ctx, flow_ctx, Edge(callee=callee, call_to=agent.agent_name, data=data)))


def test_trail_is_cleared_between_handoffs(tmp_path, monkeypatch):
    worker = make_agent("researcher", [
        yield_json("finished tool A research", "orchestrator", "findings A"),
        yield_json("finished tool B research", "orchestrator", "findings B"),
    ])
    worker.successors["orchestrator"] = MagicMock(agent_name="orchestrator")
    monkeypatch.chdir(tmp_path)  # prompt logs land in tmp
    ctx = AgentContext(agent_id="researcher")

    run(worker, ctx, "orchestrator", "research tool A")
    run(worker, ctx, "orchestrator", "research tool B")

    second_prompt = worker.llm.invoke.call_args_list[1].args[0]
    assert "finished tool A research" not in second_prompt


def test_yield_is_recorded_in_own_history(tmp_path, monkeypatch):
    manager = make_agent("orchestrator", [yield_json("delegate", "research", "research tool A")])
    manager.successors["research"] = MagicMock(agent_name="researcher")
    monkeypatch.chdir(tmp_path)
    ctx = AgentContext(agent_id="orchestrator")

    edge = run(manager, ctx, "user", "compare A vs B")

    assert edge.call_to == "research"
    assert ctx.memory_manager.conversation_history[-1] == {
        "role": "orchestrator", "content": "[to researcher] research tool A"
    }


def test_recall_store_uses_real_sender_role(tmp_path, monkeypatch):
    manager = make_agent("orchestrator", [yield_json("done", "end", "answer")])
    monkeypatch.chdir(tmp_path)

    run(manager, AgentContext(agent_id="orchestrator"), "researcher", "findings A")

    first_append = manager.recall_store.append.call_args_list[0].kwargs
    assert first_append["role"] == "researcher"
