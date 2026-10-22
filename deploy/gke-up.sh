#!/usr/bin/env bash
# Bring agentflow up on GKE with one command (see docs/deployment.md, "Phase 3: GKE").
#
#   deploy/gke-up.sh            # deploy the most recently pushed image tag
#   deploy/gke-up.sh f541b88    # deploy a specific tag (a short git commit SHA)
#
# Assumes: gcloud is installed and logged in, the APIs are enabled, the image tag is
# already in Artifact Registry, AGENTFLOW_API_KEY is set in the environment and the
# bearer token is in ~/.agentflow-token. Safe to re-run: existing pieces are reused.
# Creates billable resources (node VM, disks, load balancer); tear down with deploy/gke-down.sh.
set -euo pipefail

PROJECT_ID=project-80856c2d-bafd-40a6-8f8
REGION=asia-south1
ZONE=asia-south1-a
CLUSTER=agentflow
REPO=agentflow
IMAGE_NAME=agentflow
MACHINE_TYPE=e2-medium
DISK_SIZE_GB=20
TOKEN_FILE="$HOME/.agentflow-token"

IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/$IMAGE_NAME"
KCTX="gke_${PROJECT_ID}_${ZONE}_${CLUSTER}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

step() { echo; echo "==> $*"; }
die() { echo "error: $*" >&2; exit 1; }

step "Checking prerequisites"
[ -n "${AGENTFLOW_API_KEY:-}" ] || die "AGENTFLOW_API_KEY is not set"
[ -s "$TOKEN_FILE" ] || die "$TOKEN_FILE is missing or empty"
gcloud config set project "$PROJECT_ID" --quiet >/dev/null 2>&1
gcloud auth print-access-token >/dev/null 2>&1 || die "gcloud is not logged in (gcloud auth login --no-launch-browser)"

if [ $# -ge 1 ]; then
  TAG="$1"
else
  # newest tagged version; an image with several tags prints them comma-separated
  TAG=$(gcloud artifacts docker images list "$IMAGE" --include-tags --filter="tags:*" \
    --sort-by="~UPDATE_TIME" --limit=1 --format="value(tags)" | cut -d, -f1)
  [ -n "$TAG" ] || die "no tagged image found in $IMAGE"
fi
gcloud artifacts docker images describe "$IMAGE:$TAG" >/dev/null 2>&1 || die "image $IMAGE:$TAG not found"
echo "image: $IMAGE:$TAG"

step "Cluster $CLUSTER in $ZONE"
if gcloud container clusters describe "$CLUSTER" --zone "$ZONE" >/dev/null 2>&1; then
  echo "exists, reusing it"
else
  gcloud container clusters create "$CLUSTER" --zone "$ZONE" --num-nodes 1 \
    --machine-type "$MACHINE_TYPE" --disk-type pd-balanced --disk-size "$DISK_SIZE_GB" \
    --release-channel regular
fi

step "Allowing the node service account to pull from Artifact Registry"
# New projects no longer give the default compute service account any roles, so
# without this the kubelet gets 403 Forbidden pulling the image. Idempotent.
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
gcloud artifacts repositories add-iam-policy-binding "$REPO" --location "$REGION" \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role=roles/artifactregistry.reader >/dev/null
echo "ok"

step "Getting cluster credentials (kubectl context $KCTX)"
gcloud container clusters get-credentials "$CLUSTER" --zone "$ZONE"

step "Creating or updating Secret agentflow-secrets"
# The manifest goes through a pipe into the API server; it is never printed or written.
kubectl --context "$KCTX" create secret generic agentflow-secrets \
  --from-literal=AGENTFLOW_API_KEY="$AGENTFLOW_API_KEY" \
  --from-literal=AGENTFLOW_AUTH_TOKEN="$(cat "$TOKEN_FILE")" \
  --dry-run=client -o yaml | kubectl --context "$KCTX" apply -f -

step "Applying k8s/overlays/gke with tag $TAG"
# A throwaway overlay on top of the gke overlay that only overrides the tag, so the
# committed kustomization.yaml is left untouched. It sits next to the gke overlay
# because kustomize refuses absolute paths in resources.
TMP=$(mktemp -d "$ROOT/k8s/overlays/.gke-tag-XXXXXX")
trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../gke
images:
  - name: $IMAGE
    newTag: "$TAG"
EOF
kubectl --context "$KCTX" apply -k "$TMP"

step "Waiting for the rollout (pod Ready)"
kubectl --context "$KCTX" rollout status deployment/agentflow --timeout=300s

step "Waiting for the load balancer's external IP"
IP=""
for _ in $(seq 60); do
  IP=$(kubectl --context "$KCTX" get service agentflow -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
  [ -n "$IP" ] && break
  sleep 5
done
[ -n "$IP" ] || die "no external IP after 5 minutes (kubectl describe service agentflow)"
echo "external IP: $IP"

step "Health check: curl http://$IP:8000/healthz"
# a fresh forwarding rule can take a few seconds before it answers
for _ in $(seq 24); do
  curl -fsS --max-time 5 "http://$IP:8000/healthz" && echo && break
  sleep 5
done || die "/healthz did not answer"

echo
echo "Deployed $IMAGE:$TAG at http://$IP:8000"
echo "Call the API with:  -H \"Authorization: Bearer \$(cat ~/.agentflow-token)\""
