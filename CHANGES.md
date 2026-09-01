# llama.cpp/EPYC Support — Final Changes

## Status summary (as of 2026-08-27)

This repo now has a working llama.cpp-backed AIM engine, built as a real
3-layer image (matching the existing vLLM image pattern), deployable via
the `llm-chat` Helm chart onto a real single-node `kubeadm` cluster (the
only supported target — see `command.md`; an earlier `kind`-based
hand-written-manifest smoke test existed under `deploy/local/` and has been
removed in favor of this one real path). Verified end-to-end on the actual
`sushant` cluster with the model-cache/validation/persistence work below
live: the `model-cache-init` init container downloaded the full
`Qwen/Qwen2.5-0.5B-Instruct-GGUF` repo (~5GB, 9 quant variants) onto a real
`local-path`-backed PVC, `llama-server` started with
`-m .../qwen2.5-0.5b-instruct-q8_0.gguf` (the exact file `gguf_filename`
picked out), and a real `/v1/chat/completions` call returned generated text
(~35 tok/s) through the pod directly; the AIM/llama.cpp pod and an
OpenWebUI pod both reached `1/1 Running`, with the old
`0.1.0`-image pod cleanly rolled off once the new one passed its readiness
probe.

What this does **not** cover yet:
- **No ZenDNN image built or benchmarked yet.** Layer 1
  (`assets/epyc/engines/llamacpp/image/Dockerfile`) now compiles with
  `GGML_ZENDNN=ON` by default, folding in what previously existed only in
  the standalone build (`deploy/epyc-standalone/Dockerfile`) — but it must
  be built *on* real EPYC silicon (`GGML_NATIVE` defaults to on, so the
  binary is tuned to its build host), and no such image has been produced,
  pushed, or measured. Every image deployed so far is the generic-CPU
  build. See `deploy/epyc-standalone/NOTES.md` for what remains deferred
  there.
- **Deployed llama.cpp throughput is unexplained.** The generic-CPU image
  under the nested-K8s deployment measured 0.26 tok/s generating with
  Qwen2.5-0.5B-Instruct Q8_0 on a 4-CPU limit — with `nr_throttled 0` and
  roughly 0.8 CPU of the 4 actually consumed, so it is neither cgroup
  throttling nor a saturated core. Whether the ZenDNN/native rebuild
  resolves it is untested.
- **No auto-computed defaults beyond `HF_TOKEN`.** `LlamaCppEngine.apply_engine_defaults()`
  now plumbs `HF_TOKEN` (see "Resolved" below) but doesn't compute anything
  else, unlike `VllmEngine`'s override (`vllm.py:369`), which auto-injects
  `served-model-name` at launch. Every other value this engine needs (`threads`, `ctx-size`, etc.) has to be hand-set
  in the profile/Helm values and manually kept in sync with pod resource
  limits — nothing is computed or defaulted by the engine itself.
- **Single-node only**, and (on the kubeadm path) no `metrics-server`, so
  `kubectl top nodes/pods` doesn't work.

