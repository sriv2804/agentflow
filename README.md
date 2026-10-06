# agentflow

![Python](https://img.shields.io/badge/python-3.12%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green) ![LLM](https://img.shields.io/badge/runs%20on-Ollama%20%7C%20OpenAI--compatible-orange)

**An async multi-agent framework where the framework owns the control flow — you bring a prompt and a list of tools.**

agentflow is built from scratch in Python (no LangChain, LangGraph or AutoGen). It runs the ReAct loop, routes work between agents, manages memory, pauses for human input and persists sessions, so an agent is just a markdown prompt, its tool groups and who answers its questions. The same flow runs in a terminal or behind a web frontend, because the agent runtime never touches the transport.

What it deliberately skips: native function-calling APIs (every backend speaks plain text in, one JSON action out) and per-agent orchestration code (routing, memory and human-in-the-loop are the framework's job).

---

## Getting started

You need Python 3.12+ and a [supported LLM backend](#supported-llm-backends). Set your backend and model in the `llm` block of `config.yaml`:

```yaml
llm:
  backend: "ollama"                       # or "openai" for any OpenAI-compatible endpoint
  model_name: "gemma4:26b-a4b-it-q4_K_M"
```

Then install and run:

```bash
git clone https://github.com/sriv2804/agentflow.git
cd agentflow
pip install -r requirements.txt
python -m src.tui.tui
```

With the default Ollama setup, pull the model first (`ollama pull <model_name>`). For other backends, see [Supported LLM backends](#supported-llm-backends).

This starts a session of the **competitive analysis** demo: three agents that compare two software tools for your team and write a sourced report. Type a comparison when prompted and answer the orchestrator's question:

```
Started session 81e27857-…  (flow: competitive_analysis)
What would you like to do? (Ctrl+C to quit)
> Slack vs Microsoft Teams for a remote team

[orchestrator asks] Which angles should I focus on, and what is your team size?
> features and pricing, 25-person remote startup
[researcher] → tool: web_search
[researcher] → tool: web_fetch
[report_generator] → tool: write_report

[done] … Microsoft Teams is the recommended choice for a 25-person remote startup …
Report saved to: .agentflow/81e27857-…/reports/slack_vs_microsoft_teams_competitive_analysis.md
Session id: 81e27857-…  (resume with --resume 81e27857-…)
```

A run takes roughly 10–15 minutes on local hardware with a ~26B model. Continue the conversation later with `python -m src.tui.tui --resume <session_id>`.

To run the same flow behind HTTP/SSE instead, start the server with `uvicorn src.sse_server.app:app --port 8000` (see [Reference](#reference)).

---

## Why agentflow

Most agent frameworks treat orchestration, memory, and HITL as things you wire up yourself per-agent. agentflow inverts this — the framework owns the control flow, and agents are declarative: a prompt, a set of tools, and a resolver (who handles clarifications). Everything else is the framework's problem.

Design principles:
- **Text-in, text-out LLM contract** — tools and output format are described in plain text in the prompt. The LLM returns a single JSON action object. No native function-calling APIs, so any backend (OpenAI-compatible, Ollama, future providers) works with zero changes to the agent loop.
- **Memory as a first-class concern** — distinct memory scopes, each designed for a specific job, rather than dumping everything into a vector DB. Every session gets its own storage and can be resumed later.
- **HITL falls out of the architecture** — agents pause for human input by awaiting on the async channel. No separate pause/resume state machine.
- **Agent runtime decoupled from the transport** — agents only ever talk to an `AsyncChannel`. How messages reach a human or another system is a thin adapter on the other side of the channel, so the same flow runs unchanged over a terminal UI or a web frontend (SSE today; webhooks or agent-to-agent protocols such as A2A plug in the same way).
- **On-demand tool loading** — tools are grouped; the LLM explicitly loads a group before using it. Keeps the static prompt lean and improves caching as tool count grows.

---

## Architecture Overview

```
   Terminal UI          Web frontend           Webhooks / A2A
   (src/tui)            (SSE, src/sse_server)  (same pattern, not yet built)
        │                      │                      │
        └──────────────┬───────┴──────────────────────┘
                       │  transport adapters — client side
             ┌─────────▼─────────┐
             │   AsyncChannel    │  one per session, thread-safe, turn-based
             └─────────┬─────────┘
                       │  agent side
             ┌─────────▼─────────┐
             │   Agent Runtime   │  dedicated agent event loop, one task per session
             │   (runtime/)      │
             └─────────┬─────────┘
             ┌─────────▼─────────┐
             │   AgentsFlow      │  graph runner — routes between agents
             └─────────┬─────────┘
             ┌─────────▼─────────┐
             │   Agent           │  ReAct loop per agent (core/agent.py)
             └─────────┬─────────┘
       ┌───────────┬───┴────────┬──────────────┐
       ▼           ▼            ▼              ▼
  LLM backend   Memory        HITL           Tools
  (text in/out) (per session) (via channel)  (on-demand groups)
```

---

## Multi-agent demo: competitive analysis

`examples/competitive_analysis/` is a three-agent flow that compares two software tools for a given team and writes a sourced markdown report. It exercises manager–worker routing, inter-agent handoffs, HITL clarification, tool use across agents, SkillStore save and reuse, and session resumption.

| Agent | Role | Tool groups | Resolver |
|---|---|---|---|
| `orchestrator` | Only agent that talks to the user: checks for a saved plan, asks a clarifying question, delegates, saves the plan as a skill, presents the result | `memory_tools` | `user` |
| `researcher` | One research task per handoff: 2–3 web searches, fetches full pages when snippets are vague, returns findings with source URLs | `web_tools` (`web_search`, `web_fetch`) | `orchestrator` |
| `report_generator` | Turns the findings into a structured report (TL;DR, at-a-glance table, per-tool sections, head-to-head, recommendation, numbered sources) and writes it to disk | `reporting_tools` (`write_report`) | `orchestrator` |

```python
orchestrator - "research" >> researcher
orchestrator - "generate_report" >> report_generator
researcher - "orchestrator" >> orchestrator
report_generator - "orchestrator" >> orchestrator
```

```
User ──query──▶ orchestrator ── skill check, clarifying question ──▶ User
                orchestrator ── "research" tool A ──▶ researcher ── findings ──▶ orchestrator
                orchestrator ── "research" tool B ──▶ researcher ── findings ──▶ orchestrator
                orchestrator ── "generate_report" ──▶ report_generator ── summary + path ──▶ orchestrator
                orchestrator ── save skill, "end" ──▶ User (answer + report path)
```

Run it:

```bash
python -m src.tui.tui                                   # interactive
python -m tests.scripts.competitive_analysis_e2e \
    --query "Slack vs Microsoft Teams for a remote team" \
    --answer "Focus on features and pricing. We're a 25-person remote startup."
```

The E2E script runs the flow over the real HTTP/SSE API (starting the server in-process unless `--base-url` is given) and answers clarifications from `--answer` values, then stdin. A run takes roughly 10–15 minutes with a ~26B model on local hardware. Reports are written to `.agentflow/<session_id>/reports/`.

---

## Terminal UI

`src/tui/` runs any registered flow in the terminal, without the web server. It is a second consumer of `AsyncChannel`, built from the same primitives: it creates its own channel and a daemon thread running the agent loop, while the terminal's main loop is the client side.

```bash
python -m src.tui.tui                         # new session, prompts for your query
python -m src.tui.tui --resume <session_id>   # continue a previous session
python -m src.tui.tui --flow qa_agent         # run a different registered flow
python -m src.tui.tui --query "..."           # skip the prompt (scripted runs)
```

```
Started session 3748c72e-…  (flow: competitive_analysis)
What would you like to do? (Ctrl+C to quit)
> Notion vs Coda for a startup team

[orchestrator] Step 1: Checking for any existing competitive analysis skills…
[orchestrator] → tool: load_tool_group
[orchestrator asks] Which angles should I focus on, and what is your team size?
> features and pricing, 8-person startup
[researcher] → tool: web_search
[researcher] → tool: web_fetch
[report_generator] → tool: write_report

[done] The comparison between Notion and Coda for your 8-person startup is complete…
Report saved to: .agentflow/3748c72e-…/reports/notion_vs_coda_competitive_analysis.md
Session id: 3748c72e-…  (resume with --resume 3748c72e-…)
```

- Agent output streams live with a colored prefix per agent; tool calls are shown dimmed
- Clarifications appear as a `>` prompt and the flow continues with your answer
- On exit (done, Ctrl+C, or end of input) it waits for the session to save its history, then prints the session ID

---

## Session resumption

Every chat gets its own storage directory keyed by its session ID (a UUID): `.agentflow/<session_id>/`. When a session ends gracefully — the flow finishes, errors, or is cancelled (including Ctrl+C in the TUI) — each agent's conversation history is written to that directory. Resuming the session reloads it, together with the session's skills, facts and working memory, so the agents pick up where they left off.

```bash
# TUI
python -m src.tui.tui --resume <session_id>

# SSE server: pass the session id when creating the chat
curl -X POST localhost:8000/chats -H 'Content-Type: application/json' \
     -d '{"session_id": "<session_id>"}'
```

`POST /chats` returns 400 if the id is not a UUID, 404 if no stored session exists, and 409 if that session is currently active. Without a body it starts a new session as before.

In practice, a resumed session reuses context from earlier turns: in the demo below, a resumed session answered "now compare X vs Y with the same criteria" without re-asking the user, and followed the skill saved earlier in the same session.

Limitations:
- History is saved on graceful end only; a crash or a killed process loses the unsaved history.
- Only the last `max_short_term` (20) messages per agent are kept, so long sessions remember recent exchanges, not everything.
- Skills, facts and working memory are per session: a new session starts empty.

---

## Execution Layer & Async Runtime

agentflow runs two event loops in two OS threads inside a single process. The agent loop never touches transport code; the client loop never runs agent code. `AsyncChannel` is the only thing they share.

**Thread 1 — client loop**: owned by the transport. For the SSE server this is the FastAPI/uvicorn loop (session creation, SSE streaming, user input, session deletion). For the TUI it is the terminal's own loop.

**Thread 2 — agent loop**: a dedicated asyncio event loop. In the server it is managed by `AsyncAgentManager`, and each agent session runs as an asyncio Task on it — multiple sessions run concurrently via cooperative scheduling. A `ThreadPoolExecutor(max_workers=200)` is configured as the default executor for offloading blocking calls (e.g. ChromaDB queries) via `asyncio.to_thread()`.

### AsyncChannel

The sole bridge between the two threads, one per session:

- `client_in_q` — agent → client messages (`info` progress, `response` questions, `done` final answer); each message carries the sending `agent` name
- `agent_in_q` — client → agent messages (the user's query, HITL answers)
- A `Turn` enum (`AGENT` / `CLIENT`) with a threading lock enforces turn-taking — only one side may enqueue at a time
- Cross-thread queue operations use `asyncio.run_coroutine_threadsafe` + `asyncio.wrap_future` to safely bridge the two loops

### Session lifecycle (SSE server)

1. `POST /chats` → creates a session ID (or reuses one to resume, see [Session resumption](#session-resumption)), a fresh `AsyncChannel`, and schedules the agent flow on Thread 2. The flow waits for the first message.
2. `GET /chats/{id}/output/stream` → SSE stream; yields every agent message until a `"done"` message closes the stream
3. `POST /chats/{id}/input` → sends user input to the agent; returns HTTP 400 if it's not the client's turn
4. `DELETE /chats/{id}` → cancels the agent task (its conversation history is saved), removes the session

---

## Core ReAct Loop

The Think → Act → Observe loop lives entirely in `src/core/agent.py`.

Each iteration branches on a `RuntimeState` dataclass:

```
while not should_yield and not irrecoverable_error:
    if pending_tool_call     →  Act:    execute tool, append result to scratchpad
    elif needs_clarification →  Pause:  send query to resolver (user or agent), await response
    else                     →  Think:  format prompt, call LLM, parse output
```

### Output contract

The LLM must return a single JSON object with an `"action"` field — one of:

| action          | meaning                                           |
|-----------------|---------------------------------------------------|
| `tool_call`     | invoke a tool with `tool_name` + `args`           |
| `clarification` | pause and ask the resolver a question             |
| `yield`         | hand off output to the next agent or end the flow |
| `error`         | signal an unrecoverable error                     |

### Self-correcting output parsing

On any parse failure (malformed JSON, missing fields, unknown tool name):

1. The error + the raw response are fed back into the scratchpad as a corrective message
2. The next prompt includes this under `<recent_errors>` — the LLM sees its own mistake and corrects it
3. After 3 failed attempts, the session fails gracefully with a `"done"` message explaining the error

This means the loop never silently swallows malformed output.

---

## Agent System

### Defining an agent

Agents are constructed directly in Python — no YAML or config DSL. The model comes from the `llm` block in `config.yaml` unless the agent overrides it:

```python
Agent(
    agent_name="researcher",
    tool_grps=[web_tools],
    always_on_tools=[load_tool_group],
    execution_prompt_path=Path("examples/competitive_analysis/prompts/researcher.md"),
    resolver="orchestrator",   # clarifications route to this agent
)
```

- `execution_prompt_path` — a markdown file defining the agent's role, persona, and instructions. This is the only agent-specific configuration.
- `model_name`, `model_backend` (optional) — override the `config.yaml` default for this agent only.
- `resolver` — either `"user"` (clarifications go to the human over the channel) or another agent's name (clarifications are routed to that agent by the flow).

### Wiring agents together

Agents are wired into a directed graph using operator overloads:

```python
# Default routing
orchestrator >> worker

# Named action routing — the LLM's yield_action picks the path
orchestrator - "research" >> researcher
researcher - "orchestrator" >> orchestrator   # return edge: results and clarifications
```

At runtime, `AgentsFlow` drives execution: it calls `agent.execute()`, reads the returned `Edge`, looks up `successors[yield_action]`, and advances to the next agent — passing the previous agent's output as the new agent's input. A `yield_action` of `"end"` finishes the flow and sends the final answer to the client. When an agent yields, its reasoning trail is cleared and the handoff is recorded in its own conversation history.

---

## Memory Architecture

Distinct memory scopes, each designed for a specific job. All persistent state lives in a per-session directory, so sessions are isolated from each other and can be resumed.

```
┌──────────────────────────────────────────────────────────────────┐
│                          Memory Scopes                            │
├──────────────────┬──────────────────┬────────────────────────────┤
│  Conversation    │  Task            │  Long-term                 │
│  (per agent)     │  (in-memory)     │  (persisted per session)   │
├──────────────────┼──────────────────┼────────────────────────────┤
│ rolling message  │ scratchpad trail │ SkillStore (ChromaDB)      │
│ history — saved  │ active skill     │ RecallStore (ChromaDB)     │
│ when the session │ loaded tool grps │ FactStore (ChromaDB)       │
│ ends, reloaded   │                  │ working memory (JSON)      │
│ on resume        │                  │                            │
└──────────────────┴──────────────────┴────────────────────────────┘

.agentflow/<session_id>/
    conversation_history_<agent>.json
    <agent>_working_memory.json
    chromadb/            skills_<agent>, recall_<agent>, facts_<agent>
    skills_<agent>/      skill markdown files
    reports/             files written by tools (e.g. write_report)
```

### Conversation history

`MemoryManager.conversation_history` holds each agent's rolling `{role, content}` message list, capped at `max_short_term` (default 20). It records both sides of every exchange — messages an agent receives and the handoffs it makes (`[to researcher] …`). Injected into the prompt's `<memory>` section every turn.

As history fills up, a **memory pressure system** kicks in:
- At 70% capacity → a system instruction is injected into the scratchpad telling the LLM to offload important context to `fact_write` or `working_memory_update` before continuing
- At 100% capacity → oldest 30% of history is evicted

### Task scope — scratchpad

`ScratchPad` carries the running reasoning trail within a single task:

- `trail` — every `[THOUGHT]`, `[TOOL CALL]`, `[FAILED TOOL CALL]`, and `[SYSTEM]` entry from the current task; cleared when the agent yields
- `active_skill` — the retrieved skill content, rendered as `[ACTIVE SKILL]` when a skill is loaded
- `loaded_tool_grp_str` — schemas of currently-loaded tool groups

The scratchpad is what gives the LLM continuity across ReAct iterations — it sees its own reasoning trail and tool results, not just the latest turn.

### Long-term scope

All three long-term stores use ChromaDB with cosine similarity, scoped per agent (`skills_{agent_name}`, `recall_{agent_name}`, `facts_{agent_name}`) within the session directory.

**SkillStore** — the most distinctive component. Agents distill successful multi-step task plans into reusable skills and retrieve them on demand.

- Skills are stored as markdown files on disk; only the name + description go into ChromaDB for semantic search
- Retrieval is via the `skill_retriever` tool (on-demand, not auto-injected) — the LLM decides when to look for a relevant skill
- Skill creation is LLM-driven: after a multi-step task, the LLM is instructed to check for duplicates, then save a generalisable plan as a skill
- A retrieved skill surfaces under `[ACTIVE SKILL]` in the scratchpad — a dedicated section separate from the tool call trail

This design is consistent with the procedural memory approach in the [Voyager paper](https://arxiv.org/abs/2305.16291), arrived at independently.

**RecallStore** — append-only semantic log of turns (inputs, clarification exchanges, LLM responses). Searchable via `recall_search`.

**FactStore** — curated facts explicitly written by the LLM via `fact_write`. Semantically searchable via `fact_search`. Separate from recall — these are things the agent has decided are worth keeping, not a full transcript.

**Working memory** — a single string per agent, persisted as JSON. Always injected verbatim into the prompt as a compact index the agent maintains. Updated only via `working_memory_update`.

---

## Tool System

### Defining a tool

```python
@tool(name="web_search", description="Search the web for current information on a topic")
async def web_search(query: str, _ctx: ToolContext = None) -> str:
    ...
```

The `@tool` decorator introspects type hints and docstrings to auto-build the args schema string that gets embedded in the LLM prompt. `_ctx` is a dependency-injection bag carrying all framework internals (stores, scratchpad, session info) — never exposed in the schema. Tools that need to change agent state do it through `_ctx` (e.g. `load_tool_group` loads a group into the scratchpad, `skill_retriever` sets the active skill), so the agent loop treats every tool result the same way.

### Tool groups — always-on vs on-demand

**Always-on tools** (e.g. `load_tool_group`) are available in every prompt from the first turn.

**Tool groups** bundle related tools with a shared instruction block. Only the group name + description + tool name list appear in the base prompt. Full schemas and instructions are hidden until the group is loaded:

```
LLM calls: load_tool_group("web_tools")
→ full schemas for web_search and web_fetch injected into scratchpad
→ tools are now callable
```

This keeps the static prompt prefix lean and lets the LLM load only what it needs for the current task. Up to 3 groups can be loaded simultaneously (FIFO eviction when the cap is exceeded).

---

## Supported LLM backends

agentflow's contract with a model is plain text: the framework flattens the full context (role, tools, memory, scratchpad) into one prompt string, and expects one JSON action back. There is no dependency on native function calling or provider-specific message formats, so any model that can follow instructions works with zero changes to the agent loop.

| Backend | `backend` | Notes |
|---------|-----------|-------|
| Ollama | `"ollama"` | `model_name` is any pulled Ollama model; `base_url` defaults to `http://localhost:11434` |
| OpenAI-compatible | `"openai"` | `base_url` defaults to GitHub Models; the API key is read from the env var named by `api_key_env` (default `GITHUB_TOKEN`, e.g. in `.env`) |

The `llm` block in `config.yaml` sets the default for every agent. To mix models in one flow (e.g. a larger model for the orchestrator and a cheaper one for workers), pass `model_name` / `model_backend` on individual `Agent(...)` calls. An agent that switches backend uses that backend's default endpoint.

### Adding a provider

A provider is one class with a single method, registered in `src/utils/llm.py`:

```python
class MyProviderClient(LLMClient):
    def __init__(self, model_name: str):
        self.model_name = model_name
        self._client = ...  # the provider's async SDK client

    async def invoke(self, prompt: str) -> str:
        # send `prompt` as a single user message, return the text of the reply
        ...
```

Then add a branch for it in `LLM.__init__` (e.g. `backend == "my_provider"`) and set `backend: "my_provider"` in `config.yaml` (or `model_backend="my_provider"` on individual agents). Nothing else in the framework changes.

**Model used in testing:** Gemma 4 26B (`gemma4:26b-a4b-it-q4_K_M`) via Ollama, the `config.yaml` default — consistently follows tool group loading rules and the JSON action contract. Stronger models are expected to produce better reports (fewer copying slips, better source handling) with the same flows.
---

## Reference

### Configuration

Edit `config.yaml`:

```yaml
flow:
  name: competitive_analysis   # which registered flow to run (qa_agent, competitive_analysis)

llm:
  backend: ollama              # ollama | openai (any OpenAI-compatible endpoint)
  model_name: gemma4:26b-a4b-it-q4_K_M
  # base_url: http://localhost:11434   # optional, defaults per backend
  # api_key_env: GITHUB_TOKEN          # openai only: env var holding the API key

server:
  host: 0.0.0.0
  port: 8000

storage:
  working_dir: .agentflow      # sessions are stored under <working_dir>/<session_id>/
```

For the `openai` backend, put the API key in `.env` under the name set by `api_key_env` (default `GITHUB_TOKEN`):
```
GITHUB_TOKEN=your_token_here
```

### Running the server

```bash
uvicorn src.sse_server.app:app --host 0.0.0.0 --port 8000
```

### API

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/chats` | POST | Start a new session, or resume one with `{"session_id": "<id>"}` |
| `/chats/{id}/output/stream` | GET | SSE stream of agent output |
| `/chats/{id}/input` | POST | Send user input / HITL response |
| `/chats/{id}` | DELETE | Cancel and end a session |

### Flows

- `competitive_analysis` (default) — the [multi-agent demo](#multi-agent-demo-competitive-analysis). Scripted end-to-end run over HTTP/SSE: `python -m tests.scripts.competitive_analysis_e2e --query "..." --answer "..."`
- `qa_agent` — a single agent with web search, calculator and full memory tooling: `python -m src.tui.tui --flow qa_agent`

### Tests

```bash
pip install -r requirements-dev.txt
pytest tests/unit/test_memory.py tests/unit/test_agent_handoff.py \
       tests/unit/test_skill_retriever.py tests/unit/test_web_fetch.py
```

---

## Project Status

The framework's core is complete and working:

- ✅ Async two-threaded runtime, decoupled from the transport (SSE server and terminal UI)
- ✅ ReAct loop with self-correcting output parsing
- ✅ Tool system with on-demand group loading
- ✅ Memory scopes: conversation history, task scratchpad, long-term ChromaDB stores
- ✅ SkillStore with LLM-driven skill creation and retrieval
- ✅ Human-in-the-loop via the async channel design
- ✅ Multi-agent wiring via `>>` / `-` operator DSL, with a three-agent demo flow
- ✅ Per-session storage and session resumption

🔧 In progress:
- Multi-turn sessions (stay open for follow-up queries until the user ends the session)
- `agentflow` CLI command (packaging)
- Eval harness (tool-call correctness, task completion, HITL accuracy)
- Conversation summarization

---

## License

MIT
