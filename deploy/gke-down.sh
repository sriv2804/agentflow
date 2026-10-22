#!/usr/bin/env bash
# Tear agentflow down on GKE and check that nothing billable is left
# (see docs/deployment.md, "Phase 3: GKE").
#
#   deploy/gke-down.sh                   # keeps the images in Artifact Registry
#   deploy/gke-down.sh --delete-image    # also deletes the agentflow image (all tags)
#
# Exits non-zero if any VM, disk, forwarding rule, target pool or reserved address remains.
set -euo pipefail

PROJECT_ID=project-80856c2d-bafd-40a6-8f8
REGION=asia-south1
ZONE=asia-south1-a
CLUSTER=agentflow
REPO=agentflow
IMAGE_NAME=agentflow

IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/$IMAGE_NAME"
KCTX="gke_${PROJECT_ID}_${ZONE}_${CLUSTER}"

DELETE_IMAGE=false
for arg in "$@"; do
  case "$arg" in
    --delete-image) DELETE_IMAGE=true ;;
    *) echo "usage: $0 [--delete-image]" >&2; exit 2 ;;
  esac
done

step() { echo; echo "==> $*"; }

gcloud config set project "$PROJECT_ID" --quiet >/dev/null 2>&1

if gcloud container clusters describe "$CLUSTER" --zone "$ZONE" >/dev/null 2>&1; then
  step "Deleting the Service and PVC first, so their Google Cloud resources are cleaned up"
  # The load balancer and the data disk are created and deleted by controllers running
  # in the cluster. Deleting the cluster first can leave them behind (still billed).
  gcloud container clusters get-credentials "$CLUSTER" --zone "$ZONE"
  kubectl --context "$KCTX" delete service agentflow --ignore-not-found --wait=true --timeout=300s
  kubectl --context "$KCTX" delete deployment agentflow --ignore-not-found --wait=true --timeout=120s
  kubectl --context "$KCTX" delete pvc agentflow-data --ignore-not-found --wait=true --timeout=120s
  echo "waiting for the dynamically provisioned disk to be deleted"
  for _ in $(seq 24); do
    [ -z "$(kubectl --context "$KCTX" get pv -o name)" ] && break
    sleep 5
  done
  kubectl --context "$KCTX" get pv

  step "Deleting cluster $CLUSTER (node VM, boot disk, control plane; a few minutes)"
  gcloud container clusters delete "$CLUSTER" --zone "$ZONE" --quiet
else
  step "Cluster $CLUSTER not found, skipping deletion"
fi

if $DELETE_IMAGE; then
  step "Deleting image $IMAGE (all tags)"
  gcloud artifacts docker images delete "$IMAGE" --delete-tags --quiet
else
  step "Keeping image $IMAGE (pass --delete-image to delete it)"
fi

step "Checking for leftover billable resources in $PROJECT_ID"
LEFT=0
check() {
  local what="$1"; shift
  local out
  out=$("$@" --format="value(name)" 2>/dev/null || true)
  if [ -n "$out" ]; then
    echo "LEFT  $what:"; echo "$out" | sed 's/^/        /'
    LEFT=1
  else
    echo "none  $what"
  fi
}
check "GKE clusters"         gcloud container clusters list
check "VM instances"         gcloud compute instances list
check "persistent disks"     gcloud compute disks list
check "forwarding rules"     gcloud compute forwarding-rules list
check "target pools"         gcloud compute target-pools list
check "reserved IP addresses" gcloud compute addresses list
echo
echo "Artifact Registry ($REPO, storage billed per GB):"
gcloud artifacts docker images list "$IMAGE" --include-tags --format="table(version.basename(),tags,createTime.date())" 2>/dev/null || true

echo
if [ "$LEFT" -eq 0 ]; then
  echo "Nothing billable left apart from the Artifact Registry images listed above."
else
  echo "Some resources are still there (listed above); delete them or re-run this script."
  exit 1
fi
