# agentflow

A custom async multi-agent orchestration framework built from scratch in Python — no LangChain, no LangGraph, no AutoGen.

agentflow owns the hard parts of running agents in production: the ReAct execution loop, multi-agent routing, tiered memory, and human-in-the-loop — so agent builders only supply a prompt and a tool list.

---

## Why agentflow

Most agent frameworks treat orchestration, memory, and HITL as things you wire up yourself per-agent. agentflow inverts this — the framework owns the control flow, and agents are declarative: a prompt, a set of tools, and a resolver (who handles clarifications). Everything else is the framework's problem.

Design principles:
- **Text-in, text-out LLM contract** — tools and output format are described in plain text in the prompt. The LLM returns a single JSON action object. No native function-calling APIs, so any backend (OpenAI-compatible, Ollama, future providers) works with zero changes to the agent loop.
- **On-demand tool loading** — tools are grouped; the LLM explicitly loads a group before using it. Keeps the static prompt lean and improves caching as tool count grows.
- **Memory as a first-class concern** — three distinct memory scopes, each designed for a specific job, rather than dumping everything into a vector DB.
- **HITL falls out of the architecture** — agents pause for human input by awaiting on the async channel. No separate pause/resume state machine.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                   Client (HTTP / SSE)                │
└────────────────────────┬────────────────────────────┘
                         │
              ┌──────────▼──────────┐
              │   FastAPI Server    │  Thread 1 — server event loop
              │   (sse_server/)     │
              └──────────┬──────────┘
                         │  AsyncChannel (thread-safe, turn-based)
              ┌──────────▼──────────┐
              │   Agent Runtime     │  Thread 2 — dedicated agent event loop
              │   (runtime/)        │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │   AgentsFlow        │  Graph runner — routes between agents
              │   (core/flow.py)    │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │   Agent             │  ReAct loop per agent
              │   (core/agent.py)   │
              └──────────┬──────────┘
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
     ToolManager      MemoryManager   LLM Backend
   (tools/ + groups)  (memory/)      (Ollama / GitHub Models)
```

---

## Execution Layer & Async Runtime

agentflow runs two event loops in two OS threads inside a single process.

**Thread 1 — FastAPI server thread**: handles HTTP requests (session creation, SSE streaming, user input, session deletion).

**Thread 2 — Agent thread**: a dedicated asyncio event loop managed by `AsyncAgentManager`. Each agent session runs as an asyncio Task on this loop — multiple sessions run concurrently via cooperative async scheduling. A `ThreadPoolExecutor(max_workers=200)` is configured as the default executor for offloading synchronous blocking calls (e.g. ChromaDB queries, non-async LLM backends) via `asyncio.to_thread()`. Concurrent sessions beyond 200 are supported — Tasks simply await thread availability while the event loop continues processing other sessions.

### AsyncChannel

The sole bridge between the two threads, one per session:

- `client_in_q` — agent → client messages (streamed to the user over SSE)
- `agent_in_q` — client → agent messages (user inputs, HITL responses)
- A `Turn` enum (`AGENT` / `CLIENT`) with a threading lock enforces turn-taking — only one side may enqueue at a time
- Cross-thread queue operations use `asyncio.run_coroutine_threadsafe` + `asyncio.wrap_future` to safely bridge the two loops

### Session lifecycle

1. `POST /chats` → creates a session ID, a fresh `AsyncChannel`, schedules the agent flow on Thread 2
2. `GET /chats/{id}/output/stream` → SSE stream; yields every agent message until a `"done"` message closes the stream
3. `POST /chats/{id}/input` → sends user input to the agent; returns HTTP 400 if it's not the client's turn
4. `DELETE /chats/{id}` → cancels the agent task, removes the session

---

## Core ReAct Loop

The Think → Act → Observe loop lives entirely in `src/core/agent.py`.

Each iteration branches on a `RuntimeState` dataclass:

```
while not should_yield and not irrecoverable_error:
    if pending_tool_call  →  Act:     execute tool, append result to scratchpad
    elif needs_clarification →  Pause:  send query to resolver (user or agent), await response
    else                  →  Think:   format prompt, call LLM, parse output
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

Agents are constructed directly in Python — no YAML or config DSL:

