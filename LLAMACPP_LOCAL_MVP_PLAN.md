# llama.cpp — Local Smoke-Test Plan (Phase 0, no EPYC needed)

Companion to `LLAMACPP_MVP_PLAN.md` (EPYC-first 3-day schedule) and
`LLAMACPP_FULL_PROJECT_CHANGES.md` (full change list, all 9 groups). This doc
covers a **new Phase 0** that fits into the wait for EPYC access: get the real
`llama-server` binary talking to real AIM code, end-to-end, on a laptop/WSL2,
with a tiny model, no Docker image build required.

**Status: Phase 0 executed and verified end-to-end, both bare-process and
containerized** (2026-08-20). The 3 code
changes below are live in this working tree, `list-profiles` / `dry-run` /
`serve` all ran successfully against a local profile, and a real
`/v1/chat/completions` call returned generated text from
`Qwen/Qwen2.5-0.5B-Instruct-GGUF` (fp16 — see note below on why not bf16).
The `existing test suite (153 tests under tests/aim_runtime/engines +
tests/aim_common) still passes; the 11 failures/5 errors elsewhere in the
full suite are pre-existing missing-dependency issues (`pandas`, `semver`,
`vllm`) unrelated to this change.

**Note on "bf16":** no dedicated bf16 GGUF is published for this model —
the official `Qwen/Qwen2.5-0.5B-Instruct-GGUF` repo ships `fp16`
(unquantized) plus the usual `Q*` quants, no `bf16`-tagged file. Used `fp16`
instead (`precision: fp16`, an exact match to the existing `Precision` enum,
no `variant` bucketing needed).

---

## The discovery that changes the design: `llama-server` downloads GGUF itself

`llama-server` has a built-in `-hf <user>/<model>[:quant]` flag (aliases
`-hfr`/`--hf-repo`, plus `-hff/--hf-file` to pin an exact file and
`-hft/--hf-token` for gated repos) that downloads straight from the Hugging
Face Hub and caches under `HF_HOME` (default `~/.cache/huggingface`) — the
same cache convention `huggingface_hub` and `ModelCacheResolver` already use.
No AIM-side download step, no pre-staging a `.gguf` file. Quant defaults to
`Q4_K_M` if you don't specify one. Confirmed against the current
`tools/server/README.md` in `ggml-org/llama.cpp` (see Sources).

**Why this matters for the plan:** `LLAMACPP_FULL_PROJECT_CHANGES.md` Group 3
identifies the real structural gap — `ModelCacheResolver.resolve_model_path()`
(`src/aim_runtime/model_cache_resolver.py:54-85`) always returns a **directory**,
but `llama-server -m` needs one specific `.gguf` **file**, so a new
`resolve_model_path` hook + glob-for-`*.gguf` + a `gguf_filename` engine_args
convention was proposed to bridge that gap. **For Phase 0, we don't need any
of that.** If the profile's `model_arg` is left empty, `CommandGenerator`
never touches `-m` or the resolver's directory path at all — it just
serializes whatever's in `engine_args` and hands it to `llama-server`,
including `hf-repo`. Confirmed by reading the exact branch:

```python
# src/aim_runtime/command_generator.py:166-170
if self.engine.model_arg:
    command_list = launch + [self.engine.model_arg, model_path] + args_list
else:
    command_list = launch + args_list
