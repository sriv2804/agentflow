import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.flow import Edge, FlowContext
from src.core.session import AgentContext


@pytest.fixture(autouse=True)
def fake_stores(tmp_path, monkeypatch):
    # stores are bound on the first execute(), so keep them patched for the whole test;
    # run from tmp so history files and prompt logs don't touch the repo
    monkeypatch.setattr("src.core.agent.SkillStore", MagicMock())
    monkeypatch.setattr("src.core.agent.FactStore", MagicMock())
    monkeypatch.setattr("src.core.agent.RecallStore", lambda *a, **k: MagicMock(append=AsyncMock()))
    (tmp_path / "config.yaml").write_text("storage:\n  working_dir: .agentflow\n")
    monkeypatch.chdir(tmp_path)


def make_agent(name, llm_responses):
    with (
        patch("src.core.agent.LLM") as llm_cls,
        patch("src.core.agent.PromptReader") as reader,
    ):
        reader.read_prompt.return_value = "{conversation_history}\n{scratchpad}"
        reader.read_execution_prompt.return_value = ""
        llm_cls.return_value.invoke = AsyncMock(side_effect=llm_responses)
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


def run(agent, agent_ctx, callee, data, session_id="s1"):
    session = MagicMock(session_id=session_id)
    session.channel.send_to_client = AsyncMock()
    flow_ctx = FlowContext.__new__(FlowContext)
    flow_ctx.flow_description = ""
    flow_ctx.agents_description = {}
    return asyncio.run(agent.execute(session, agent_ctx, flow_ctx, Edge(callee=callee, call_to=agent.agent_name, data=data)))


def test_trail_is_cleared_between_handoffs():
    worker = make_agent("researcher", [
        yield_json("finished tool A research", "orchestrator", "findings A"),
        yield_json("finished tool B research", "orchestrator", "findings B"),
    ])
    worker.successors["orchestrator"] = MagicMock(agent_name="orchestrator")
    ctx = AgentContext(agent_id="researcher")

    run(worker, ctx, "orchestrator", "research tool A")
    run(worker, ctx, "orchestrator", "research tool B")

    second_prompt = worker.llm.invoke.call_args_list[1].args[0]
    assert "finished tool A research" not in second_prompt


def test_yield_is_recorded_in_own_history():
    manager = make_agent("orchestrator", [yield_json("delegate", "research", "research tool A")])
    manager.successors["research"] = MagicMock(agent_name="researcher")
    ctx = AgentContext(agent_id="orchestrator")

    edge = run(manager, ctx, "user", "compare A vs B")

    assert edge.call_to == "research"
    assert ctx.memory_manager.conversation_history[-1] == {
        "role": "orchestrator", "content": "[to researcher] research tool A"
    }


def test_recall_store_uses_real_sender_role():
    manager = make_agent("orchestrator", [yield_json("done", "end", "answer")])

    run(manager, AgentContext(agent_id="orchestrator"), "researcher", "findings A")

    first_append = manager.recall_store.append.call_args_list[0].kwargs
    assert first_append["role"] == "researcher"


def test_storage_is_scoped_to_runtime_session_id():
    manager = make_agent("orchestrator", [yield_json("done", "end", "answer")])
    ctx = AgentContext(agent_id="orchestrator")

    run(manager, ctx, "user", "hi", session_id="chat-123")

    assert manager.session_id == "chat-123"
    assert ctx.memory_manager.history_path == Path(".agentflow/chat-123/conversation_history_orchestrator.json")


def end_session(agent_ctx):
    # what run_on_channel does when the session ends
    mm = agent_ctx.memory_manager
    mm.save_history(mm.history_path)


def test_resumed_session_restores_conversation_history():
    first = make_agent("orchestrator", [yield_json("done", "end", "first answer")])
    first_ctx = AgentContext(agent_id="orchestrator")
    run(first, first_ctx, "user", "first question", session_id="chat-1")
    end_session(first_ctx)

    # a fresh process: new agent objects, new contexts, same session id
    resumed = make_agent("orchestrator", [yield_json("done", "end", "second answer")])
    ctx = AgentContext(agent_id="orchestrator")
    run(resumed, ctx, "user", "second question", session_id="chat-1")

    contents = [m["content"] for m in ctx.memory_manager.conversation_history]
    assert contents == ["first question", "[to user] first answer", "second question", "[to user] second answer"]
    prompt = resumed.llm.invoke.call_args.args[0]
    assert "first question" in prompt


def test_new_session_starts_with_empty_history():
    first = make_agent("orchestrator", [yield_json("done", "end", "first answer")])
    first_ctx = AgentContext(agent_id="orchestrator")
    run(first, first_ctx, "user", "first question", session_id="chat-1")
    end_session(first_ctx)

    other = make_agent("orchestrator", [yield_json("done", "end", "x")])
    ctx = AgentContext(agent_id="orchestrator")
    run(other, ctx, "user", "unrelated", session_id="chat-2")

    assert ctx.memory_manager.conversation_history[0]["content"] == "unrelated"


def fake_run_ctx(flow_run):
    from src.runtime.run_context import AgentRunContext
    channel = MagicMock()
    channel.receive_from_client = AsyncMock(return_value="hi")
    flow = MagicMock(run=flow_run)
    flow.start_agent.agent_name = "orchestrator"
    return AgentRunContext(session_id="chat-9", channel=channel, flow_name="x"), flow


def session_with_history(session_ctx, path):
    from src.core.memory import MemoryManager
    mm = MemoryManager(history_path=path)
    mm.append_msg("user", "hi")
    session_ctx.get_agent("orchestrator").memory_manager = mm


def test_run_on_channel_saves_history_when_flow_ends(tmp_path):
    from src.runtime import run_context
    path = tmp_path / "history.json"

    async def flow_run(session_ctx, flow_ctx, initial_edge):
        session_with_history(session_ctx, path)

    run_ctx, flow = fake_run_ctx(flow_run)
    with patch.object(run_context, "get_flow", return_value=(flow, MagicMock())):
        asyncio.run(run_context.run_on_channel(run_ctx))

    assert json.loads(path.read_text()) == [{"role": "user", "content": "hi"}]


def test_run_on_channel_saves_history_when_cancelled(tmp_path):
    from src.runtime import run_context
    path = tmp_path / "history.json"

    async def flow_run(session_ctx, flow_ctx, initial_edge):
        session_with_history(session_ctx, path)
        await asyncio.sleep(3600)  # e.g. waiting on the user

    async def cancel_mid_flow():
        task = asyncio.create_task(run_context.run_on_channel(run_ctx))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run_ctx, flow = fake_run_ctx(flow_run)
    with patch.object(run_context, "get_flow", return_value=(flow, MagicMock())):
        asyncio.run(cancel_mid_flow())

    assert path.exists()