```python
Agent(
    agent_name="researcher",
    model_name="gemma4:26b-a4b-it-q4_K_M",
    model_backend="ollama",
    tool_grps=[web_tools, memory_tools],
    always_on_tools=[load_tool_group],
    execution_prompt_path=Path("prompts/researcher.md"),
    resolver="orchestrator",   # clarifications route to this agent
)
```

- `execution_prompt_path` — a markdown file defining the agent's role, persona, and instructions. This is the only agent-specific configuration.
- `resolver` — either `"user"` (clarifications go to the human over the channel) or another agent's name (clarifications become inter-agent edges, routed by the flow).

### Wiring agents together

Agents are wired into a directed graph using operator overloads:

```python
# Default routing
orchestrator >> worker

# Named action routing — LLM's yield_action determines which path is taken
orchestrator - "research" >> researcher
orchestrator - "calculate" >> calculator_agent
orchestrator - "end" >> None
```

At runtime, `AgentsFlow` drives execution: it calls `agent.execute()`, reads the returned `Edge`, looks up `successors[yield_action]`, and advances to the next agent — passing the previous agent's output as the new agent's input.

---

## Tool System

### Defining a tool

```python
@tool(name="web_search", description="Search the web using DuckDuckGo")
async def web_search(query: str, _ctx: ToolContext = None) -> str:
    ...
```

The `@tool` decorator introspects type hints and docstrings to auto-build the args schema string that gets embedded in the LLM prompt. `_ctx` is a dependency-injection bag carrying all framework internals (stores, scratchpad, session info) — never exposed in the schema.

### Tool groups — always-on vs on-demand

**Always-on tools** (e.g. `load_tool_group`) are available in every prompt from the first turn.

**Tool groups** bundle related tools with a shared instruction block. Only the group name + description + tool name list appear in the base prompt. Full schemas and instructions are hidden until the group is loaded:

```
LLM calls: load_tool_group("web_tools")
→ full schema for web_search injected into scratchpad
→ tool is now callable
```

This keeps the static prompt prefix lean and lets the LLM load only what it needs for the current task. Up to 3 groups can be loaded simultaneously (FIFO eviction when the cap is exceeded).

### Shipped tools

| Tool | Group | Purpose |
|------|-------|---------|
| `load_tool_group` | always-on | loads a tool group's schema into the scratchpad |
| `web_search` | web_tools | DuckDuckGo search, top 3 results |
| `calculator` | compute_tools | safe math expression evaluator |
| `skill_retriever` | memory_tools | view or semantically retrieve skills from SkillStore |
| `save_skill` | memory_tools | persist a skill to SkillStore |
| `recall_search` | long_term_memory | semantic search over conversation history |
| `fact_search` | long_term_memory | semantic search over curated facts |
| `fact_write` | long_term_memory | persist a fact |
| `working_memory_update` | long_term_memory | overwrite and persist working memory index |

---

## Memory Architecture

Three distinct memory scopes, each designed for a specific job.

```
┌─────────────────────────────────────────────────────────┐
│                    Memory Scopes                         │
├──────────────┬──────────────────┬───────────────────────┤
│  Session     │  Task            │  Long-term            │
│  (in-memory) │  (in-memory)     │  (persisted to disk)  │
├──────────────┼──────────────────┼───────────────────────┤
│ conversation │ scratchpad trail │ SkillStore (ChromaDB)  │
│ history      │ active skill     │ RecallStore (ChromaDB) │
│              │ loaded tool grps │ FactStore (ChromaDB)   │
│              │                  │ working memory (JSON)  │
└──────────────┴──────────────────┴───────────────────────┘
```

### Session scope — conversation history

`MemoryManager.conversation_history` holds the rolling `{role, content}` message list, capped at `max_short_term` (default 20). Injected into the prompt's `<memory>` section every turn.

As history fills up, a **memory pressure system** kicks in:
- At 70% capacity → a system instruction is injected into the scratchpad telling the LLM to offload important context to `fact_write` or `working_memory_update` before continuing
- At 100% capacity → oldest 30% of history is evicted

### Task scope — scratchpad

`ScratchPad` carries the running reasoning trail within a single task:

- `trail` — every `[THOUGHT]`, `[TOOL CALL]`, `[FAILED TOOL CALL]`, and `[SYSTEM]` entry from the current task
- `active_skill` — the retrieved skill content, rendered as `[ACTIVE SKILL]` when a skill is loaded
- `loaded_tool_grp_str` — schemas of currently-loaded tool groups

