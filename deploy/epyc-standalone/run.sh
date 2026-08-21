#!/bin/bash
# Standalone Docker run on the real EPYC box — not Kubernetes, not the
# 3-layer production image pattern. See Dockerfile for why.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

echo "==> 0. Sanity checks before spending 10-20 min building"
echo "--- CPU model ---"
grep -m1 "model name" /proc/cpuinfo
echo "--- BF16 hardware support (only matters if you switch the profile to bf16 later) ---"
if grep -qo "avx512bf16" /proc/cpuinfo; then
  echo "avx512bf16: present — BF16 will get real ZenDNN acceleration on this box"
else
  echo "avx512bf16: NOT present — BF16 would silently fall back to plain CPU here; the"
  echo "  profile in this dir uses Q8_0 instead, which ZenDNN accelerates regardless"
fi
echo "--- docker sees ---"
nproc
free -h | head -2

echo "==> 1. Build (compiles llama.cpp from source with ZenDNN — first build"
echo "        auto-downloads+builds ZenDNN too, expect 10-20+ min on 32 cores)"
docker build -t aim-llamacpp-zendnn -f "$HERE/Dockerfile" "$REPO_ROOT"

echo "==> 2. Run"
docker rm -f aim-llamacpp-zendnn-test 2>/dev/null || true
docker run -d --name aim-llamacpp-zendnn-test -p 8000:8000 aim-llamacpp-zendnn

echo "==> 3. Confirm the ZenDNN backend actually loaded (not just that the"
echo "        server started — GGML_ZENDNN=ON at build time doesn't guarantee"
echo "        it registered at runtime)"
sleep 2
docker logs aim-llamacpp-zendnn-test 2>&1 | grep -i zendnn || \
  echo "  no 'zendnn' mention yet in logs — check again once the model finishes loading:"
echo "    docker logs aim-llamacpp-zendnn-test | grep -i zendnn"

echo "==> 4. Wait for readiness, then verify"
for i in $(seq 1 60); do
  curl -s -m 2 http://localhost:8000/health | grep -q '"status":"ok"' && { echo "ready"; break; }
  sleep 5
done
curl -s http://localhost:8000/v1/models
echo
echo "==> 5. Real inference request"
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct-GGUF","messages":[{"role":"user","content":"Say hello in one sentence."}],"max_tokens":30}'
echo
