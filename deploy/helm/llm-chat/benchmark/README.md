<!--
Copyright © Advanced Micro Devices, Inc., or its affiliates.

SPDX-License-Identifier: MIT
-->

# Benchmarking the AIM LLM deployments

One Python script, one YAML file, one Markdown report.

```bash
cd deploy/helm/llm-chat/benchmark
python3 bench.py run -c bench_config.yaml          # full run, report printed at the end
python3 bench.py run -c bench_quick.yaml           # ~2 min/target smoke test of the harness
python3 bench.py run -c bench_config.yaml --dry-run
python3 bench.py run -c bench_config.yaml --targets vllm-96c --scenarios concurrency prefix_cache
python3 bench.py compare results/<run-a> results/<run-b> -o compare.md   # head-to-head across runs
python3 bench.py report results/<run>               # re-render a report
```

Needs only Python 3.8+ and PyYAML (already on the cluster host) plus `kubectl`
when a target is given as a Kubernetes Service. Run it from the host while the
deployment is up.

Each run writes `results/<timestamp>-<config>/`:

| File | Contents |
|---|---|
| `report.md` | Human-readable report: glance table, head-to-head, per-run details, measurement checks, metric definitions |
| `summary.csv` | One row per target × scenario × sweep point, for spreadsheets |
| `summary.json` | Everything in the report, machine-readable |
| `requests.jsonl` | Every measured request: timestamps, token counts, inter-token gaps, server timings |
| `config.yaml`, `run.log` | Exactly what ran |

## Configuring a benchmark

Everything lives in the YAML (see comments in [bench_config.yaml](bench_config.yaml)):

- **`client.cpu_affinity`** pins the load generator to cores the engine under test
  isn't using. The 96-core single-instance engines use 0-95, the 10-instance
  Qwen deployment uses 96-175, so the default is 176-191.
- **`targets`** are OpenAI-compatible endpoints. `kubernetes: {namespace, service}`
  resolves the ClusterIP and the pods behind it (for per-pod vLLM `/metrics`), or
  give `base_url` (+ `headers`, e.g. `Host: llm.local` for the Traefik Ingress).
  Unreachable targets are skipped and listed in the report, so both engines can
  stay in one config even though only one fits on the cores at a time.
- **`defaults`** / **`scenarios`**: prompt and output length, shared-prefix size and
  cache-hit rate, closed loop (N users, think time, ramp-up, request/time limit) or
  open loop (request rate, burstiness, max concurrency). Any parameter given as a
  list becomes a sweep.

A typical engine comparison, since both engines need the same 96 cores:

```bash
# vLLM up (values.epyc-vllm-96core-llama31-8b.yaml)
python3 bench.py run -c bench_config.yaml --targets vllm-96c
# swap to llama.cpp (values.epyc-llamacpp-96core-llama31-8b.yaml)
python3 bench.py run -c bench_config.yaml --targets llamacpp-96c
python3 bench.py compare results/*-vllm-run results/*-llamacpp-run -o compare.md
```

## What gets measured, and how it's kept honest

Per request (client side, `time.perf_counter()`, streaming):

- **Prompt size and generated size** from the server's own `usage` report.
- **TTFT**, **E2E**, **TPOT** = (E2E − TTFT)/(tokens − 1), **ITL** between token chunks.
- **Decode speed** (tokens/s one user sees) and **effective prefill speed**
  (prompt tokens / TTFT, a lower bound because TTFT includes queueing).

Server side, for the true prefill/decode split:

- **vLLM**: deltas of `/metrics` counters and histograms from every pod
  (prefill, decode, queue, TTFT, prefix-cache hits, preemptions), plus
  running/waiting/KV-cache gauges sampled every 2s.
- **llama.cpp**: `timings` from each response (`prompt_n/prompt_ms` =
  uncached prefill speed, `predicted_n/predicted_ms` = decode speed, `cache_n`).

Every distribution reports n, mean, std, min, p50 (with bootstrap 95% CI),
p90, p95, p99 and max. Percentiles use the same definition as numpy and
`vllm bench serve` (checked by `python3 test_bench_stats.py`). A percentile
computed from too few samples to mean anything (p90 < 10, p95 < 20, p99 < 100
samples) is marked `*` — size `num_requests` accordingly.

Controls that make runs comparable across engines:

- **Exact prompt sizes.** Prompts are built from filler words the target's own
  tokenizer encodes as one token each; the chat-template overhead is measured with
  a real request; warmup requests re-verify the size. Both engines therefore see
  exactly `input_tokens` even though their chat templates differ.
- **Exact output sizes.** `ignore_eos: true` forces every request to generate
  `output_tokens`, so engines do identical work.
- **Warmup** before every run (excluded), which also warms the shared prefix so
  each sweep point starts from the same cache state. Miss requests get a unique
  prefix of the same length.

Each run ends with **measurement checks** in the report:

| Check | Catches |
|---|---|
| request errors | failures silently shrinking the sample |
| output / prompt length | a server ignoring `ignore_eos` or a mis-sized prompt |
| one token per stream chunk | ITL being per chunk rather than per token |
| server request count / prompt tokens / generated tokens | other traffic on the server, or a missed pod |
| client vs server TTFT | client-side timing overhead (normally a few ms) |
| client CPU headroom | the load generator itself becoming the bottleneck |
| achieved concurrency | closed-loop users not actually overlapping |
| sample size | percentiles that aren't statistically meaningful |

## Independent cross-check

`run_vllm_bench.sh` runs upstream `vllm bench serve` (a separately written
client) against the same Service from an in-cluster pod. Run an equivalent
workload through both tools to confirm `bench.py`'s numbers:

```bash
SERVICE=llm-vllm-96c-llama31-8b INPUT_LEN=512 OUTPUT_LEN=128 NUM_PROMPTS=24 \
  ./run_vllm_bench.sh xcheck --request-rate inf --max-concurrency 4
```

## Things to know about this cluster

- **Prefix caches are per instance.** Behind a Service (kube-proxy random
  selection) or Traefik (round robin), a repeated prefix only hits if it
  lands on a pod that already cached it, so with 10 replicas the cache
  benefit is diluted unless routing is cache-aware. Measured on the
  10-instance Qwen deployment: 20 requests sharing 4 prefixes got 0 cached
  tokens. Single-instance targets don't have this problem.
- **llama.cpp's `--parallel` is a hard concurrency ceiling.** Requests
  beyond the slot count queue inside llama-server, so TTFT jumps sharply
  once users > slots. vLLM batches continuously and degrades more gradually.
- **Chat templates differ between engines** (vLLM's Llama 3.1 template adds a
  date header), which is why prompt sizes are calibrated per target.
