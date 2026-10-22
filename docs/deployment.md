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

curl localhost:8000/healthz   # {"status":"ok","version":"dev"}
```

`-e AGENTFLOW_API_KEY` without `=value` passes the variable through from your shell, so the key never appears in the command or shell history.

`GET /healthz` returns `200 {"status": "ok", "version": "<git sha>"}` without calling the LLM, so it is safe to use as a liveness probe. `version` comes from the `AGENTFLOW_VERSION` build arg (`docker build --build-arg AGENTFLOW_VERSION=$(git rev-parse --short HEAD) ...`) and is `dev` when not set. `/healthz` never requires the auth token (see Phase 3).

### Environment variables

| Variable | Overrides | Notes |
|---|---|---|
| `AGENTFLOW_MODEL_BACKEND` | `llm.backend` in `config.yaml` | `openai` (any OpenAI-compatible endpoint) or `ollama`. When it differs from the backend in `config.yaml`, that backend's `base_url` / `api_key_env` are ignored. |
| `AGENTFLOW_MODEL_NAME` | `llm.model_name` in `config.yaml` | e.g. `gemini-3.1-flash-lite` |
| `AGENTFLOW_MODEL_BASE_URL` | `llm.base_url` in `config.yaml` | Endpoint of the backend. Required for `openai` (there is no default endpoint), e.g. `https://generativelanguage.googleapis.com/v1beta/openai/`. Optional for `ollama` (default `http://localhost:11434`). |
| `AGENTFLOW_API_KEY` | – | API key for the `openai` backend. If unset, the env var named by `llm.api_key_env` is read, if configured. Not used by `ollama`. |
| `AGENTFLOW_LOG_DIR` | – | Where per-call LLM prompt logs are written. Default `logs/` (relative to the working dir); the image sets `/app/logs`, which is writable by the app user. Point it elsewhere only together with a writable mount. |
| `AGENTFLOW_AUTH_TOKEN` | – | When set, every route except `/healthz` requires `Authorization: Bearer <token>` (401 otherwise). Unset: no auth, as before. Set on GKE (Phase 3), not locally or on kind. The e2e script sends it when the same variable is set. |
| `AGENTFLOW_VERSION` | – | Reported by `/healthz`. Baked in at build time with `--build-arg AGENTFLOW_VERSION=<git sha>`; `dev` if not set. |

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