**Resolved since the summary above was first written:**
- **Native argument validation.** `LlamaCppEngine.ARGS_MODEL` is now
  `LlamaCppEngineArgsModel` (`src/aim_runtime/engines/llamacpp.py`) — a
  hand-authored Pydantic model mirroring `llama-server`'s real CLI surface
  (same non-delegating pattern as `BentomlEngineArgsModel`, since llama.cpp
  has no importable Python CLI parser to delegate to like vLLM's). A bad
  type or an invalid enumerated value (e.g. `numa: bogus-mode`) now fails at
  profile-load time instead of surfacing only as a runtime crash inside the
  pod.
- **AIM-owned model cache.** `LlamaCppEngine` now resolves its model through
  `ModelCacheResolver` via a new `resolve_model_path` hook on `BaseEngine`
  (`src/aim_runtime/engines/base.py`), the same mechanism vLLM/BentoML use,
  instead of bypassing it via `llama-server`'s own `--hf-repo`/`--hf-file`
  downloader. `model_arg` is now `-m` (`assets/epyc/base/llamacpp/config/engines.yaml`).
  Unlike vLLM, a cache miss is a hard error rather than a silent runtime
  download — the model must be pre-staged first
  (`./entrypoint.py download-to-cache`). Multi-quant HF repos (several `.gguf` files as
  siblings, e.g. `Qwen/Qwen2.5-0.5B-Instruct-GGUF`) are handled via a
  `gguf_filename` engine_args hint, consumed and stripped by
  `resolve_model_path` before the command line is built.
- **K8s init-container wiring for that pre-staging step.** The AIM/llama.cpp
  pod's own Deployment template lives in `aimchart-llm`, a chart previously
  pulled fresh from Docker Hub's OCI registry on every `helm dependency
  build` (not tracked as source in this repo) — with no `initContainers` or
  extra-volume-mount hook at all. Vendored it into
  `deploy/helm/llm-chat/aimchart-llm/` as local-path source (see that
  directory's `Chart.yaml` for why, and the top-level `Chart.yaml`'s
  dependency entry, now `repository: file://aimchart-llm`) and added an
  opt-in `modelCacheInit` block to its `templates/deployment.yml`: when
  `llm.modelCacheInit.enabled: true` (set in `values.epyc-llamacpp.yaml`),
  an init container runs `./entrypoint.py download-to-cache` — same image, same
  env, so it resolves the identical profile — before the main container
  starts, into the shared `ephemeral-storage` volume. (First deploy attempt
  used `command: ["aim-runtime", "download-to-cache"]` — `aim-runtime` is
  only a real executable when the package is `pip install`'ed, via its
  `[project.scripts]` entry point; the production image just `COPY`s
  `src/`, it never runs that install step, so the init container hit
  `CrashLoopBackOff` with "executable file not found in $PATH" on the real
  cluster. Fixed to `["./entrypoint.py", "download-to-cache"]`, matching
  how the main container's own `ENTRYPOINT` invokes it.) `AIM_CACHE_PATH` is
  set to `/workload/model-cache` so both containers agree on where the
  model lives (the default, `/workspace/model-cache`, isn't on any mounted
  volume). Off by default (`aimchart-llm/values.yaml`) so vLLM/other engine
  deployments through this chart are unaffected — verified with `helm
  template` that the default (no llamacpp override) and OpenWebUI's own
  Deployment both render with no `initContainers` block. Vendoring means
  this repo no longer auto-tracks AMD's upstream releases of `aimchart-llm`
  — re-sync manually if needed.
- **Real persistent storage, so the pre-staged model survives a full pod
  replacement.** Previously, `values.epyc-llamacpp.yaml` set
  `storageClassName: null` for both charts' storage (no cluster here shipped
  a default StorageClass), which routed through each chart's `emptyDir`
  fallback; even the "persistent" branch of that fallback logic used a
  Kubernetes *generic ephemeral volume* (`ephemeral: volumeClaimTemplate`),
  whose PVC is deleted along with the pod that owns it — so a Deployment
  rollout or node reschedule still lost the pre-staged model. Fixed two
  ways: (1) `command.md`'s cluster bring-up now installs Rancher's
  `local-path-provisioner` (a generic K8s addon, not `kind`-specific) and
  marks its `local-path` StorageClass default — this repo's single-node
  kubeadm cluster has no other dynamic provisioner available; (2) both
  charts' `container.volumes` helper (`_helpers.tpl` /
  `aimchart-llm/templates/_helpers.tpl`) now mounts a real, stably-named
  `PersistentVolumeClaim` (new `templates/pvc.yaml` in each chart, tied to
  release identity via a new `pvc.name`/`aimchart-llm.pvc.name` helper)
  instead of the generic ephemeral volume, whenever `storageClassName` is
  set. `values.epyc-llamacpp.yaml` now sets `storageClassName: local-path`
  for both charts. Verified with `helm template`: the PVC renders with the
  init container's `claimName` matching, and setting `storageClassName: null`
  explicitly still falls back to `emptyDir` with zero PVCs rendered (for
  anyone without a StorageClass at all).
