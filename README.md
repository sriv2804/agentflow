# agentflow

A custom async multi-agent orchestration framework built from scratch in Python — no LangGraph, no LangChain.
agentflow is an agent harness — it owns the execution loop, not you. Bring a prompt and tools; the harness handles the rest.

---

## Why agentflow

- **Framework-owned ReAct loop** — agent implementers only provide a prompt and tools. The think/tool-call/clarify cycle is handled by the framework, not scattered across user code.
- **Name-based action edges** — LLM output drives routing between agents natively. No wrapper logic; the agent declares its successors and the framework resolves them.
- **Memory as a tool** — agents retrieve long-term context on demand via a retriever tool, not blindly injected at session start.
- **HITL falls out naturally** — human-in-the-loop is just `await channel.receive_from_client()`. No special framework handling or callback hooks needed.

---

## Architecture

```
User (HTTP)
    │
    ▼
┌─────────────────────────┐
│  FastAPI Server Thread  │
│  POST /chat             │
│  GET  /stream/{id} SSE  │
└───────────┬─────────────┘
            │  AsyncChannel (turn-based, thread-safe)
┌───────────▼─────────────┐
│  Agent Runtime Thread   │
│                         │
│  AgentsFlow             │
│    OrchestratorAgent    │
│      ├── WorkerAgent A  │
│      └── WorkerAgent B  │
└─────────────────────────┘
```

The server and agent runtime communicate exclusively through `AsyncChannel` — bidirectional async queues that make the boundary between HTTP handling and agent execution explicit.

---

## Core Concepts

**`Agent`**  
Holds a prompt, a list of tools, a resolver (`"user"` or another agent name), and a successor map. The framework runs the ReAct loop; implementers don't write loop logic.

**`AgentsFlow`**  
Wires agents together with typed action edges. Routing is name-based and declared in code:

```python
orchestrator - "summarize" >> summarizer
orchestrator >> worker  # "default" edge
```

**`SessionContext`**  
Single shared object per user session. Holds the `AsyncChannel` and lazily-initialized per-agent `AgentContext` instances.

**`MemoryManager`**  
Per-agent short-term conversation history with a configurable eviction window. Summarization and long-term retrieval via ChromaDB are added in Week 4.

**`AsyncChannel`**  
Bidirectional queues bridging the FastAPI server thread and the agent runtime thread. HITL is a first-class pattern — no special framework support required beyond `await channel.receive_from_client()`.

---

## Project Status

| Week | Focus | Status |
|------|-------|--------|
| 1 | Core framework — `Agent`, `AgentsFlow`, `SessionContext`, `MemoryManager` | ✅ Complete |
| 2 | Runtime — `AsyncChannel`, scheduler, FastAPI server | 🔲 In progress |
| 3 | LLM integration + tool calling | 🔲 Upcoming |
| 4 | Memory layer — short-term + RAG over ChromaDB | 🔲 Upcoming |
| 5–6 | Evals + polish | 🔲 Upcoming |

---

## Setup

```bash
git clone https://github.com/sriv2804/agentflow
cd agentflow
pip install -r requirements.txt
```

---

## Running Tests

```bash
pytest tests/
```
