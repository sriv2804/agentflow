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

## Phase 1+2: Kubernetes (local kind)

Runs the same `agentflow:dev` image on a single-node [kind](https://kind.sigs.k8s.io/) cluster (Kubernetes in a Docker container). The manifests are in `k8s/`. Tested with kind v0.33.0 (node image `kindest/node:v1.37.0`) and kubectl v1.37.

### Create the cluster and load the image

```bash
# kind as a user binary (kubectl is preinstalled in the Codespace)
curl -fsSLo ~/.local/bin/kind https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-amd64
chmod +x ~/.local/bin/kind

kind create cluster --name agentflow          # ~1-2 min; sets the kubectl context to kind-agentflow
docker build -t agentflow:dev .
kind load docker-image agentflow:dev --name agentflow
```

The kind node is a Docker container with its own container runtime (containerd) and image store; it cannot see images in the Codespace's Docker daemon. `kind load` copies the image into the node. Without it the kubelet tries to pull `docker.io/library/agentflow:dev`, which does not exist, and the pod sits in `ErrImagePull` / `ImagePullBackOff`. Re-run `kind load` (then `kubectl rollout restart deployment/agentflow`) after every rebuild.

**Codespaces: pods have no internet access until you allow the kind bridge.** See Known issues; run this once after creating the cluster (and again after a Codespace restart):

```bash
BR=br-$(docker network inspect kind -f '{{.Id}}' | cut -c1-12)
sudo iptables-legacy -I FORWARD 1 -i "$BR" -j ACCEPT
sudo iptables-legacy -I FORWARD 2 -o "$BR" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
docker exec agentflow-control-plane getent hosts generativelanguage.googleapis.com   # should print an address
```

### Create the Secret, apply, port-forward

```bash
# the key goes from your shell straight into the cluster, never into a file
kubectl create secret generic agentflow-secrets --from-literal=AGENTFLOW_API_KEY="$AGENTFLOW_API_KEY"

kubectl apply -k k8s/                         # not -f k8s/: that would also apply secret.example.yaml
kubectl wait --for=condition=Ready pod -l app.kubernetes.io/name=agentflow --timeout=180s

kubectl port-forward service/agentflow 8000:8000   # keep running; new terminal for the rest
curl localhost:8000/healthz                         # {"status":"ok"}
```

`port-forward service/...` picks one pod when it starts and stays attached to it. When that pod is replaced (delete, rollout restart, config change) the forward fails with `lost connection to pod` and must be restarted. Traffic inside the cluster (`http://agentflow:8000`) follows the Service to the new pod automatically.

End-to-end check (the report is on the PVC inside the cluster, so the script prints `Report file: NOT FOUND`; check it with `kubectl exec`):

```bash
timeout 1200 python -m tests.scripts.competitive_analysis_e2e --base-url http://localhost:8000 \
  --answer "features, pricing and integrations for a 20-person eng team" < /dev/null
kubectl exec deploy/agentflow -- ls /app/.agentflow/<session_id>/reports
```

### What each manifest does

| File | Object | What it does |
|---|---|---|
| `k8s/kustomization.yaml` | – | Lists the files `kubectl apply -k k8s/` applies; leaves out `secret.example.yaml`. |
| `k8s/configmap.yaml` | ConfigMap `agentflow-config` | The `AGENTFLOW_MODEL_*` and `AGENTFLOW_LOG_DIR` env vars (Gemini endpoint and model). |
| `k8s/secret.example.yaml` | Secret `agentflow-secrets` (example only) | Shape of the Secret holding `AGENTFLOW_API_KEY`; never applied. The real one is created with `kubectl create secret`. |
| `k8s/pvc.yaml` | PersistentVolumeClaim `agentflow-data` | 1Gi ReadWriteOnce volume for session data, mounted at `/app/.agentflow`; survives pod restarts. |
| `k8s/deployment.yaml` | Deployment `agentflow` | One replica, `Recreate` strategy (single-writer volume), env from the ConfigMap and Secret, PVC plus an `emptyDir` for `/app/logs`, non-root, `/healthz` probes, memory request 512Mi / limit 1Gi, CPU request 250m and no CPU limit. |
| `k8s/service.yaml` | Service `agentflow` (ClusterIP) | Stable in-cluster name and IP on port 8000, routing to Ready pods with label `app.kubernetes.io/name=agentflow`. |

### Operating notes

- **Config changes need a restart.** Env vars from a ConfigMap are read when the container starts. After `kubectl apply -k k8s/` with a new value, the running pod keeps the old one; run `kubectl rollout restart deployment/agentflow`.
- **Memory.** Measured in this run: ~150Mi idle, up to ~540Mi during a competitive-analysis flow. With a 200Mi limit the container was OOMKilled (exit 137) within 2 s of the first query and restarted by the kubelet. The request must be <= the limit, or the API server rejects the Deployment.
- **Volume ownership.** kind's `local-path` provisioner backs the PVC with a `hostPath` directory created as `root:root 0777`; `fsGroup` is not applied to `hostPath` volumes, so the app can write only because the directory is world-writable. (The `emptyDir` at `/app/logs` does get `fsGroup` 1000.) On a cloud storage class `fsGroup` takes effect.
- **Prompt logs are per pod.** `/app/logs` is an `emptyDir`; it is deleted with the pod.

## Known issues

- **SSE stream hangs when the flow fails.** If an LLM call fails for good (e.g. retries exhausted), the output stream never sends a `done` message and the client waits forever. Always use a client-side timeout. Being fixed separately.
- **A pod stopped mid-flow loses that session's conversation.** Observed on kind (pod deleted while a flow was running a web search):
  - The kubelet sends SIGTERM. Uvicorn (PID 1) starts a graceful shutdown (`Shutting down`, `Waiting for connections to close`) but waits for the open SSE stream, which never closes on its own. The flow runs on its own event loop thread and is not cancelled: it kept calling tools for another ~14 s.
  - After `terminationGracePeriodSeconds` (default 30, not set in `deployment.yaml`) the kubelet sends SIGKILL; the pod ended in `Error` 30.5 s after the delete.
  - Conversation history is written only when a flow ends (`finally` in `run_on_channel`), and that never ran: the session directory had `chromadb/` and `skills_*/` but no `conversation_history_*.json`.
  - `POST /chats {"session_id": ...}` still accepts the session (the directory exists), but the agent starts with an empty history: asked what was being compared, it answered "We haven't started a competitive analysis project yet".
  - Not fixed here. Directions: handle shutdown in the FastAPI lifespan (cancel running flows so their `finally` saves history, and close SSE streams), set uvicorn's `--timeout-graceful-shutdown` below the grace period, and/or save history after each turn instead of only at the end.
- **Codespaces: kind pods have no internet access.** The Codespace has two iptables rulesets that the kernel applies together: the current Docker daemon's (nftables, correct) and a leftover `iptables-legacy` one whose `FORWARD` policy is `DROP` and only allows `docker0`. Traffic from the kind bridge network (`br-<id>`) is dropped there, so the node's DNS lookups time out (CoreDNS logs `read udp ...->172.18.0.1:53: i/o timeout`) and LLM calls fail with `ConnectTimeout`. The default bridge works, which is why plain `docker run` (Phase 0) is not affected. Fix: the two `iptables-legacy` rules in "Create the cluster and load the image"; they do not survive a Codespace restart.
- **Service links collide with the `AGENTFLOW_` prefix.** For a Service named `agentflow`, Kubernetes injects `AGENTFLOW_PORT=tcp://<ip>:8000`, `AGENTFLOW_SERVICE_HOST`, etc. into every pod in the namespace. The app reads none of these today, but `deployment.yaml` sets `enableServiceLinks: false` so a future `AGENTFLOW_PORT` setting cannot be overridden by accident.
- **Unit tests already failing on `main`/HEAD** (they predate Phase 0 and are not fixed here):
  - `tests/unit/test_agent.py`: `test_rshift_registers_under_default`, `test_sub_rshift_registers_under_named_action`, `test_conditional_transition_rshift_returns_dest_agent`, `test_invalid_action_type_raises_type_error`, `test_parse_llm_output_returns_valid_structure`
  - `tests/unit/test_agent_handoff.py`: `test_trail_is_cleared_between_handoffs`, `test_yield_is_recorded_in_own_history`, `test_recall_store_uses_real_sender_role`, `test_storage_is_scoped_to_runtime_session_id`, `test_resumed_session_restores_conversation_history`, `test_new_session_starts_with_empty_history`
  - `tests/unit/test_skill_retriever.py`: `test_view_with_no_skills_says_so`, `test_view_lists_names_without_touching_active_skill`, `test_get_sets_active_skill_and_returns_confirmation`, `test_get_with_no_match_leaves_active_skill_empty`
  - `tests/unit/test_web_fetch.py`: `test_fetch_returns_clean_text`, `test_fetch_reports_http_errors`, `test_fetch_reports_timeouts`
