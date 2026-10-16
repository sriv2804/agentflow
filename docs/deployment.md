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
- The app runs as a non-root user (`app`, uid 1000). It can only write to `/app/.agentflow` (session data) and `/app/logs` (prompt debug logs).

### Run

```bash
docker run -d --name af -p 8000:8000 \
  -e AGENTFLOW_MODEL_BACKEND=openai \
  -e AGENTFLOW_MODEL_NAME=gpt-4o-mini \
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
| `AGENTFLOW_MODEL_BACKEND` | `llm.backend` in `config.yaml` | `openai` or `ollama`. When it differs from the backend in `config.yaml`, that backend's `base_url` / `api_key_env` are ignored and the new backend's defaults are used. |
| `AGENTFLOW_MODEL_NAME` | `llm.model_name` in `config.yaml` | |
| `AGENTFLOW_API_KEY` | – | API key for the `openai` backend. If unset, falls back to the env var named by `llm.api_key_env` (default `GITHUB_TOKEN`). |

Precedence, highest first: a `model_name` / `model_backend` passed to `Agent(...)` in a flow, then env vars, then `config.yaml`. With no env vars set, `config.yaml` behaves exactly as before.

If the selected backend needs a key and none is set, the server exits at startup:

```
agentflow: startup failed: The 'openai' LLM backend needs an API key: set AGENTFLOW_API_KEY (or GITHUB_TOKEN).
```

Note: the `ollama` backend defaults to `http://localhost:11434`. Inside a container, `localhost` is the container itself, so Ollama on the host is not reachable with the default settings.

### Volume: `/app/.agentflow`

Each session stores its conversation history, working memory, skills and ChromaDB data under `/app/.agentflow/<session_id>/`. A container's filesystem is discarded when the container is removed, so mount a volume there to keep sessions:

```bash
mkdir -p .agentflow-data        # create it first so it is owned by you, not root
docker run ... -v "$(pwd)/.agentflow-data:/app/.agentflow" agentflow:dev
```

A new container with the same volume can resume a stored session with `POST /chats {"session_id": "<id>"}`. If Docker creates the host directory itself it is owned by root, and the non-root `app` user cannot write to it.