- **Dropped the `kind`-based smoke test.** `deploy/local/` (hand-written
  manifests, no Helm) is deleted; the real single-node `kubeadm` cluster via
  `deploy/helm/llm-chat/` + `command.md` is now the only supported
  deployment target, so there's one path to keep correct instead of two.
- **Gated/authenticated Hugging Face model support.** `LlamaCppEngine` now
  overrides `apply_engine_defaults()` to read `HF_TOKEN` from the process
  environment and inject it as `engine_args["hf-token"]` before
  serialization — unlike vLLM/`huggingface_hub`, `llama-server` has no env
  var convention of its own for gated repos, only an explicit CLI flag.
  This lets the token come from a K8s Secret via the Helm chart's existing
  `env_vars` `secretKeyRef` support (already used for `HF_TOKEN` with other
  engines) instead of being hardcoded into a profile. An explicit
  `hf-token`/`hf_token` already in `engine_args` takes precedence over the
  env var.

For day-to-day cluster operation (health checks, logs, teardown, full
rebuild) see **`command.md`** — that file is the *how*; this one is the
*what changed and why*.

---

## What changed, by area

### New AIM engine: `llamacpp`

- `src/aim_runtime/engines/llamacpp.py` — `LlamaCppEngine`: native arg
  validation via `LlamaCppEngineArgsModel` (hand-authored Pydantic model
  mirroring `llama-server`'s real CLI flags), inherited `--key value` CLI
  serialization, no AITER kernels. Model loading goes through AIM's own
  `ModelCacheResolver` via the new `resolve_model_path` hook (`base.py`) —
  the profile's `engines.yaml` sets `model_arg: -m`, and the engine requires
  the model pre-staged in the cache dir first (see status summary above).
- `src/aim_utils/config_utils.py` — added `"llamacpp"` to
  `NON_VLLM_BASE_TARGET_IDS`, so CI's vLLM-specific base-image smoke test
  doesn't run against this engine's base target.

### New 3-layer image for llama.cpp/EPYC

Same Layer 1 → Layer 2 → Layer 3 pattern already used for vLLM AIM images:

- **Layer 1** (`assets/epyc/engines/llamacpp/image/Dockerfile`): compiles
  `llama-server` from source, generic CPU build.
- **Layer 2** (`assets/epyc/base/llamacpp/`): named base-image target —
  `config.yaml`, `config/engines.yaml` (registers the `llamacpp` engine,
  `model_arg: -m`), empty `profiles/general/` — auto-discovered by
  `enumerate_specialized_base_targets()` and built via
  `docker/Dockerfile.aim-epyc-base`. Both this layer's and Layer 3's
  `config.yaml` point at images genuinely pushed to and pulled from Docker
  Hub (public, under `stpauljackson3/`), layer by layer — not local-only
  tags.
- **Layer 3** (`assets/epyc/aim-smoketest/llamacpp-tiny/`): model-specific
  AIM image config plus the `llamacpp-cpu-int8-tp1-latency` profile,
  targeting `Qwen/Qwen2.5-0.5B-Instruct-GGUF`. `threads: 4` is set
  explicitly in the profile rather than left to `llama-server`'s own
  auto-detection, since under a K8s CPU request/limit,
  `hardware_concurrency()` sees the host's full core count, not the
  cgroup's actual quota, and would oversubscribe.

### Kubernetes deployment via the `llm-chat` Helm chart

- `deploy/helm/llm-chat/` — the upstream `solution-blueprints` `llm-chat`
  chart. All template logic (`templates/*.yaml`) is engine-agnostic as
  shipped — it just runs whatever image it's given — with one fix applied
  on top (below).
- `deploy/helm/llm-chat/templates/_helpers.tpl`, `container.volumes`
  helper — fixed a real bug in the chart's `emptyDir` fallback branch:
  `sizeLimit` was a sibling of `emptyDir:` instead of nested inside it,
  which `kubectl`'s strict decoding rejects (`unknown field
  "spec.template.spec.volumes[0].sizeLimit"`). Now:
  ```yaml
  - emptyDir:
      sizeLimit: {{ .Values.storage.ephemeral.quantity }}
    name: ephemeral-storage
  ```
  This branch only runs when `storageClassName` is falsy — see next item —
  which is why the bug was invisible until this deployment exercised it.
- `deploy/helm/llm-chat/values.epyc-llamacpp.yaml` — the override file for
  this deployment: llama.cpp image reference
  (`docker.io/stpauljackson3/aim-epyc-target-llamacpp-model-aim-smoketest-llamacpp-tiny:0.3.0`
  — Layers 2 and 3 rebuilt/repushed twice this pass as the model-cache and
  init-container work landed and one bug in it was fixed; Layer 1, the
  `llama-server` compile, is unchanged and still `0.1.0`), CPU-only
  resources (`gpus: 0`), env vars, and
  `storage.ephemeral.storageClassName: local-path` (both top-level for
  OpenWebUI and under `llm:` for the llama.cpp pod) — see the "Resolved"
  list in the status summary for the `local-path-provisioner` +
  real-PVC work this depends on.
- `deploy/helm/llm-chat/aimchart-llm/` — the AIM/llama.cpp pod's own chart,
  vendored from AMD's upstream OCI dependency (see its `Chart.yaml`) so
  `templates/deployment.yml` could gain an opt-in `modelCacheInit` init
  container, and `templates/pvc.yaml`/`_helpers.tpl` could gain real
  PVC-backed persistence — see the "Resolved" list in the status summary
  for both. `deploy/helm/llm-chat/Chart.yaml`'s dependency now points at
  `file://aimchart-llm` instead of the OCI registry.

### `kind` smoke test removed

- `deploy/local/` (hand-written Deployment/Service manifests, no Helm, for a
  local `kind` cluster) has been **deleted**. It served its purpose early
  on to de-risk the plumbing without needing a real cluster, but `kind` is
  no longer a supported target — the real single-node `kubeadm` cluster via
  `deploy/helm/llm-chat/` + `command.md` is now the only deployment path,
  and duplicating fixes/config across two paths (as the storage-class work
  below would have required) wasn't worth maintaining a toy cluster
  alongside the real one.

### Standalone Docker build for real EPYC hardware

- `deploy/epyc-standalone/` — a separate, non-Kubernetes proof that
  `llama-server` compiled with `GGML_ZENDNN=ON` works (plain `docker
  build`/`docker run`, Q8_0 quantization since ZenDNN's accelerated path
  only covers FP32/BF16/Q8_0, not FP16). Not the 3-layer production image
  pattern — see its own `NOTES.md` for what's deferred.
- `deploy/epyc-standalone/setup.sh` — rootless Docker Engine install for
  that box from Docker's static binaries under `$HOME`, needing no sudo
  except two one-time prerequisites (`uidmap` package, `/etc/subuid`+
  `subgid` range) that the script detects and prints exact admin commands
  for.

### Operational runbook

- `command.md` — cluster health/info commands, pod/container/log
  inspection, full teardown, and full create-and-redeploy sequences for
  running this stack on a real single-node `kubeadm` cluster — node name,
  CRI socket, CNI, namespace, and release name all specific to that setup.

---

## Where to look next

- **Day-to-day cluster commands**: `command.md`
- **llama.cpp engine internals**: `src/aim_runtime/engines/llamacpp.py`,
  `LLAMACPP_FULL_PROJECT_CHANGES.md`
- **Chart deployment conventions** (`helm template | kubectl apply`
  instead of `helm install`): `deploy/helm/llm-chat/docs/DEPLOYMENT.md`
- **What's tracked vs. gitignored in the chart directory**:
  `deploy/helm/llm-chat/.gitignore` — the chart's own source is tracked;
  the `aimchart-llm` subchart dependency (`Chart.lock`, `charts/*.tgz`) is
  not, and must be re-fetched with `helm dependency build .` on any fresh
  checkout (see `command.md`, "Deploy the AIM app").
