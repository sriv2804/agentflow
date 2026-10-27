"""
Session lifecycle: the client's stream always ends (done / error / shutdown),
history is saved per turn and on exit, and shutdown drains active sessions.

Uses the real AsyncChannel, AsyncAgentManager (agent thread), routes and Agent
loop with a scripted fake LLM; no network, no model.
"""
import asyncio
import concurrent.futures
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CONFIG = """
flow:
  name: "test_flow"
server:
  host: "127.0.0.1"
  port: 8000
llm:
  backend: "ollama"
  model_name: "fake-model"
storage:
  working_dir: ".agentflow"
"""


def _purge_src():
    return {k: sys.modules.pop(k) for k in list(sys.modules) if k.split(".")[0] in ("src", "examples")}


@pytest.fixture
def af(tmp_path, monkeypatch):
    """
    Fresh, real agentflow modules in an isolated working dir.

    tests/unit/test_agent.py replaces some src modules with MagicMocks in
    sys.modules at collection time; this fixture imports clean copies for the
    duration of each test and restores the previous modules afterwards.
    """
    saved = _purge_src()
    for name in ("AGENTFLOW_MODEL_BACKEND", "AGENTFLOW_MODEL_NAME", "AGENTFLOW_MODEL_BASE_URL",
                 "AGENTFLOW_AUTH_TOKEN", "AGENTFLOW_SHUTDOWN_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)  # keep the repo's .env out
    (tmp_path / "config.yaml").write_text(CONFIG)
    (tmp_path / "prompt.md").write_text("You are a test agent.")
    monkeypatch.setenv("AGENTFLOW_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.chdir(tmp_path)

    from src.core import agent as agent_mod
    from src.runtime import run_context

    recall = MagicMock(append=AsyncMock())
    monkeypatch.setattr(agent_mod, "SkillStore", MagicMock())
    monkeypatch.setattr(agent_mod, "FactStore", MagicMock())
    monkeypatch.setattr(agent_mod, "RecallStore", lambda *a, **k: recall)

    ns = SimpleNamespace(tmp=tmp_path, agent_mod=agent_mod, run_context=run_context, prompts=[])

    def use_flow(builder):
        monkeypatch.setattr(run_context, "get_flow", lambda name: builder())
    ns.use_flow = use_flow

    yield ns

    _purge_src()
    sys.modules.update(saved)


# ---------- helpers ----------

def clarify(question):
    return json.dumps({"summary": f"asking: {question}", "action": "clarification", "query": question})


def yield_to(action, output):
    return json.dumps({"summary": f"yield {action}", "action": "yield", "yield_action": action, "yield_output": output})


def make_agent(af, name, script, resolver="user"):
    """Real Agent whose LLM returns the scripted items (an exception item is raised)."""
    script = list(script)

    async def invoke(prompt):
        af.prompts.append((name, prompt))
        item = script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    with patch.object(af.agent_mod, "LLM") as llm_cls:
        llm_cls.return_value.invoke = invoke
        return af.agent_mod.Agent(
            agent_name=name, tool_grps=[], always_on_tools=[],
            execution_prompt_path=Path("prompt.md"), resolver=resolver,
        )


def flow_of(*agents):
    from src.core.flow import AgentsFlow, FlowContext
    flow = AgentsFlow(start_agent=agents[0], list_of_agents=list(agents))
    return flow, FlowContext(flow=flow, flow_description="test flow")


class FakeFlow:
    """Minimal flow object for run_on_channel: start_agent.agent_name + async run()."""

    def __init__(self, run):
        self.start_agent = SimpleNamespace(agent_name="fake")
        self.run = run


def history_path(af, session_id, agent):
    return af.tmp / ".agentflow" / session_id / f"conversation_history_{agent}.json"


def read_history(af, session_id, agent):
    return json.loads(history_path(af, session_id, agent).read_text())


def read_status(af, session_id, expected, timeout=3.0):
    """
    Status is written in run_on_channel's cleanup, i.e. after the final message
    is sent, so wait (briefly) for the expected value before returning it.
    """
    path = af.tmp / ".agentflow" / session_id / "session_status.json"

    def current():
        return json.loads(path.read_text())["status"] if path.exists() else None
    wait_until(lambda: current() == expected, timeout)
    return current()


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


async def async_wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def sse_events(body: str):
    return [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]


def stream(client, session_id, timeout=10):
    """GET the SSE stream; the TestClient returns once the stream has ended."""
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        start = time.monotonic()
        response = pool.submit(client.get, f"/chats/{session_id}/output/stream").result(timeout=timeout)
        return sse_events(response.text), time.monotonic() - start


@pytest.fixture
def server(af):
    from fastapi.testclient import TestClient
    from src.sse_server.app import app, agent_manager, session_manager
    import src.sse_server.routes  # noqa: F401  registers the routes
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, app=app, agent_manager=agent_manager,
                              session_manager=session_manager)


def start_chat(srv, text, session_id=None):
    response = srv.client.post("/chats", json={"session_id": session_id} if session_id else {})
    assert response.status_code == 200, response.text
    sid = response.json()["session_id"]
    assert srv.client.post(f"/chats/{sid}/input", json={"text": text}).status_code == 200
    return sid


# ---------- SSE server: every ending path closes the stream ----------

def test_llm_error_ends_stream_with_error_and_saves(af, server):
    from src.utils.llm import LLMError

    def build():
        lead = make_agent(af, "lead", [yield_to("work", "do the work")])
        worker = make_agent(af, "worker", [LLMError("upstream 500")], resolver="lead")
        lead - "work" >> worker
        return flow_of(lead, worker)
    af.use_flow(build)

    sid = start_chat(server, "hello")
    events, elapsed = stream(server.client, sid)

    assert [e["message_type"] for e in events] == ["info", "error"]
    assert events[-1]["content"] == "LLMError: flow failed"   # type only: no provider details
    assert elapsed < 2
    assert read_history(af, sid, "lead")[0]["content"] == "hello"
    assert read_history(af, sid, "worker")[0]["content"] == "do the work"
    assert read_status(af, sid, "failed") == "failed"
    assert server.session_manager.get_session(sid) is None


def test_normal_flow_ends_with_a_single_done(af, server):
    af.use_flow(lambda: flow_of(make_agent(af, "lead", [yield_to("end", "final answer")])))

    sid = start_chat(server, "hello")
    events, _ = stream(server.client, sid)

    assert [e["message_type"] for e in events] == ["info", "done"]
    assert events[-1]["content"] == "final answer"
    assert read_status(af, sid, "completed") == "completed"
    assert server.session_manager.get_session(sid) is None


def test_task_cancelled_right_after_creation_ends_with_shutdown(af, server):
    af.use_flow(lambda: flow_of(make_agent(af, "lead", [])))
    sid = server.client.post("/chats", json={}).json()["session_id"]

    server.agent_manager.cancel(sid).result(timeout=5)   # still waiting for its first message
    events, elapsed = stream(server.client, sid)

    assert [e["message_type"] for e in events] == ["shutdown"]
    assert elapsed < 2
    assert server.session_manager.get_session(sid) is None


# ---------- agent thread + channel, no HTTP ----------

def run_session(af, flow_builder, query="go", max_q_size=1000):
    """Start a session on a real AsyncAgentManager; returns (manager, channel, session_id)."""
    from src.runtime.channel import AsyncChannel
    from src.runtime.run_context import AgentRunContext
    from src.runtime.scheduler import AsyncAgentManager

    af.use_flow(flow_builder)
    manager = AsyncAgentManager()
    manager.run()
    channel = AsyncChannel(max_q_size=max_q_size)
    channel.client_loop = asyncio.get_running_loop()
    session_id = f"s-{time.monotonic_ns()}"
    manager.submit(AgentRunContext(session_id=session_id, channel=channel, flow_name="x")).result(timeout=5)
    return manager, channel, session_id


async def finished(manager, session_id, timeout):
    deadline = time.monotonic() + timeout
    while session_id in manager.registry:
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


def test_full_queue_without_reader_fails_the_flow_and_saves(af):
    from src.core.memory import MemoryManager
    from src.runtime.channel import ChannelClosed

    async def main():
        async def flood(session_ctx, flow_ctx, initial_edge):
            mm = MemoryManager(history_path=history_path(af, session_ctx.session_id, "fake"))
            mm.append_msg("user", initial_edge.data)
            session_ctx.get_agent("fake").memory_manager = mm
            history_path(af, session_ctx.session_id, "fake").parent.mkdir(parents=True, exist_ok=True)
            for i in range(10):   # queue holds 5; nobody reads
                await session_ctx.channel.send_to_client({"message_type": "info", "content": str(i)})

        manager, channel, sid = run_session(af, lambda: (FakeFlow(flood), MagicMock()), max_q_size=5)
        start = time.monotonic()
        await channel.send_to_agent("go")

        assert await finished(manager, sid, timeout=10)
        elapsed = time.monotonic() - start
        assert 4.5 < elapsed < 7      # one 5s put timeout, then the flow ends
        assert read_history(af, sid, "fake") == [{"role": "user", "content": "go"}]
        assert read_status(af, sid, "failed") == "failed"

        received = [await channel.receive_from_agent() for _ in range(5)]
        assert [m["content"] for m in received] == ["0", "1", "2", "3", "4"]
        with pytest.raises(ChannelClosed) as closed:
            await channel.receive_from_agent()
        assert closed.value.reason == "error"

    asyncio.run(main())


def test_response_send_failure_ends_the_flow_instead_of_deadlocking(af):
    from src.runtime.channel import ChannelClosed

    async def main():
        manager, channel, sid = run_session(
            af, lambda: flow_of(make_agent(af, "lead", [clarify("which angle?")]))
        )
        real_send = channel.send_to_client

        async def failing_send(msg, timeout=5):
            if msg.get("message_type") == "response":
                raise ChannelClosed("error", "client not reading: queue full for 5s")
            return await real_send(msg, timeout)
        channel.send_to_client = failing_send

        await channel.send_to_agent("go")
        assert await finished(manager, sid, timeout=2)   # old behaviour: waits for an answer forever

        messages = []
        with pytest.raises(ChannelClosed) as closed:
            while True:
                messages.append(await asyncio.wait_for(channel.receive_from_agent(), 1))
        assert closed.value.reason == "error"
        assert read_status(af, sid, "failed") == "failed"

    asyncio.run(main())


def test_history_is_saved_after_each_turn(af):
    async def main():
        manager, channel, sid = run_session(
            af, lambda: flow_of(make_agent(af, "lead", [clarify("q1"), clarify("q2"), clarify("q3")])),
            query="start",
        )
        await channel.send_to_agent("start")
        answered = 0
        while answered < 2:
            msg = await asyncio.wait_for(channel.receive_from_agent(), 5)
            if msg["message_type"] == "response":
                if msg["content"] == "q2":
                    break
                await channel.send_to_agent("a1")
                answered += 1

        # paused on the second question: nothing has ended, yet both turns are on disk
        # (the save runs right after the question is sent, so give it a moment;
        # wait without blocking this loop, the agent's send completes through it)
        assert await async_wait_until(lambda: len(read_history(af, sid, "lead")) == 4)
        history = read_history(af, sid, "lead")
        assert [m["content"] for m in history] == ["start", "[to user] q1", "a1", "[to user] q2"]
        manager.cancel(sid).result(timeout=5)

    asyncio.run(main())


def test_history_save_failure_in_finally_still_closes_the_stream(af, monkeypatch):
    from src.runtime.channel import ChannelClosed

    async def broken_save(session_ctx):
        raise OSError("disk full")
    monkeypatch.setattr(af.run_context, "save_session_history", broken_save)

    async def quiet(session_ctx, flow_ctx, initial_edge):
        return None   # ends without sending "done"

    async def main():
        manager, channel, sid = run_session(af, lambda: (FakeFlow(quiet), MagicMock()))
        await channel.send_to_agent("go")
        with pytest.raises(ChannelClosed) as closed:
            await asyncio.wait_for(channel.receive_from_agent(), 2)
        assert closed.value.reason == "done"
        assert read_status(af, sid, "completed") == "completed"   # the next cleanup step still ran

    asyncio.run(main())


# ---------- AsyncChannel.close ----------

def test_close_from_another_thread_wakes_a_blocked_reader(af):
    from src.runtime.channel import AsyncChannel, ChannelClosed

    async def main():
        channel = AsyncChannel()
        channel.client_loop = asyncio.get_running_loop()
        reader = asyncio.ensure_future(channel.receive_from_agent())
        await asyncio.sleep(0.05)

        start = time.monotonic()
        threading.Thread(target=channel.close, args=("error", "boom")).start()
        with pytest.raises(ChannelClosed) as closed:
            await reader
        assert time.monotonic() - start < 0.1
        assert (closed.value.reason, closed.value.message) == ("error", "boom")

    asyncio.run(main())


def test_close_on_a_full_queue_delivers_buffered_messages_first(af):
    from src.runtime.channel import AsyncChannel, ChannelClosed

    async def main():
        channel = AsyncChannel(max_q_size=3)
        channel.client_loop = asyncio.get_running_loop()
        for i in range(3):
            channel.client_in_q.put_nowait({"n": i})
        threading.Thread(target=channel.close, args=("done",)).start()
        await asyncio.sleep(0.05)

        assert [(await channel.receive_from_agent())["n"] for _ in range(3)] == [0, 1, 2]
        with pytest.raises(ChannelClosed) as closed:
            await channel.receive_from_agent()
        assert closed.value.reason == "done"

    asyncio.run(main())


def test_close_twice_or_without_a_running_client_loop_is_harmless(af):
    from src.runtime.channel import AsyncChannel, ChannelClosed

    AsyncChannel().close("done")                   # no client loop at all

    stopped = asyncio.new_event_loop()
    channel = AsyncChannel()
    channel.client_loop = stopped
    channel.close("done")                          # loop exists but isn't running
    stopped.close()
    channel.close("error")                         # loop closed

    async def main():
        channel = AsyncChannel()
        channel.client_loop = asyncio.get_running_loop()
        channel.close("shutdown")
        channel.close("error", "second close is ignored")
        await asyncio.sleep(0.05)
        for _ in range(2):                         # stays closed, never blocks
            with pytest.raises(ChannelClosed) as closed:
                await asyncio.wait_for(channel.receive_from_agent(), 0.5)
            assert closed.value.reason == "shutdown"

    asyncio.run(main())


# ---------- graceful shutdown ----------

def test_shutdown_drains_active_sessions_and_they_can_be_resumed(af, server):
    from src.sse_server.server import drain_sessions

    builds = []

    def build():
        # a fresh flow per session: the first two sessions pause on a question,
        # the resumed one answers
        builds.append(1)
        script = [clarify("which angle?")] if len(builds) <= 2 else [yield_to("end", "resumed answer")]
        return flow_of(make_agent(af, "lead", script))
    af.use_flow(build)

    sids = [start_chat(server, f"compare tool {i}") for i in (1, 2)]
    assert wait_until(lambda: all(
        history_path(af, sid, "lead").exists()
        and any(m["content"] == "[to user] which angle?" for m in read_history(af, sid, "lead"))
        for sid in sids
    ))

    server.app.state.shutting_down = True          # what AgentflowServer.handle_exit sets
    server.client.portal.call(drain_sessions, 5.0)

    for sid in sids:
        events, elapsed = stream(server.client, sid)
        assert events[-1]["message_type"] == "shutdown"
        assert elapsed < 2
        assert read_status(af, sid, "interrupted") == "interrupted"
        assert [m["content"] for m in read_history(af, sid, "lead")] == [
            f"compare tool {sids.index(sid) + 1}", "[to user] which angle?"]

    rejected = server.client.post("/chats", json={})
    assert rejected.status_code == 503
    assert "retry-after" in rejected.headers

    # an interrupted session resumes from its saved turns
    server.app.state.shutting_down = False
    af.prompts.clear()
    resumed = start_chat(server, "features and pricing", session_id=sids[0])
    assert resumed == sids[0]
    events, _ = stream(server.client, resumed)
    assert (events[-1]["message_type"], events[-1]["content"]) == ("done", "resumed answer")
    first_prompt = af.prompts[0][1]
    assert "compare tool 1" in first_prompt and "which angle?" in first_prompt
    assert read_status(af, resumed, "completed") == "completed"


# ---------- recall store ids ----------

class FakeCollection:
    """The slice of a ChromaDB collection RecallStore uses; add() rejects duplicate ids like Chroma."""

    def __init__(self):
        self.rows = {}

    def add(self, ids, documents, metadatas):
        for i, d, m in zip(ids, documents, metadatas):
            assert i not in self.rows, f"duplicate recall id {i}"
            self.rows[i] = (d, m)

    def upsert(self, ids, documents, metadatas):   # what the old code used: overwrites silently
        for i, d, m in zip(ids, documents, metadatas):
            self.rows[i] = (d, m)

    def get(self):
        return {"ids": list(self.rows), "documents": [d for d, _ in self.rows.values()],
                "metadatas": [m for _, m in self.rows.values()]}

    def count(self):
        return len(self.rows)


def test_recall_entries_survive_repeated_and_resumed_executions(af, monkeypatch):
    from src.core.recall_store import RecallStore
    from src.core.session import AgentContext

    store = RecallStore.__new__(RecallStore)
    store.agent_name = "worker"
    store.collection = FakeCollection()
    monkeypatch.setattr(af.agent_mod, "RecallStore", lambda *a, **k: store)

    def execute(agent, agent_ctx, data):
        session = MagicMock(session_id="s1")
        session.channel.send_to_client = AsyncMock()
        from src.core.flow import Edge, FlowContext
        flow_ctx = FlowContext.__new__(FlowContext)
        flow_ctx.flow_description, flow_ctx.agents_description = "", {}
        asyncio.run(agent.execute(session, agent_ctx, flow_ctx, Edge(callee="lead", call_to="worker", data=data)))

    worker = make_agent(af, "worker", [yield_to("lead", "result 1"), yield_to("lead", "result 2")], resolver="lead")
    worker.successors["lead"] = MagicMock(agent_name="lead")
    ctx = AgentContext(agent_id="worker")
    execute(worker, ctx, "task 1")
    execute(worker, ctx, "task 2")            # same agent, same session: used to overwrite turns 0..n

    resumed = make_agent(af, "worker", [yield_to("lead", "result 3")], resolver="lead")
    resumed.successors["lead"] = MagicMock(agent_name="lead")
    execute(resumed, AgentContext(agent_id="worker"), "task 3")   # new process: counters restart

    assert store.collection.count() == 6      # input + LLM response per execution, none overwritten
    recent = asyncio.run(store.get_recent(n=10))
    inputs = [r["content"] for r in recent if r["role"] == "lead"]
    assert inputs == ["task 1", "task 2", "task 3"]
    assert [r["role"] for r in recent] == ["lead", "agent"] * 3
