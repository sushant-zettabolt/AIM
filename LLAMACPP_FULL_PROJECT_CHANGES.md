# llama.cpp — Full Project File Change List

Scope: this repo (aim-build) only. Target accelerator family: EPYC (CPU),
per the scope established for this work; the pattern generalizes to
Instinct/Radeon trivially (they already carry the same commented-out
`llamacpp:` stanza in their `engines.yaml`, see Group 5) but that is not
included below unless you want it added.

Grouped per the feature list. Each entry: repo, path, CREATE/UPDATE, summary,
and status. All paths are relative to `aim-build/` unless marked otherwise.

---

## 1. Engine registration and adapter — DONE

| Repo | Path | Action | Summary |
|---|---|---|---|
| aim-build | `src/aim_common/object_model.py` | DONE | `Engine.LLAMACPP = "llamacpp"` added to the `Engine` StrEnum. |
| aim-build | `src/aim_runtime/engines/llamacpp.py` | DONE | `LlamaCppEngine(BaseEngine)` — `requires_aiter_kernels = False`, `ARGS_FORMAT = EngineArgsFormat.STANDARD`. Holds `resolve_model_path` (Group 3). |
| aim-build | `src/aim_runtime/engines/__init__.py` | DONE | `LlamaCppEngine`/`LlamaCppEngineArgsModel` imported, `Engine.LLAMACPP: LlamaCppEngine` in `ENGINE_CLASSES`, both names in `__all__`. |
| aim-build | `src/aim_runtime/engines/base.py` | DONE | `resolve_model_path(self, resolved: ResolvedModelPath, engine_args: dict) -> str` hook added, default `return resolved.path` (identity — every other engine's behavior is unchanged). |
| aim-build | `src/aim_runtime/command_generator.py:_build_command_list` | DONE | `model_path = self.engine.resolve_model_path(resolved_model, engine_args)` replaces the direct `resolved_model.path` read. `ModelCacheResolver.resolve_model_path()` (`model_cache_resolver.py:54-85`) returns a **directory**; `llama-server -m` needs one specific `.gguf` **file** — this hook is where that gap closes, per-engine. |

## 2. Argument mapping — DONE

| Repo | Path | Action | Summary |
|---|---|---|---|
| aim-build | `src/aim_runtime/engines/llamacpp.py` | DONE | `LlamaCppEngineArgsModel(EngineArgsModel)` — a fully-typed field list mirroring `llama-server`'s real CLI surface (host/port/ctx-size/threads/n-gpu-layers/numa/split-mode/cache-type-k/v/rope-*/lora/etc., with `Literal` constraints on the small number of enumerated-choice flags), set as `ARGS_MODEL`. No native Python parser to delegate to (same structural situation as `BentomlEngineArgsModel`, `engines/bentoml.py`), so validation is Pydantic-only; `extra="allow"` still passes through an unmodeled-but-real flag rather than rejecting it outright. |
| aim-build | `assets/epyc/base/llamacpp/config/engines.yaml` | DONE | `model_arg: -m` (was `""`). |

## 3. Model artifact resolution (GGUF single-file vs HF repo) — DONE

| Repo | Path | Action | Summary |
|---|---|---|---|
| aim-build | `src/aim_runtime/engines/llamacpp.py` | DONE | `LlamaCppEngine.resolve_model_path()` requires `resolved.is_local_dir` (i.e. the model was pre-staged via `./entrypoint.py download-to-cache`) and raises a clear error naming the required command if not — no fallback to `llama-server`'s own HF downloader. Within the cached directory: globs for `*.gguf`; single match → use it; zero or 2+ → raises naming the directory and what was found. |
| aim-build | `src/aim_runtime/engines/llamacpp.py` | DONE | **Multi-quant HF repos** (e.g. `bartowski/...-GGUF`, `Qwen/Qwen2.5-0.5B-Instruct-GGUF`) ship several quantizations as sibling `.gguf` files, which breaks the "exactly one match" glob. Resolved via a `gguf_filename` engine_args key (declared as a typed field on `LlamaCppEngineArgsModel`) that `resolve_model_path` pops before serialization and uses to pick the exact file, falling back to the single-file glob when absent. |
| aim-build | `src/aim_runtime/model_cache_resolver.py` | No change | Stays engine-agnostic — it keeps returning a directory; the file-selection logic lives in the engine (Group 1's hook), so vLLM/BentoML/vLLM-Omni are unaffected. |
| aim-build | `deploy/helm/llm-chat/aimchart-llm/templates/deployment.yml` | DONE | Pre-staging (`./entrypoint.py download-to-cache`) now runs in an opt-in `modelCacheInit` init container before the main container starts, sharing the `ephemeral-storage` volume. Required vendoring `aimchart-llm` (previously a pure OCI dependency with no `initContainers` hook) into this repo as local-path source — see Group 7. `values.epyc-llamacpp.yaml` sets `llm.modelCacheInit.enabled: true` and `AIM_CACHE_PATH: /workload/model-cache`. Doesn't survive a full pod replacement (needs a real PVC, not the chart's `emptyDir`/generic-ephemeral-volume choice) — tracked in `CHANGES.md`. |

## 4. Precision / quantization — DONE (bucketing approach, no enum change)

| Repo | Path | Action | Summary |
|---|---|---|---|
| aim-build | `src/aim_common/object_model.py` (`Precision` enum) | DONE, no enum change | GGUF quant types don't map 1:1 onto `Precision` (`fp4/fp8/fp16/fp32/bf16/int4/int8`). Bucketed into the nearest existing value (`Q8_0 → int8`) and the exact quant string carried in `metadata.variant` (`object_model.py`) — see `assets/epyc/aim-smoketest/llamacpp-tiny/profiles/llamacpp-cpu-int8-tp1-latency.yaml` (`precision: int8`, `variant: q8-0`). |
| aim-build | `src/aim_runtime/profile_selector.py` (`precision_priority` dict) | No change (verified) | Unmapped precisions fall back to `UNKNOWN_PRIORITY`. Low-risk as-is since llama.cpp/EPYC profiles don't compete for selection against GPU-precision profiles. |
| aim-build | `docs/metadata_overview.md` | Still open | Document the quant-bucketing + `variant` slug convention so future profile authors don't reinvent it per-model. |

## 5. Profile assets — DONE

| Repo | Path | Action | Summary |
|---|---|---|---|
| aim-build | `assets/epyc/base/llamacpp/config/engines.yaml` | DONE | Named-base engines.yaml declaring `llamacpp:` (`launch: llama-server`, `model_arg: -m`). |
| aim-build | `assets/epyc/base/llamacpp/config.yaml` | DONE | `base_image:` block pointing at the Layer 1 compiled-llama.cpp image (Group 6). |
| aim-build | `assets/epyc/base/llamacpp/profiles/general/` | DONE | Empty general-profiles dir (model-specific only; no general profile shipped yet). |
| aim-build | `assets/epyc/aim-smoketest/llamacpp-tiny/profiles/llamacpp-cpu-int8-tp1-latency.yaml` | DONE | Model-specific profile: `model_id: Qwen/Qwen2.5-0.5B-Instruct-GGUF` (real HF repo id, needed now that model loading resolves through the cache — `aim_id` stays the catalog identity), `engine_args` (`gguf_filename`, `host: 0.0.0.0`, `ctx-size`, `threads`), `env_vars: {}`. |
| aim-build | `assets/epyc/aim-smoketest/llamacpp-tiny/config.yaml` | DONE | `base_image:` block pointing at the Layer 2 `aim-epyc-llamacpp-base` image. |

## 6. Image build — DONE

| Repo | Path | Action | Summary |
|---|---|---|---|
| aim-build | `assets/epyc/engines/llamacpp/image/Dockerfile` | DONE | Layer 1: compiles/installs `llama-server` (generic CPU build; real EPYC/ZenDNN build is a separate, still-open item — see `CHANGES.md` status summary). Auto-discovered as a CI build target by `enumerate_specialized_base_targets()`. |
| aim-build | `docker/Dockerfile.aim-epyc-base` | No content change | Fully build-arg driven; building the Layer 2 llama.cpp base is a build-invocation concern, not a Dockerfile edit. |
| aim-build | `src/aim_utils/config_utils.py` (`NON_VLLM_BASE_TARGET_IDS`) | DONE | `"llamacpp"` is in this frozenset, so CI's vLLM-specific base-image smoke test doesn't run against this engine's base target. |
| aim-build | `requirements/epyc-requirements.txt` | No change | `llama-server` is a compiled binary, not a pip package. |

## 7. Kubernetes deployment

Largely out of scope for aim-build by design, with one exception: this repo ships no hand-rolled production manifests, and `deploy/helm/llm-chat/` itself is a vendored upstream chart (`solution-blueprints`). Its `aimchart-llm` dependency, though, was pulled fresh from Docker Hub's OCI registry at build time until this pass — now also vendored (`deploy/helm/llm-chat/aimchart-llm/`, local-path dependency) specifically because it needed a real template change (the `modelCacheInit` init container, Group 3) that no values-only override could provide. What deploys today (`deploy/helm/llm-chat/` + `command.md`, verified end-to-end on `kind` and a real `kubeadm` cluster — see `CHANGES.md`) covers the smoke-test path; the still-open pieces (PVC-backed cache persistence across pod replacement, real EPYC/ZenDNN image) are tracked in `CHANGES.md`'s status summary, not here.

Node labeling needs no change: llama.cpp profiles reuse the same `AcceleratorModel`/`AcceleratorType` enum values as vLLM EPYC profiles, so the existing CPU-detection DaemonSet labels match without new code.

## 8. Profile generation and tiering — no change expected

| Repo | Path | Action | Summary |
|---|---|---|---|
| aim-build | `src/aim_utils/profile_utils.py` | No change expected (verify) | Its maintenance commands operate generically over `ProfileMetadata`/YAML and don't hardcode an engine allowlist — new `llamacpp-*` profiles pass through unchanged. |
| — | — | Process note | New profiles should ship `metadata.type: unoptimized` until real benchmarking exists — matches the current `llamacpp-tiny` profile. |

## 9. Tests and CI — PARTIALLY DONE

| Repo | Path | Action | Summary |
|---|---|---|---|
| aim-build | `tests/aim_runtime/engines/test_llamacpp_engine.py` | DONE | `LlamaCppEngineArgsModel` validation (kebab keys, bad type, bad `Literal` choice, extra-key pass-through, `gguf_filename`) and `resolve_model_path` (not-pre-staged raises, `gguf_filename` hit/miss, glob 0/1/2+ matches). |
| aim-build | `tests/aim_runtime/engines/test_engines.py` | DONE | `TestFactory` parametrize lists now include `(Engine.LLAMACPP, LlamaCppEngine)`; class-attribute, serialize-format, and engine-defaults coverage added alongside the vLLM/BentoML cases. |
| aim-build | `tests/aim_utils/test_specialized_utils.py` | **NOT DONE** | Add a case asserting `assets/epyc/engines/llamacpp/image/` is discovered by `enumerate_specialized_base_targets()`. |
| aim-build | `tests/aim_utils/test_image_naming.py` | No change | Already asserts `llamacpp` base naming (radeon-scoped). |
| *(not aim-build)* | CI workflow / pipeline definitions | **Cannot determine from this repo** | No `.github/` or `ci/` directory exists in aim-build — `config_utils.py`'s own comments reference `ci/discover_base_build_targets.py`, which lives in a separate CI/pipeline repo not cloned here. |

---

## Design decisions made (were open, now resolved)

1. **Multi-quant HF repos** (Group 3): the `gguf_filename` engine_args convention — implemented.
2. **Precision bucketing** (Group 4): bucket GGUF quant → nearest existing `Precision` enum value + `variant` slug — implemented (no enum change).
3. **Base layout** (Group 5/6): named base dir (`assets/epyc/base/llamacpp/`) — implemented, matches the existing `vllm-omni`/`bentoml` pattern.
4. **`LlamaCppEngineArgsModel` strictness** (Group 2): a fully-typed field list mirroring `VllmEngineArgsModel`'s role (not a permissive pass-through) — implemented, for validation parity with vLLM's engine.
5. **Cache-miss behavior** (Group 3, new): a model not found in AIM's cache dir is a hard error (pre-staging required via `./entrypoint.py download-to-cache`), not a silent fallback to `llama-server`'s own HF downloader — chosen for reproducibility and to avoid a cold-start download racing the pod's readiness probe on every restart.
