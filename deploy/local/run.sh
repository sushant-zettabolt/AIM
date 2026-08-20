#!/bin/bash
# Throwaway local artifact — see 00-namespace.yaml for context.
#
# Exact from-nothing-to-working command sequence for the kind smoke test.
# Re-runnable: safe to run again against an already-existing cluster/images.
#
# Prereqs on PATH: docker, kind, kubectl. This machine needed
# `sg docker -c "..."` for every docker command because the invoking user
# wasn't in the docker group (no fresh-login usermod applied yet) — drop the
# `sg docker -c` wrapper below if yours doesn't need it. kind/kubectl were
# installed to /workspace/bin in this session; adjust PATH if yours differ.
set -euo pipefail

export PATH="/workspace/bin:$PATH"
CLUSTER=aim-local
NODE="${CLUSTER}-control-plane"
NAMESPACE=aim-demo
AIM_IMAGE=aim-llamacpp-smoketest
UI_IMAGE=ghcr.io/open-webui/open-webui:main
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

dockerc() { sg docker -c "$*"; }

echo "==> 1. Create the kind cluster (skip if it already exists)"
if ! kind get clusters | grep -q "^${CLUSTER}\$"; then
  dockerc "docker info --format '{{.MemTotal}} bytes, {{.NCPU}} CPUs'" # sanity check before creating
  kind create cluster --name "$CLUSTER"
else
  echo "cluster '$CLUSTER' already exists, skipping create"
fi
kubectl wait --for=condition=Ready node --all --timeout=90s --context "kind-${CLUSTER}"

echo "==> 2. Sideload both images into the kind node (no registry)"
# kind load docker-image can fail on images with a locally-cached multi-platform
# manifest index (newer Docker Engine / containerd image store) with:
#   "ctr: content digest sha256:...: not found"
# because it passes --all-platforms to `ctr images import` and chokes on a
# platform/attestation manifest whose blobs aren't fully present locally. Hit
# this on ghcr.io/open-webui/open-webui:main. Workaround: docker save -> cp
# into the node -> `ctr images import` WITHOUT --all-platforms.
load_image() {
  local image="$1"
  if dockerc "kind load docker-image '$image' --name '$CLUSTER'"; then
    return 0
  fi
  echo "kind load docker-image failed for $image (likely the multi-platform-index bug) — falling back to manual ctr import"
  local tar="/tmp/$(echo "$image" | tr '/:' '__').tar"
  dockerc "docker save '$image' -o '$tar'"
  # /tmp inside the kind node is a tmpfs that silently drops docker cp'd files
  # in this environment (docker cp reports success, file never lands) — use
  # /root instead.
  dockerc "docker cp '$tar' '${NODE}:/root/import.tar'"
  dockerc "docker exec '$NODE' ctr --namespace=k8s.io images import --digests --snapshotter=overlayfs /root/import.tar"
  rm -f "$tar"
}

load_image "$AIM_IMAGE"
dockerc "docker pull --platform linux/amd64 '$UI_IMAGE'"
load_image "$UI_IMAGE"

echo "==> 3. Apply manifests"
kubectl apply -f "$HERE"

echo "==> 4. Wait for both pods to be Ready (AIM can take several minutes — GGUF download + model load)"
kubectl -n "$NAMESPACE" rollout status deployment/aim-llamacpp --timeout=600s
kubectl -n "$NAMESPACE" rollout status deployment/openwebui --timeout=180s
kubectl -n "$NAMESPACE" get pods -o wide

echo "==> 5. Verify pod-to-pod: curl the AIM Service from inside the UI pod"
UI_POD=$(kubectl -n "$NAMESPACE" get pod -l app=openwebui -o jsonpath='{.items[0].metadata.name}')
kubectl -n "$NAMESPACE" exec "$UI_POD" -- curl -s http://aim-llamacpp.${NAMESPACE}.svc.cluster.local:8000/v1/models
echo

echo "==> 6. Port-forward the UI (Ctrl+C to stop) and open http://localhost:8080"
echo "    Sign up (first account becomes admin — WEBUI_AUTH=False hides the login"
echo "    wall but OpenWebUI's own API still requires a real user session/token,"
echo "    see NOTES.md), pick the Qwen model, and send a chat message."
kubectl -n "$NAMESPACE" port-forward svc/openwebui 8080:8080
