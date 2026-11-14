import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from openai import InternalServerError

from src.core.flow import AgentsFlow, Edge, FlowContext
from src.core.session import SessionContext
from src.core.trace import NOOP_TRACER, NoopTracer, RecordingTracer, Tracer, safe_emit
from src.tools.common import tool


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.agent.SkillStore", MagicMock())
    monkeypatch.setattr("src.core.agent.FactStore", MagicMock())
    monkeypatch.setattr("src.core.agent.RecallStore", lambda *a, **k: MagicMock(append=AsyncMock()))
    (tmp_path / "config.yaml").write_text("storage:\n  working_dir: .agentflow\n")
    monkeypatch.setenv("AGENTFLOW_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.chdir(tmp_path)


class FakeLLM:
    """Scripted responses; reports token usage the way the real clients do."""

    def __init__(self, responses, usage=(100, 20)):
        self.responses = list(responses)
        self.usage = usage
        self.last_usage = None
        self.last_retries = 0

    async def invoke(self, prompt):
        self.last_usage = {"tokens_in": self.usage[0], "tokens_out": self.usage[1]}
        return self.responses.pop(0)


@tool(name="lookup", description="look something up")
async def lookup(key: str, _ctx=None):
    if key == "bad":
        raise ValueError("no such key")
    return f"value of {key}"


def make_agent(name, responses, resolver="user", tools=()):
    with patch("src.core.agent.LLM"), patch("src.core.agent.PromptReader") as reader:
        reader.read_prompt.return_value = "{conversation_history}\n{scratchpad}"
        reader.read_execution_prompt.return_value = ""
        from src.core.agent import Agent
        agent = Agent(agent_name=name, model_name="test-model", tool_grps=[], always_on_tools=list(tools),
                      execution_prompt_path=Path("/fake/prompt.md"), resolver=resolver)
    agent.llm = FakeLLM(responses)
    return agent


def j(**fields):
    return json.dumps(fields)


def fake_channel(replies=()):
    channel = MagicMock()
    channel.send_to_client = AsyncMock()
    channel.receive_from_client = AsyncMock(side_effect=list(replies))
    return channel


def run_flow(agents, start, channel, tracer=None, prompt="hello"):
    flow = AgentsFlow(start_agent=start, list_of_agents=agents)
    flow_ctx = FlowContext(flow)
    session = SessionContext(session_id="s1", channel=channel, logger=None, workspace_dir=Path("."),
                             tracer=tracer)
    asyncio.run(flow.run(session, flow_ctx, Edge(callee="user", call_to=start.agent_name, data=prompt)))
    return session


def two_agent_flow():
    orchestrator = make_agent("orchestrator", [
        j(summary="look it up", action="tool_call", tool_name="lookup", args={"key": "a"}),
        j(summary="bad lookup", action="tool_call", tool_name="lookup", args={"key": "bad"}),
        j(summary="need the order", action="clarification", query="Which order?"),
        j(summary="delegate", action="yield", yield_action="research", yield_output="research order 7"),
    ], tools=[lookup])
    worker = make_agent("researcher", [
        j(summary="done", action="yield", yield_action="end", yield_output="the answer"),
    ])
    orchestrator - "research" >> worker
    return orchestrator, worker


def test_run_emits_expected_event_sequence():
    orchestrator, worker = two_agent_flow()
    tracer = RecordingTracer()
    run_flow([orchestrator, worker], orchestrator, fake_channel(["order 7"]), tracer)

    events = tracer.events
    assert [e["type"] for e in events] == [
        "handoff",                      # user -> orchestrator
        "llm_call", "tool_call",        # lookup a
        "llm_call", "tool_call",        # lookup bad (tool error)
        "llm_call", "clarification", "user_message",
        "llm_call", "handoff",          # orchestrator -> researcher
        "llm_call", "final", "handoff",  # researcher ends -> user
    ]
    assert [e["index"] for e in events] == list(range(len(events)))
    handoffs = [(e["from_agent"], e["action"], e["to_agent"]) for e in events if e["type"] == "handoff"]
    assert handoffs == [("user", None, "orchestrator"), ("orchestrator", "research", "researcher"),
                        ("researcher", "end", "user")]
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert tool_calls[0]["output"] == "value of a" and tool_calls[0]["error"] is None
    assert tool_calls[1]["error"] == "no such key" and tool_calls[1]["args"] == {"key": "bad"}
    clar = next(e for e in events if e["type"] == "clarification")
    assert clar["agent"] == "orchestrator" and clar["to"] == "user" and clar["query"] == "Which order?"
    assert next(e for e in events if e["type"] == "user_message")["content"] == "order 7"
    final = next(e for e in events if e["type"] == "final")
    assert final["agent"] == "researcher" and final["output"] == "the answer"
    first_llm = events[1]
    assert first_llm["agent"] == "orchestrator" and first_llm["summary"] == "look it up"
    assert first_llm["action"] == "tool_call" and first_llm["parse_ok"] is True


def test_llm_call_carries_token_usage_and_retries():
    agent = make_agent("solo", [j(summary="s", action="yield", yield_action="end", yield_output="x")])
    agent.llm.usage = (321, 45)
    agent.llm.last_retries = 2
    tracer = RecordingTracer()
    run_flow([agent], agent, fake_channel(), tracer)
    call = next(e for e in tracer.events if e["type"] == "llm_call")
    assert (call["tokens_in"], call["tokens_out"], call["retries"]) == (321, 45, 2)
    assert call["latency_s"] >= 0


def test_parse_failures_and_irrecoverable_error_are_traced():
    agent = make_agent("solo", ["not json"] * 4)
    tracer = RecordingTracer()
    channel = fake_channel()
    run_flow([agent], agent, channel, tracer)
    types = [e["type"] for e in tracer.events]
    assert types == ["handoff"] + ["llm_call"] * 4 + ["error", "handoff"]
    assert all(e["parse_ok"] is False and e["error"] for e in tracer.events if e["type"] == "llm_call")
    assert "Failed to parse" in tracer.events[-2]["error"]
    # the client still only sees a normal "done": the trace is the only signal
    assert channel.send_to_client.call_args.args[0]["message_type"] == "done"


def test_llm_exception_is_traced_then_propagates():
    agent = make_agent("solo", [])
    agent.llm.invoke = AsyncMock(side_effect=RuntimeError("provider down"))
    tracer = RecordingTracer()
    with pytest.raises(RuntimeError):
        run_flow([agent], agent, fake_channel(), tracer)
    call = tracer.events[-1]
    assert call["type"] == "llm_call" and call["parse_ok"] is False and "provider down" in call["error"]


def test_run_on_channel_passes_tracer_and_traces_first_message():
    from src.runtime import run_context
    from src.runtime.run_context import AgentRunContext
    agent = make_agent("solo", [j(summary="s", action="yield", yield_action="end", yield_output="x")])
    flow = AgentsFlow(start_agent=agent, list_of_agents=[agent])
    tracer = RecordingTracer()
    channel = fake_channel(["first question"])
    run_ctx = AgentRunContext(session_id="s9", channel=channel, flow_name="x", tracer=tracer)
    with patch.object(run_context, "get_flow", return_value=(flow, FlowContext(flow))):
        asyncio.run(run_context.run_on_channel(run_ctx))
    assert tracer.events[0] == {"index": 0, "t": tracer.events[0]["t"], "type": "user_message",
                                "content": "first question", "initial": True}
    assert tracer.events[-1]["type"] == "handoff"


# ---- NoopTracer path ----

def test_session_defaults_to_noop_tracer():
    session = SessionContext(session_id="s", channel=MagicMock(), logger=None, workspace_dir=Path("."))
    assert session.tracer is NOOP_TRACER and isinstance(session.tracer, NoopTracer)
    from src.runtime.run_context import AgentRunContext
    assert AgentRunContext(session_id="s", channel=MagicMock()).tracer is None


def test_noop_path_sends_the_same_client_messages():
    def messages(tracer):
        orchestrator, worker = two_agent_flow()
        channel = fake_channel(["order 7"])
        run_flow([orchestrator, worker], orchestrator, channel, tracer)
        return [c.args[0] for c in channel.send_to_client.call_args_list]

    assert messages(None) == messages(RecordingTracer())


def test_faulty_tracer_never_breaks_a_run():
    class Broken(Tracer):
        def emit(self, event_type, **fields):
            raise RuntimeError("sink down")

    agent = make_agent("solo", [j(summary="s", action="yield", yield_action="end", yield_output="x")])
    channel = fake_channel()
    run_flow([agent], agent, channel, Broken())
    assert channel.send_to_client.call_args.args[0] == {"message_type": "done", "agent": "solo", "content": "x"}
    safe_emit(None, "x")  # no tracer at all is fine too


# ---- token usage and temperature in the clients ----

def openai_response(text="hi", usage=(12, 3)):
    u = SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]) if usage else None
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))], usage=u)