```

and `model_arg` is a plain string read from `engines.yaml` (`EngineConfig.model_arg: str = ""`,
`src/aim_runtime/engine_config.py:37`), with the empty string already
documented as the "model embedded in launch" case
(`assets/instinct/base/config/engines.yaml:12` — *"Omit or set to \"\" for
engines that embed the model in launch"*). `-hf` fits that description
exactly — the model source is a launch argument, not a path AIM resolves.

Net effect: **Group 3's glob/`gguf_filename` design is deferred to Phase 1**
(useful later for offline/pre-staged/air-gapped EPYC deployments), not needed
to get a real request through `llama-server` today.

---

## Phase 0 file changes (3 code files, 0 Docker, 0 image build)

| # | File | Action | What |
|---|---|---|---|
| 1 | `src/aim_common/object_model.py:40` | UPDATE | Add `LLAMACPP = "llamacpp"` to the `Engine` `StrEnum` (currently `BENTOML = "bentoml"` is the first member at this line; keep alphabetical-ish grouping or just append). |
| 2 | `src/aim_runtime/engines/llamacpp.py` | CREATE | `LlamaCppEngine(BaseEngine)` — no fields overridden except what's needed: `ARGS_MODEL = None` (skip native validation for now, same MVP choice as the original plan), `ARGS_FORMAT` left at the inherited default `EngineArgsFormat.STANDARD` (`engines/base.py:54`), `requires_aiter_kernels` left at the inherited default `False` (`engines/base.py:55`). This class body can be nearly empty — everything it needs it inherits. |
| 3 | `src/aim_runtime/engines/__init__.py` | UPDATE | Import `LlamaCppEngine`, add `Engine.LLAMACPP: LlamaCppEngine` to `ENGINE_CLASSES` (`engines/__init__.py:44-48`), add `"LlamaCppEngine"` to `__all__`. |

**Explicitly not touched for Phase 0:** `command_generator.py`,
`model_cache_resolver.py`, `engines/base.py` (no new `resolve_model_path`
hook needed — that's the whole point of using `-hf`). This is a smaller
diff than the original 3-day plan's Day 1 because the hardest unknown
(file-vs-directory resolution) isn't on the critical path anymore.

No code touches `assets/epyc/...` — Phase 0 config lives outside the repo's
asset tree (see next section), since it's a personal local test, not a
shippable profile yet.

---

## Local config + profile (not code — files you create on your machine)

`AIMConfig.from_environment()` hardcodes the search roots — there's no env
var to override them:

```python
# src/aim_runtime/config.py:26-28
DEFAULT_PROFILE_BASE_PATH = "/workspace/aim-runtime/profiles"
DEFAULT_CONFIG_PATH = "/workspace/aim-runtime/config"
```
```python
# src/aim_runtime/config.py:340-341 (inside from_environment())
profile_base_path=DEFAULT_PROFILE_BASE_PATH,
config_path=DEFAULT_CONFIG_PATH,
```

So the fastest path is a **one-time** `sudo mkdir -p /workspace && sudo
chown $(whoami) /workspace` on the WSL2 box (harmless, nothing else on this
machine uses `/workspace`), then treat it like a normal writable dev
directory. (Alternative if you'd rather not touch `/`: run inside a throwaway
Docker container with `-v $(pwd)/local-workspace:/workspace`. Slower
edit/test loop since Phase 0 code changes wouldn't hot-reload across a
container boundary without also mounting `src/`. Recommending the `sudo
mkdir` route for Phase 0 since it's a pure Python process — `python
src/entrypoint.py serve` — with instant iteration.)

Layout to create:

```
/workspace/aim-runtime/config/engines.yaml
/workspace/aim-runtime/profiles/aim-smoketest/llamacpp-tiny/profile.yaml
```

`engines.yaml`:
```yaml
llamacpp:
  launch: llama-server
  model_arg: ""
```

`profile.yaml` (aim_id `aim-smoketest/llamacpp-tiny` → org/model split by
`ProfileSelector._build_search_paths()`, `src/aim_runtime/profile_selector.py:202-210`,
so the directory name must match):
```yaml
aim_id: aim-smoketest/llamacpp-tiny
model_id: aim-smoketest/llamacpp-tiny
metadata:
  engine: llamacpp
  accelerator_type: cpu
  accelerator_model: CPU        # the sentinel AcceleratorModel.CPU (object_model.py:136) —
                                  # what a non-EPYC CPU actually detects as; omitting this
                                  # causes an accelerator_mismatch against the detected value
  accelerator_count: 0          # not tensor-parallel degree for CPU; 0 is fine, nothing selects on it locally
  precision: fp16                # or int4 + variant: q4-k-m for a quantized file — see LLAMACPP_FULL_PROJECT_CHANGES.md Group 4
  metric: latency
  manual_selection_only: false  # true also works, but then requires AIM_PROFILE_ID to be set
                                  # explicitly to the profile's profile_id — simpler to leave false locally
  type: unoptimized