The scratchpad is what gives the LLM continuity across ReAct iterations — it sees its own reasoning trail and tool results, not just the latest turn.

### Long-term scope — persisted across sessions

All three long-term stores use ChromaDB with cosine similarity, scoped per-agent (`skills_{agent_name}`, `recall_{agent_name}`, `facts_{agent_name}`).

**SkillStore** — the most distinctive component. Agents distill successful multi-step task plans into reusable skills and retrieve them on demand.

- Skills are stored as markdown files on disk; only the name + description go into ChromaDB for semantic search
- Retrieval is via the `skill_retriever` tool (on-demand, not auto-injected) — the LLM decides when to look for a relevant skill
- Skill creation is LLM-driven: after a multi-step task using 2+ distinct tool calls, the LLM is instructed to check for duplicates, then save a generalisable plan as a skill
- A retrieved skill surfaces under `[ACTIVE SKILL]` in the scratchpad — a dedicated section separate from the tool call trail

This design is consistent with the procedural memory approach in the [Voyager paper](https://arxiv.org/abs/2305.16291), arrived at independently.

**RecallStore** — append-only semantic log of every turn (user inputs, clarification exchanges, LLM responses). Searchable via `recall_search`. Provides long-range memory beyond the conversation history window.

**FactStore** — curated facts explicitly written by the LLM via `fact_write`. Semantically searchable via `fact_search`. Separate from recall — these are things the agent has decided are worth keeping, not a full transcript.

**Working memory** — a single string per agent, persisted as JSON. Always injected verbatim into the prompt as a compact index the agent maintains. Updated only via `working_memory_update`. Survives process restarts.

---

## Supported Models

| Backend | How to configure |
|---------|-----------------|
| Ollama (local) | `model_backend="ollama"`, `model_name="<ollama model>"` — defaults to `localhost:11434` |
| GitHub Models | `model_backend="openai"`, `model_name="<model>"` — requires `GITHUB_TOKEN` in `.env` |

The LLM interface is a single `invoke(prompt: str) -> str` call — the full context is pre-flattened into one prompt string, so any backend that accepts a text prompt works with zero changes to the agent loop.

**Best-performing model in testing:** Gemma 4 26B via Ollama (thinking mode) — most consistently follows tool group loading rules and memory workflow instructions.

---

## Getting Started

### Prerequisites

- Python 3.11+
- [Ollama](https://ollama.com/) installed and running locally (for local model inference)
- ChromaDB (installed via pip)

### Installation

```bash
git clone https://github.com/sriv2804/agentflow.git
cd agentflow
pip install -r requirements.txt  # see repo for full dependency list
```

### Configuration

Edit `config.yaml`:

```yaml
flow:
  name: qa_agent       # which registered flow to run

server:
  host: 0.0.0.0
  port: 8000

storage:
  working_dir: .agentflow
  session_id: default  # long-term memory namespace
```

For GitHub Models, add to `.env`:
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
| `/chats` | POST | Start a new session |
| `/chats/{id}/output/stream` | GET | SSE stream of agent output |
| `/chats/{id}/input` | POST | Send user input / HITL response |
| `/chats/{id}` | DELETE | Cancel and end a session |

### Running the example

```bash
python -m examples.qa_agent.qa_agent_e2e
```

The shipped example (`qa_agent`) runs a single orchestrator agent with web search, calculator, and full memory tooling — a working proof-of-concept for the framework's execution layer, memory system, and tool group loading.

---

## Project Status

The framework's core is complete and working:

- ✅ Async two-threaded runtime with SSE streaming
- ✅ ReAct loop with self-correcting output parsing
- ✅ Tool system with on-demand group loading
- ✅ Three-tier memory (session, task scratchpad, long-term ChromaDB stores)
- ✅ SkillStore with LLM-driven skill creation and retrieval
- ✅ Human-in-the-loop via the async channel design
- ✅ Multi-agent wiring via `>>` / `-` operator DSL

🔧 In progress:
- Multi-agent demo flow (manager + workers)
- Eval harness (tool-call correctness, task completion, HITL accuracy)
- Conversation summarization

---

## License

MIT