Runs the same `agentflow:dev` image on a single-node [kind](https://kind.sigs.k8s.io/) cluster (Kubernetes in a Docker container). The manifests are in `k8s/base/`, applied through the `k8s/overlays/kind` overlay (which adds nothing; see Phase 3 for the layout). Tested with kind v0.33.0 (node image `kindest/node:v1.37.0`) and kubectl v1.37.

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

kubectl apply -k k8s/overlays/kind           # not -f k8s/base/: that would also apply secret.example.yaml
kubectl wait --for=condition=Ready pod -l app.kubernetes.io/name=agentflow --timeout=180s

kubectl port-forward service/agentflow 8000:8000   # keep running; new terminal for the rest
curl localhost:8000/healthz                         # {"status":"ok","version":"dev"}
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
| `k8s/base/kustomization.yaml` | – | Lists the files both overlays build on; leaves out `secret.example.yaml`. |
| `k8s/base/configmap.yaml` | ConfigMap `agentflow-config` | The `AGENTFLOW_MODEL_*` and `AGENTFLOW_LOG_DIR` env vars (Gemini endpoint and model). |
| `k8s/base/secret.example.yaml` | Secret `agentflow-secrets` (example only) | Shape of the Secret holding `AGENTFLOW_API_KEY`; never applied. The real one is created with `kubectl create secret`. |
| `k8s/base/pvc.yaml` | PersistentVolumeClaim `agentflow-data` | 1Gi ReadWriteOnce volume for session data, mounted at `/app/.agentflow`; survives pod restarts. |
| `k8s/base/deployment.yaml` | Deployment `agentflow` | One replica, `Recreate` strategy (single-writer volume), env from the ConfigMap and Secret, PVC plus an `emptyDir` for `/app/logs`, non-root, `/healthz` probes, memory request 512Mi / limit 1Gi, CPU request 250m and no CPU limit. |
| `k8s/base/service.yaml` | Service `agentflow` (ClusterIP) | Stable in-cluster name and IP on port 8000, routing to Ready pods with label `app.kubernetes.io/name=agentflow`. |

### Operating notes

- **Config changes need a restart.** Env vars from a ConfigMap are read when the container starts. After `kubectl apply -k k8s/overlays/<kind|gke>` with a new value, the running pod keeps the old one; run `kubectl rollout restart deployment/agentflow`.
- **Memory.** Measured in this run: ~150Mi idle, up to ~540Mi during a competitive-analysis flow. With a 200Mi limit the container was OOMKilled (exit 137) within 2 s of the first query and restarted by the kubelet. The request must be <= the limit, or the API server rejects the Deployment.
- **Volume ownership.** kind's `local-path` provisioner backs the PVC with a `hostPath` directory created as `root:root 0777`; `fsGroup` is not applied to `hostPath` volumes, so the app can write only because the directory is world-writable. (The `emptyDir` at `/app/logs` does get `fsGroup` 1000.) On a cloud storage class `fsGroup` takes effect.
- **Prompt logs are per pod.** `/app/logs` is an `emptyDir`; it is deleted with the pod.

## Phase 3: GKE

Runs the same image on Google Kubernetes Engine: a zonal Standard cluster with one `e2-medium` node in `asia-south1-a`, the image in Artifact Registry, and a public IP from a Google Cloud load balancer. Tested with Google Cloud SDK 590.0.0 and GKE 1.36.4-gke.1495002.

| Setting | Value |
|---|---|
| Project | `project-80856c2d-bafd-40a6-8f8` |
| Region / zone | `asia-south1` / `asia-south1-a` |
| Cluster | `agentflow` (zonal, Standard, 1 x `e2-medium`, 20 GB `pd-balanced` boot disk) |
| Image | `asia-south1-docker.pkg.dev/project-80856c2d-bafd-40a6-8f8/agentflow/agentflow:<short git sha>` |

For a demo, `deploy/gke-up.sh` and `deploy/gke-down.sh` do everything below in one command each (see "Scripts").

### One-time setup

```bash
# gcloud as a user install, plus the plugin kubectl uses to get Google tokens for GKE
curl -fsSLO https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-linux-x86_64.tar.gz
tar -xzf google-cloud-cli-linux-x86_64.tar.gz -C ~
~/google-cloud-sdk/install.sh --quiet --usage-reporting=false --path-update=false
~/google-cloud-sdk/bin/gcloud components install gke-gcloud-auth-plugin --quiet
ln -sf ~/google-cloud-sdk/bin/{gcloud,gke-gcloud-auth-plugin,docker-credential-gcloud} ~/.local/bin/

# in a normal terminal (it reads the verification code from stdin, so `! cmd` in Claude Code fails with EOFError)
gcloud auth login --no-launch-browser

gcloud config set project project-80856c2d-bafd-40a6-8f8
gcloud services enable container.googleapis.com artifactregistry.googleapis.com   # also enables compute
gcloud billing projects describe project-80856c2d-bafd-40a6-8f8                   # billingEnabled: true

# Artifact Registry repo + docker login through gcloud (adds a credHelper, stores no password)
gcloud artifacts repositories create agentflow --repository-format=docker --location=asia-south1
gcloud auth configure-docker asia-south1-docker.pkg.dev --quiet

# Let the node VMs pull from it. New projects give the default compute service account
# no roles, so without this the pod sits in ImagePullBackOff with "403 Forbidden".
PN=$(gcloud projects describe project-80856c2d-bafd-40a6-8f8 --format="value(projectNumber)")
gcloud artifacts repositories add-iam-policy-binding agentflow --location asia-south1 \
  --member="serviceAccount:${PN}-compute@developer.gserviceaccount.com" --role=roles/artifactregistry.reader

# the API bearer token, kept outside the repo
(umask 077; python -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.agentflow-token)
```

### Build and push

```bash
SHA=$(git rev-parse --short HEAD)
IMG=asia-south1-docker.pkg.dev/project-80856c2d-bafd-40a6-8f8/agentflow/agentflow:$SHA
docker build --build-arg AGENTFLOW_VERSION=$SHA -t $IMG .
docker push $IMG     # prints the digest: "<sha>: digest: sha256:..."
```

Images are tagged with the commit they were built from, never `latest`: a tag that moves cannot be told apart by Kubernetes (re-applying the same YAML changes nothing), nodes may cache different "latest" images, and `kubectl rollout undo` needs a distinct, stable name to go back to. With a SHA, the running image maps to one `git show`, and `/healthz` reports it. Commit first, then build, so the SHA describes the code in the image.

### Cluster

```bash
gcloud container clusters create agentflow --zone asia-south1-a --num-nodes 1 \
  --machine-type e2-medium --disk-type pd-balanced --disk-size 20 --release-channel regular   # ~4 min
gcloud container clusters get-credentials agentflow --zone asia-south1-a   # kubectl context gke_<project>_<zone>_agentflow
kubectl get nodes -o wide
gcloud compute instances list   # the same node, as a Compute Engine VM
```

The control plane (API server, etcd, scheduler, controller managers, cloud controller manager, PD CSI controller) runs in a Google-owned project and never shows up as pods or VMs. Only the node VM, its boot disk and what the cluster creates for us (load balancer, data disk) are in our project.

`e2-medium` is small: of its 2 vCPU only 940m is allocatable, and GKE's own pods (logging, metrics, DNS, CSI node plugin, kube-proxy) request most of it. With agentflow's 250m request the node was at 878m (93%). That is enough for one agentflow pod at a time, which is all `Recreate` needs; a RollingUpdate would not fit.

### Kustomize layout

```
k8s/
  base/                       the manifests from Phase 1+2 (ConfigMap, PVC, Deployment, Service, Secret example)
  overlays/
    kind/kustomization.yaml   base as is
    gke/
      kustomization.yaml      image -> Artifact Registry + SHA tag
      service-loadbalancer.yaml   Service type LoadBalancer
      pvc.yaml                storageClassName standard-rwo, 10Gi
```

| Overlay | Changes from base | Why |
|---|---|---|
| `kind` | nothing | Renders byte for byte what `k8s/` did before (`kubectl apply -k k8s/overlays/kind`). |
| `gke` | `image: agentflow:dev` -> `asia-south1-docker.pkg.dev/.../agentflow:<sha>` | GKE nodes pull from a registry; there is no `kind load`. |
| `gke` | Service `ClusterIP` -> `LoadBalancer` | GKE's service controller creates a Google Cloud load balancer with a public IP (see below). |
| `gke` | PVC `standard-rwo`, `10Gi` | A `pd-balanced` persistent disk created by the PD CSI driver. 10Gi is the smallest `pd-balanced` size. The disk is zonal, so the pod can only run in `asia-south1-a`. |

Check what an overlay produces without applying it: `kubectl kustomize k8s/overlays/gke`.

### Secrets

```bash
kubectl create secret generic agentflow-secrets \
  --from-literal=AGENTFLOW_API_KEY="$AGENTFLOW_API_KEY" \
  --from-literal=AGENTFLOW_AUTH_TOKEN="$(cat ~/.agentflow-token)"
kubectl describe secret agentflow-secrets      # shows sizes only, never values
```

Both values go from the shell straight to the API server. Neither the Gemini key nor the token is in any file in the repo. `$(cat ...)` drops the token file's trailing newline, so the Secret holds exactly what clients send.

### Deploy

```bash
kubectl apply -k k8s/overlays/gke
kubectl rollout status deployment/agentflow --timeout=300s
kubectl get service agentflow -w            # wait until EXTERNAL-IP is set (~1 min)
```

What the LoadBalancer Service creates in Google Cloud (names are `a` + the Service UID; observed in this run):

| Resource | Purpose |
|---|---|
| Forwarding rule (regional, EXTERNAL, TCP 8000) | The public IP. Passthrough: packets reach the node still addressed to that IP. |
| Target pool | The node VMs that receive the traffic. |
| HTTP health check `k8s-<id>-node` on port 10256 `/healthz` | kube-proxy's health endpoint on each node ("can this node forward?"), not the app's. |
| Firewall `k8s-fw-<id>` (allow tcp:8000 from 0.0.0.0/0) and `k8s-fw-<id>-deny` | Open the service port to the internet; deny everything else to that IP. |
| Firewall `k8s-<id>-node-http-hc` | Lets Google's health checkers (`35.191.0.0/16`, `130.211.0.0/22`, ...) reach port 10256. |

```bash
gcloud compute forwarding-rules list
gcloud compute target-pools list
gcloud compute firewall-rules list --filter="name~^k8s"
```

The app sees every client as `10.8.0.1` (the node), because with the default `externalTrafficPolicy: Cluster` kube-proxy source-NATs incoming traffic.

### Calling the API

```bash
IP=$(kubectl get service agentflow -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
curl http://$IP:8000/healthz                       # 200, no token needed
curl -X POST http://$IP:8000/chats                 # 401 {"detail":"Missing or invalid bearer token"}

export AGENTFLOW_AUTH_TOKEN="$(cat ~/.agentflow-token)"
curl -X POST -H "Authorization: Bearer $AGENTFLOW_AUTH_TOKEN" http://$IP:8000/chats

timeout 1200 python -m tests.scripts.competitive_analysis_e2e --base-url http://$IP:8000 \
  --answer "features, pricing and integrations for a 20-person eng team" < /dev/null
```

The token protects the API but travels in plain HTTP: anyone on the network path can read it. Fine for a short-lived demo; anything longer needs HTTPS (an Ingress or Gateway with a managed certificate) and a rotated token.

### Updates and rollbacks

```bash
# commit, then build and push a new SHA tag (see "Build and push"), then
kubectl set image deployment/agentflow agentflow=asia-south1-docker.pkg.dev/project-80856c2d-bafd-40a6-8f8/agentflow/agentflow:<new-sha>
kubectl rollout status deployment/agentflow
kubectl rollout history deployment/agentflow
kubectl rollout undo deployment/agentflow       # back to the previous ReplicaSet (previous image)
curl http://$IP:8000/healthz                    # "version" shows which commit is serving
```

Measured in this run (`Recreate`, image already cached on the node): the old pod is killed immediately, the new one is Ready ~10 s later. From outside, `/healthz` failed for ~31 s after the first update and ~11 s after a pod delete; see Known issues for the extra time after Ready.

The data disk stays attached while the replacement pod lands on the same node (always, with one node). It is detached only when no pod on that node uses it: after `kubectl scale deployment/agentflow --replicas=0` Compute Engine showed the disk with no users, and scaling back to 1 attached it again in ~2 s (`SuccessfulAttachVolume` from the attachdetach-controller). The stored session resumed afterwards.

### Scripts

```bash
export AGENTFLOW_API_KEY=...            # in your shell; the token is read from ~/.agentflow-token
deploy/gke-up.sh                        # newest tag in Artifact Registry
deploy/gke-up.sh 044cd10                # a specific tag
deploy/gke-down.sh                      # delete everything billable except the images
deploy/gke-down.sh --delete-image       # also delete the agentflow image (all tags)
```

`deploy/gke-up.sh` assumes gcloud is installed and logged in and the image tag exists in Artifact Registry. It creates the cluster if it does not exist, grants the node service account read access to the repo, gets credentials, creates or updates the Secret (piped into `kubectl apply`, never printed), applies `k8s/overlays/gke` with the requested tag through a temporary overlay (the committed `newTag` is left untouched), waits for the rollout and the external IP, and curls `/healthz`. Re-running it against a running cluster just redeploys.

`deploy/gke-down.sh` first deletes the Service, Deployment and PVC, so the in-cluster controllers remove the load balancer and the data disk, then deletes the cluster (node VM, boot disk, control plane). It then lists clusters, VMs, disks, forwarding rules, target pools and reserved addresses in the project and exits non-zero if any are left. It keeps the Artifact Registry images unless `--delete-image` is passed.

Both scripts use `set -euo pipefail`, read the project, zone and cluster name from variables at the top, pass `--context gke_<project>_<zone>_<cluster>` to every `kubectl` call (so they cannot hit the kind cluster), and print each step.

### Cost

Approximate on-demand prices for `asia-south1` (check the [pricing pages](https://cloud.google.com/pricing) before relying on them):

| Item | Approx. cost |
|---|---|
| GKE cluster management fee | $0.10/h, covered by the free tier credit for one zonal cluster per billing account |
| `e2-medium` node VM | ~$0.04/h |
| Forwarding rule (load balancer) | ~$0.025/h |
| External IPv4 addresses in use (node + LB) | ~$0.005/h each |
| 20 GB boot disk + 10 GB data disk, `pd-balanced` | ~$0.004/h together |
| Artifact Registry storage | first 0.5 GB/month free, then ~$0.10/GB-month; tags share layers |

About $0.08/h, roughly $2/day or $55-60/month if left running. Always run `deploy/gke-down.sh` after a session.

## Known issues

- **SSE stream hangs when the flow fails.** If an LLM call fails for good (e.g. retries exhausted), the output stream never sends a `done` message and the client waits forever. Always use a client-side timeout. Being fixed separately.
- **A pod stopped mid-flow loses that session's conversation.** Observed on kind (pod deleted while a flow was running a web search):
  - The kubelet sends SIGTERM. Uvicorn (PID 1) starts a graceful shutdown (`Shutting down`, `Waiting for connections to close`) but waits for the open SSE stream, which never closes on its own. The flow runs on its own event loop thread and is not cancelled: it kept calling tools for another ~14 s.
  - After `terminationGracePeriodSeconds` (default 30, not set in `deployment.yaml`) the kubelet sends SIGKILL; the pod ended in `Error` 30.5 s after the delete.
  - Conversation history is written only when a flow ends (`finally` in `run_on_channel`), and that never ran: the session directory had `chromadb/` and `skills_*/` but no `conversation_history_*.json`.
  - `POST /chats {"session_id": ...}` still accepts the session (the directory exists), but the agent starts with an empty history: asked what was being compared, it answered "We haven't started a competitive analysis project yet".
  - Not fixed here. Directions: handle shutdown in the FastAPI lifespan (cancel running flows so their `finally` saves history, and close SSE streams), set uvicorn's `--timeout-graceful-shutdown` below the grace period, and/or save history after each turn instead of only at the end.
- **Codespaces: kind pods have no internet access.** The Codespace has two iptables rulesets that the kernel applies together: the current Docker daemon's (nftables, correct) and a leftover `iptables-legacy` one whose `FORWARD` policy is `DROP` and only allows `docker0`. Traffic from the kind bridge network (`br-<id>`) is dropped there, so the node's DNS lookups time out (CoreDNS logs `read udp ...->172.18.0.1:53: i/o timeout`) and LLM calls fail with `ConnectTimeout`. The default bridge works, which is why plain `docker run` (Phase 0) is not affected. Fix: the two `iptables-legacy` rules in "Create the cluster and load the image"; they do not survive a Codespace restart.
- **GKE: external requests fail for a while after the pod is replaced.** After a rollout, the public IP kept refusing some new connections (`curl` exit with no response, TCP handshake never completes) for 20 s to over a minute after the new pod was Ready, while requests from inside the cluster (ClusterIP and LB IP) all succeeded and a later run of 100 new connections from outside succeeded 100/100. Right after a pod replacement the node had a few `SYN_SENT UNREPLIED` conntrack entries for the LB IP, gone 30 s later; kube-proxy runs with `--iptables-min-sync-period=10s`. Likely causes, not confirmed: kube-proxy's sync delay plus stale TCP conntrack entries pointing at the old pod IP. Clients should retry connection errors; a demo should wait ~1 minute after a deploy.
- **GKE: `ImagePullBackOff` with `403 Forbidden` from Artifact Registry.** The nodes pull as the project's default compute service account, which new projects create with no roles. Fixed with `roles/artifactregistry.reader` on the repository (in "One-time setup" and `deploy/gke-up.sh`).
- **GKE: `ProvisioningFailed ... no topology key found for node`** right after cluster creation: the PD CSI driver had not labelled the brand-new node with its zone yet. It retried by itself and the disk was provisioned 17 s later; no action needed.
- **Service links collide with the `AGENTFLOW_` prefix.** For a Service named `agentflow`, Kubernetes injects `AGENTFLOW_PORT=tcp://<ip>:8000`, `AGENTFLOW_SERVICE_HOST`, etc. into every pod in the namespace. The app reads none of these today, but `deployment.yaml` sets `enableServiceLinks: false` so a future `AGENTFLOW_PORT` setting cannot be overridden by accident.
- **Unit tests already failing on `main`/HEAD** (they predate Phase 0 and are not fixed here):
  - `tests/unit/test_agent.py`: `test_rshift_registers_under_default`, `test_sub_rshift_registers_under_named_action`, `test_conditional_transition_rshift_returns_dest_agent`, `test_invalid_action_type_raises_type_error`, `test_parse_llm_output_returns_valid_structure`
  - `tests/unit/test_agent_handoff.py`: `test_trail_is_cleared_between_handoffs`, `test_yield_is_recorded_in_own_history`, `test_recall_store_uses_real_sender_role`, `test_storage_is_scoped_to_runtime_session_id`, `test_resumed_session_restores_conversation_history`, `test_new_session_starts_with_empty_history`
  - `tests/unit/test_skill_retriever.py`: `test_view_with_no_skills_says_so`, `test_view_lists_names_without_touching_active_skill`, `test_get_sets_active_skill_and_returns_confirmation`, `test_get_with_no_match_leaves_active_skill_empty`
  - `tests/unit/test_web_fetch.py`: `test_fetch_returns_clean_text`, `test_fetch_reports_http_errors`, `test_fetch_reports_timeouts`
