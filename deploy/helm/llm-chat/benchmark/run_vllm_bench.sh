#!/bin/bash
# Copyright © Advanced Micro Devices, Inc., or its affiliates.
#
# SPDX-License-Identifier: MIT
#
# Independent cross-check for bench.py: runs the upstream `vllm bench serve`
# client against the same Service, so bench.py's numbers can be compared
# with a second, separately-written measurement tool on an identical
# workload. bench.py is the primary tool; this one exists to catch
# measurement bugs in it.
#
# Runs from a throwaway in-cluster pod (vllm-bench-client-pod.yaml) that
# reuses the AIM vLLM image, since that already ships the `vllm` CLI. The
# client is taskset to CLIENT_CPU_RANGE so it never shares cores with the
# engine under test.
#
# Usage:
#   SERVICE=llm-vllm-96c-llama31-8b ./run_vllm_bench.sh <run-name> [extra vllm bench serve args]
#
# Env vars:
#   SERVICE            Service to target (required)
#   MODEL              HF id used for the client-side tokenizer (default: served model id)
#   INPUT_LEN          random-dataset prompt tokens, before chat template (default: 512)
#   OUTPUT_LEN         generated tokens, forced via --ignore-eos (default: 128)
#   NUM_PROMPTS        (default: 24)
#   CLIENT_CPU_RANGE   (default: 176-191)
#   KEEP_POD           1 = keep the client pod for another run (default: 0)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

NS=aim-demo-standalone
NAME="${1:?usage: SERVICE=<svc> $0 <run-name> [extra vllm bench serve args]}"
shift
: "${SERVICE:?set SERVICE to the Kubernetes Service to benchmark}"

CLUSTER_IP="$(kubectl get svc "$SERVICE" -n "$NS" -o jsonpath='{.spec.clusterIP}')"
PORT="$(kubectl get svc "$SERVICE" -n "$NS" -o jsonpath='{.spec.ports[0].port}')"
TARGET_URL="http://${CLUSTER_IP}:${PORT}"
MODEL="${MODEL:-$(curl -s "$TARGET_URL/v1/models" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])')}"
INPUT_LEN="${INPUT_LEN:-512}"
OUTPUT_LEN="${OUTPUT_LEN:-128}"
NUM_PROMPTS="${NUM_PROMPTS:-24}"
CLIENT_CPU_RANGE="${CLIENT_CPU_RANGE:-176-191}"
KEEP_POD="${KEEP_POD:-0}"

mkdir -p results

if ! kubectl get pod vllm-bench-client -n "$NS" >/dev/null 2>&1; then
  echo "Creating vllm-bench-client pod..." >&2
  kubectl apply -f vllm-bench-client-pod.yaml
  kubectl wait --for=condition=Ready pod/vllm-bench-client -n "$NS" --timeout=180s
fi

echo "=== $NAME: $TARGET_URL model=$MODEL input=$INPUT_LEN output=$OUTPUT_LEN prompts=$NUM_PROMPTS ===" >&2

REMOTE_FILE="${NAME}.json"
kubectl exec -n "$NS" vllm-bench-client -- taskset -c "$CLIENT_CPU_RANGE" vllm bench serve \
  --backend openai-chat \
  --base-url "$TARGET_URL" \
  --endpoint /v1/chat/completions \
  --model "$MODEL" \
  --dataset-name random \
  --random-input-len "$INPUT_LEN" \
  --random-output-len "$OUTPUT_LEN" \
  --num-prompts "$NUM_PROMPTS" \
  --ignore-eos \
  --temperature 0 \
  --num-warmups 2 \
  --percentile-metrics ttft,tpot,itl,e2el \
  --metric-percentiles 50,90,95,99 \
  --save-result \
  --save-detailed \
  --result-dir /tmp \
  --result-filename "$REMOTE_FILE" \
  "$@"

kubectl cp "${NS}/vllm-bench-client:/tmp/${REMOTE_FILE}" "results/${NAME}.json"
echo "Saved results/${NAME}.json" >&2

if [ "$KEEP_POD" != "1" ]; then
  kubectl delete pod vllm-bench-client -n "$NS" --wait=false
fi