engine_args:
  hf-repo: "Qwen/Qwen2.5-0.5B-Instruct-GGUF"
  hf-file: "qwen2.5-0.5b-instruct-fp16.gguf"   # or use hf-repo: "...GGUF:Q4_K_M" for a quantized file
  host: "0.0.0.0"
  ctx-size: 2048
  threads: 4
env_vars: {}
```

Notes:
- `model_id` is required by `ModelProfileData` (`object_model.py:489-493`) and
  is used for `served-model-name` even though `-m` isn't — set it to
  something readable, it doesn't have to be a real HF id.
- `accelerator_model` matters in practice, unlike the original draft assumed:
  `ProfileSelector._assess_profile_compatibility` (`profile_selector.py:441-449`)
  rejects a profile as `accelerator_mismatch` if `metadata.accelerator_model`
  doesn't equal the detected value, and a non-AMD-EPYC CPU detects as the
  sentinel `AcceleratorModel.CPU`, not `None` — confirmed by running this
  exact profile.
- `--port` is injected automatically by `CommandGenerator` regardless of what's
  in `engine_args` (`command_generator.py:156`); `host: 0.0.0.0` is not
  automatic and must stay in the profile or the server binds to `127.0.0.1`
  only (this is called out as a "do not cut" item in `LLAMACPP_MVP_PLAN.md`'s
  cut list too).
- `env_vars: {}` is required as a key even if empty — `ModelProfileData`
  requires the field (`object_model.py:485`).

---

## Model choice for the smoke test

`ggml-org/Qwen2.5-0.5B-Instruct-GGUF` (quant `Q4_K_M`) — official `ggml-org`
quantized upload, ~400MB, instruct-tuned so `/v1/chat/completions` gives a
sane reply, and 0.5B is light enough to run comfortably on a laptop with
default settings. `TinyLlama-1.1B` is a fine fallback if you want a second
data point. Both are small enough that `-hf`'s download is fast even on a
mediocre connection.

---

## Step-by-step execution checklist (what "execute" will do)

1. Add `Engine.LLAMACPP` (file 1 above).
2. Create `engines/llamacpp.py` (file 2).
3. Register in `ENGINE_CLASSES` / `__all__` (file 3).
4. Install `llama-server` locally — prebuilt release tarball
   (`llama-*-bin-ubuntu-x64.tar.gz` from the `ggml-org/llama.cpp` GitHub
   releases page) is the fast path for WSL2 x86_64, no compiling. Verify with
   `llama-server --help | grep -- '-hf'` that the binary was built with
   libcurl support — if it's missing, the prebuilt release wasn't built with
   `LLAMA_CURL`/`GGML_CURL` on and we'd need to build from source with that
   flag enabled instead (fallback, not expected to be needed).
5. `sudo mkdir -p /workspace && sudo chown $(whoami) /workspace`, then create
   `engines.yaml` and `profile.yaml` as above.
6. Put `llama-server` and the repo's `src/` on `PATH`/`PYTHONPATH`, set:
   ```
   AIM_ID=aim-smoketest/llamacpp-tiny
   AIM_ENGINE=llamacpp
   AIM_ACCELERATOR_TYPE=cpu
   AIM_PORT=8000
   ```
7. `python src/entrypoint.py list-profiles` — confirm the profile is
   discovered and doesn't get skipped by the per-file WARN+skip in
   `ProfileRegistry.discover_and_validate` (`profile_registry.py:77-80`).
8. `python src/entrypoint.py dry-run` — confirm the rendered command is
   `llama-server --hf-repo ggml-org/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M --host 0.0.0.0 --ctx-size 2048 --threads 4 --port 8000 ...` with no `-m` flag. This is
   the safe checkpoint (no download yet, no `shutil.which` check).
9. `python src/entrypoint.py serve` — first run downloads the GGUF via
   `llama-server`'s own `-hf` handling, then starts serving.
10. From another shell: `curl localhost:8000/v1/models`, then a real
    `curl localhost:8000/v1/chat/completions -d '{...}'` and confirm
    generated text comes back.
11. Note anything surprising (flag name mismatches, download path, timing) to
    fold back into `LLAMACPP_MVP_PLAN.md`/`LLAMACPP_FULL_PROJECT_CHANGES.md`
    before Phase 1.

Optional, not blocking: extend `tests/aim_runtime/engines/test_engines.py`'s
`TestFactory` parametrize lists with `(Engine.LLAMACPP, LlamaCppEngine)` (per
`LLAMACPP_FULL_PROJECT_CHANGES.md` Group 9) so the new registration is
covered by the existing suite.

---

## Container verification (also done, still no EPYC/K8s)

Confirmed the same plumbing works inside Docker, not just as a bare process.
Built a standalone throwaway Dockerfile (not the production
`docker/Dockerfile.aim-cpu-base` + `docker/Dockerfile.aim` two-layer system —
that needs a PARENT image with `llama-server` compiled for EPYC, which is
Phase 1) that mirrors the production image's shape: same `ENV` names
(`AIM_CONFIG_PATH`, `AIM_ID`, `AIM_ENGINE`, etc.), same `COPY` layout for
`src/aim_runtime`/`src/aim_common`/`entrypoint.py`, same
`ENTRYPOINT ["./entrypoint.py"]`, `EXPOSE 8000`. Substituted the identical
generic x86_64 prebuilt `llama-server` release binary used in the bare-process
test in place of a real EPYC-compiled one.

`docker run -p 8001:8000 aim-llamacpp-smoketest` came up, `llama-server`
downloaded the GGUF via `-hf`, and `/v1/models` + `/v1/chat/completions`
both responded correctly through the container's published port — same
result as the bare-process run.

Two sandbox-specific build issues, unlikely to recur in a real CI/registry
environment, worth remembering if they do: this environment's Docker build
network 403s on plain-HTTP `apt-get` (fixed by rewriting
`/etc/apt/sources.list.d/*.sources` to `https://`), and `python:3.11-slim`
doesn't ship `libgomp1` (`libgomp.so.1`), which `llama-server`'s CPU backend
needs at load time — a real EPYC image (Phase 1) built from a fuller base or
one that already includes an OpenMP runtime won't hit either issue.

---

## Deferred to Phase 1 (when EPYC access lands)

Everything in `LLAMACPP_FULL_PROJECT_CHANGES.md` that Phase 0 doesn't touch:

- **Group 3** — `resolve_model_path` glob + `gguf_filename` convention, for
  pre-staged/offline models where `-hf` runtime download isn't acceptable
  (air-gapped prod, reproducible builds, avoiding a cold-start download on
  every pod restart). `-hf` and a pre-staged `-m` path aren't mutually
  exclusive as a long-term design — a profile could declare either, per the
  server's own `-m`/`-hf` flags being independent-but-not-simultaneous.
- **Group 5/6** — real `assets/epyc/base/llamacpp/` layout, Layer 1 Dockerfile
  compiling `llama-server` for EPYC (CPU backend/BLAS/march flags — the
  "biggest schedule risk" called out in `LLAMACPP_MVP_PLAN.md`), Layer 2 AIM
  base image build.
- **Group 6** — `NON_VLLM_BASE_TARGET_IDS` update (`src/aim_utils/config_utils.py:37`).
- **Group 2** — a real typed `LlamaCppEngineArgsModel` (Phase 0 keeps
  `ARGS_MODEL = None`, permissive pass-through).
- **Group 9** — CI wiring (lives outside this repo).

Phase 0 exists to de-risk the parts that *don't* need EPYC hardware or a
compiled CPU-optimized binary: enum/registry wiring, profile schema shape,
and confirming `llama-server`'s CLI surface and OpenAI-compatible endpoints
behave the way the plan assumes — before spending time on the EPYC compile,
which is the actual bottleneck.

---

Sources:
- [llama.cpp server README (tools/server/README.md)](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
- [New in llama.cpp: Model Management](https://huggingface.co/blog/ggml-org/model-management-in-llamacpp)
- [ggml-org/Qwen2.5-Coder-0.5B-Q8_0-GGUF](https://huggingface.co/ggml-org/Qwen2.5-Coder-0.5B-Q8_0-GGUF)