def openai_client(monkeypatch, **kw):
    from src.utils.llm import OpenAIClient
    monkeypatch.setenv("AGENTFLOW_API_KEY", "test-key")
    client = OpenAIClient("m", base_url="http://localhost:1/v1", **kw)
    client._client = MagicMock()
    client._client.chat.completions.create = AsyncMock(return_value=openai_response())
    return client


def test_openai_client_reads_usage_and_omits_temperature_by_default(monkeypatch):
    client = openai_client(monkeypatch)
    assert asyncio.run(client.invoke("p")) == "hi"
    assert client.last_usage == {"tokens_in": 12, "tokens_out": 3} and client.last_retries == 0
    assert "temperature" not in client._client.chat.completions.create.call_args.kwargs


def test_openai_client_temperature_and_missing_usage(monkeypatch):
    client = openai_client(monkeypatch, temperature=0)
    client._client.chat.completions.create.return_value = openai_response(usage=None)
    asyncio.run(client.invoke("p"))
    assert client._client.chat.completions.create.call_args.kwargs["temperature"] == 0
    assert client.last_usage == {"tokens_in": None, "tokens_out": None}


def test_openai_client_counts_retries(monkeypatch):
    client = openai_client(monkeypatch)
    err = InternalServerError("busy", response=httpx.Response(503, request=httpx.Request("POST", "http://x")),
                              body=None)
    client._client.chat.completions.create.side_effect = [err, err, openai_response()]
    monkeypatch.setattr("src.utils.llm.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("src.utils.llm.retry_wait", lambda e, a: 0.0)
    asyncio.run(client.invoke("p"))
    assert client.last_retries == 2


def test_ollama_client_reads_eval_counts_and_temperature():
    from src.utils.llm import OllamaClient
    client = OllamaClient("m", temperature=0)
    client._client = MagicMock()
    client._client.chat = AsyncMock(return_value=SimpleNamespace(
        message=SimpleNamespace(content="ok"), prompt_eval_count=50, eval_count=7))
    assert asyncio.run(client.invoke("p")) == "ok"
    assert client.last_usage == {"tokens_in": 50, "tokens_out": 7}
    assert client._client.chat.call_args.kwargs["options"] == {"temperature": 0}

    default = OllamaClient("m")
    default._client = client._client
    asyncio.run(default.invoke("p"))
    assert "options" not in client._client.chat.call_args.kwargs


def test_llm_wrapper_exposes_usage_and_forwards_temperature(monkeypatch):
    from src.utils.llm import LLM
    llm = LLM("m", backend="ollama", temperature=0.0)
    assert llm.client.temperature == 0.0
    assert llm.last_usage is None and llm.last_retries == 0
    llm.client.last_usage = {"tokens_in": 1, "tokens_out": 2}
    assert llm.last_usage == {"tokens_in": 1, "tokens_out": 2}
    assert LLM("m", backend="ollama").client.temperature is None
