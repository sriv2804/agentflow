# Deployment

## Phase 0: Container

The repo root has a `Dockerfile` that packages the SSE server (`src/sse_server`) into one image.

### Build

```bash
docker build -t agentflow:dev .
```

- Base image: `python:3.12-slim`.
- `requirements.txt` is copied and installed before the source, so editing code only rebuilds the final `COPY . .` layer (a few seconds). Changing `requirements.txt` re-runs `pip install`.
- Chroma's default embedding model (`all-MiniLM-L6-v2`, ONNX) is downloaded during the build into the app user's cache, so containers don't fetch ~80 MB on first use.
- `config.yaml` is baked into the image. `.env`, `.git`, `tests/`, local session data and virtualenvs are excluded by `.dockerignore`.
- The app runs as a non-root user (`app`, uid 1000). It can only write to `/app/.agentflow` (session data) and `/app/logs` (prompt debug logs, `AGENTFLOW_LOG_DIR`).

### Run

```bash
docker run -d --name af -p 8000:8000 \
  -e AGENTFLOW_MODEL_BACKEND=openai \
  -e AGENTFLOW_MODEL_NAME=gemini-3.1-flash-lite \
  -e AGENTFLOW_MODEL_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/ \
  -e AGENTFLOW_API_KEY \
  -v "$(pwd)/.agentflow-data:/app/.agentflow" \
  agentflow:dev

curl localhost:8000/healthz   # {"status":"ok"}
```

`-e AGENTFLOW_API_KEY` without `=value` passes the variable through from your shell, so the key never appears in the command or shell history.

`GET /healthz` returns `200 {"status": "ok"}` without calling the LLM, so it is safe to use as a liveness probe.

### Environment variables

| Variable | Overrides | Notes |
|---|---|---|
| `AGENTFLOW_MODEL_BACKEND` | `llm.backend` in `config.yaml` | `openai` (any OpenAI-compatible endpoint) or `ollama`. When it differs from the backend in `config.yaml`, that backend's `base_url` / `api_key_env` are ignored. |
| `AGENTFLOW_MODEL_NAME` | `llm.model_name` in `config.yaml` | e.g. `gemini-3.1-flash-lite` |
| `AGENTFLOW_MODEL_BASE_URL` | `llm.base_url` in `config.yaml` | Endpoint of the backend. Required for `openai` (there is no default endpoint), e.g. `https://generativelanguage.googleapis.com/v1beta/openai/`. Optional for `ollama` (default `http://localhost:11434`). |
| `AGENTFLOW_API_KEY` | – | API key for the `openai` backend. If unset, the env var named by `llm.api_key_env` is read, if configured. Not used by `ollama`. |
| `AGENTFLOW_LOG_DIR` | – | Where per-call LLM prompt logs are written. Default `logs/` (relative to the working dir); the image sets `/app/logs`, which is writable by the app user. Point it elsewhere only together with a writable mount. |

The four `AGENTFLOW_MODEL_*` / `AGENTFLOW_API_KEY` variables fully describe a backend, so a container needs nothing from `config.yaml`'s `llm` block.

Precedence, highest first: a `model_name` / `model_backend` passed to `Agent(...)` in a flow, then env vars, then `config.yaml`. With no env vars set, `config.yaml` is used as is.

If the selected backend is missing an endpoint or key, the server exits at startup:

```
agentflow: startup failed: The 'openai' LLM backend needs an endpoint: set llm.base_url in config.yaml or AGENTFLOW_MODEL_BASE_URL.
agentflow: startup failed: The 'openai' LLM backend needs an API key: set AGENTFLOW_API_KEY.
```

Note: Inside a container, `localhost` is the container itself, so Ollama on the host is not reachable with the default endpoint. Point `AGENTFLOW_MODEL_BASE_URL` at the host, e.g. `http://host.docker.internal:11434` (Docker Desktop, or `--add-host=host.docker.internal:host-gateway` on Linux).

### Model provider: Gemini

GitHub Models (the previous default endpoint) was retired on 2026-07-30. The demo uses the Gemini API through its OpenAI-compatible endpoint with `gemini-3.1-flash-lite` (small, fast, stable name; `gemini-2.5-flash-lite` is closed to new users).

The free tier is rate limited per model (15 requests/minute for `gemini-3.1-flash-lite` at the time of writing) and returns frequent transient `503 UNAVAILABLE` ("high demand") errors. The OpenAI SDK retries both, but only twice within ~1.5 s, and it only honours a `Retry-After` header, which Gemini does not send (the wait is in the error body: "Please retry in 26.6s"). `OpenAIClient` therefore retries 429 and 5xx up to 5 more times: 429s wait for the server-suggested delay, 5xx use exponential backoff (2, 4, 8, ... s). A suggested wait over 2 minutes (daily quota exhausted) fails immediately instead of hanging.

### Volume: `/app/.agentflow`

Each session stores its conversation history, working memory, skills and ChromaDB data under `/app/.agentflow/<session_id>/`. A container's filesystem is discarded when the container is removed, so mount a volume there to keep sessions:

```bash
mkdir -p .agentflow-data        # create it first so it is owned by you, not root
docker run ... -v "$(pwd)/.agentflow-data:/app/.agentflow" agentflow:dev
```

A new container with the same volume can resume a stored session with `POST /chats {"session_id": "<id>"}`. If Docker creates the host directory itself it is owned by root, and the non-root `app` user cannot write to it.

### End-to-end check against the container

```bash
timeout 1200 python -m tests.scripts.competitive_analysis_e2e \
  --base-url http://localhost:8000 --report-dir .agentflow-data \
  --answer "features, pricing and integrations for a 20-person eng team" < /dev/null
```

`--report-dir` is the host side of the `/app/.agentflow` volume; the script looks for the session's report there. Use a client-side `timeout` (see Known issues).

## Known issues

- **SSE stream hangs when the flow fails.** If an LLM call fails for good (e.g. retries exhausted), the output stream never sends a `done` message and the client waits forever. Always use a client-side timeout. Being fixed separately.
- **Unit tests already failing on `main`/HEAD** (they predate Phase 0 and are not fixed here):
  - `tests/unit/test_agent.py`: `test_rshift_registers_under_default`, `test_sub_rshift_registers_under_named_action`, `test_conditional_transition_rshift_returns_dest_agent`, `test_invalid_action_type_raises_type_error`, `test_parse_llm_output_returns_valid_structure`
  - `tests/unit/test_agent_handoff.py`: `test_trail_is_cleared_between_handoffs`, `test_yield_is_recorded_in_own_history`, `test_recall_store_uses_real_sender_role`, `test_storage_is_scoped_to_runtime_session_id`, `test_resumed_session_restores_conversation_history`, `test_new_session_starts_with_empty_history`
  - `tests/unit/test_skill_retriever.py`: `test_view_with_no_skills_says_so`, `test_view_lists_names_without_touching_active_skill`, `test_get_sets_active_skill_and_returns_confirmation`, `test_get_with_no_match_leaves_active_skill_empty`
  - `tests/unit/test_web_fetch.py`: `test_fetch_returns_clean_text`, `test_fetch_reports_http_errors`, `test_fetch_reports_timeouts`
