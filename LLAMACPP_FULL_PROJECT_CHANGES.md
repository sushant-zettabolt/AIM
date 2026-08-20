# llama.cpp — Full Project File Change List

Scope: this repo (aim-build) only. Target accelerator family: EPYC (CPU),
per the scope established for this work; the pattern generalizes to
Instinct/Radeon trivially (they already carry the same commented-out
`llamacpp:` stanza in their `engines.yaml`, see Group 5) but that is not
included below unless you want it added.

Grouped per the feature list. Each entry: repo, path, CREATE/UPDATE, summary.
All paths are relative to `aim-build/` unless marked otherwise.

---

## 1. Engine registration and adapter

| Repo | Path | Action | Summary |
|---|---|---|---|
| aim-build | `src/aim_common/object_model.py:40-42` | UPDATE | Add `LLAMACPP = "llamacpp"` to the `Engine` StrEnum. |
| aim-build | `src/aim_runtime/engines/llamacpp.py` | CREATE | New `LlamaCppEngine(BaseEngine)` — `requires_aiter_kernels = False`, `ARGS_FORMAT = EngineArgsFormat.STANDARD` (already the default; the format's own docstring in `engine_args_models.py:46` names llama.cpp as a STANDARD-format engine). Also holds the new `resolve_model_path` override (Group 3). |
| aim-build | `src/aim_runtime/engines/__init__.py` | UPDATE | Import `LlamaCppEngine`/`LlamaCppEngineArgsModel`, add `Engine.LLAMACPP: LlamaCppEngine` to `ENGINE_CLASSES`, add both names to `__all__`. |
| aim-build | `src/aim_runtime/engines/base.py` | UPDATE | Add a `resolve_model_path(self, resolved: ResolvedModelPath) -> str` hook, default `return resolved.path` (identity — every current engine wants the directory as-is). This is the extension point `LlamaCppEngine` overrides. |
| aim-build | `src/aim_runtime/command_generator.py:_build_command_list` | UPDATE | Replace the direct `model_path = resolved_model.path` with `model_path = self.engine.resolve_model_path(resolved_model)`. This is the real gap: `ModelCacheResolver.resolve_model_path()` (`model_cache_resolver.py:54-85`) returns a **directory**, but `llama-server -m` needs one specific `.gguf` **file**. |

## 2. Argument mapping

| Repo | Path | Action | Summary |
|---|---|---|---|
| aim-build | `src/aim_runtime/engines/llamacpp.py` | CREATE (same file as Group 1) | `LlamaCppEngineArgsModel(EngineArgsModel)` — no fields declared for now (inherits `extra="allow"` + kebab-case alias generator from the shared base), so any `engine_args` key round-trips through kebab/snake normalization without a llama-server-specific field list. Set as `ARGS_MODEL` so calling `validate_engine_args` is a real (if permissive) validation instead of the MVP's `ARGS_MODEL = None` no-op. A fully-typed field list mirroring `VllmEngineArgsModel` (`engines/vllm.py:42-329`) is a later enhancement, not required to ship. |
| aim-build | `assets/epyc/base/config/engines.yaml` (or the new named-base dir, see Group 5) | UPDATE | Uncomment the existing `llamacpp:` stanza — `launch: llama-server`, `model_arg: -m`. Identical commented stanzas already exist in `assets/instinct/base/config/engines.yaml` and `assets/radeon/base/config/engines.yaml` if those families are added later. |

## 3. Model artifact resolution (GGUF single-file vs HF repo)

| Repo | Path | Action | Summary |
|---|---|---|---|
| aim-build | `src/aim_runtime/engines/llamacpp.py` | CREATE (same file) | `LlamaCppEngine.resolve_model_path()` globs the resolved directory for `*.gguf`. Single match → use it. Zero or 2+ matches → raise a clear `ValueError` naming the directory and what was found, rather than a confusing downstream `llama-server` failure. |
| aim-build | `src/aim_runtime/engines/llamacpp.py` | CREATE (same file) | **Design decision needed for multi-quant HF repos**: a real GGUF repo (e.g. `bartowski/...-GGUF`) commonly ships several quantizations as sibling `.gguf` files in one repo, which breaks the "exactly one match" glob. Recommend: profiles declare a non-CLI `engine_args` key (e.g. `gguf_filename`) that `LlamaCppEngine` pops before serialization and uses to pick the exact file, falling back to the single-file glob when absent. This key must be stripped before `serialize_engine_args` runs so it never leaks onto the `llama-server` command line. |
| aim-build | `src/aim_runtime/model_cache_resolver.py` | No change | Stays engine-agnostic — it keeps returning a directory; the file-selection logic belongs in the engine (Group 1's hook), not here, so vLLM/BentoML/vLLM-Omni are unaffected. |

## 4. Precision / quantization

| Repo | Path | Action | Summary |
|---|---|---|---|
| aim-build | `src/aim_common/object_model.py:20-29` (`Precision` enum) | **Decision, likely no enum change** | GGUF quant types (`Q4_K_M`, `Q8_0`, `Q5_K_M`, `F16`, …) don't map 1:1 onto the existing `Precision` enum (`fp4/fp8/fp16/fp32/bf16/int4/int8`), and `precision` is a *required* `ProfileMetadata` field. Recommended: bucket the GGUF quant into the nearest existing `Precision` value (e.g. `Q4_K_M → int4`, `Q8_0 → int8`, `F16 → fp16`) for selection-algorithm purposes, and carry the exact quant string in the already-existing `metadata.variant` field (`object_model.py:347`, a free-form slug already designed for exactly this "keep the filename precise without enum explosion" case). No enum edit needed under this approach. |
| aim-build | `src/aim_runtime/profile_selector.py:223-231` (`precision_priority` dict) | No change (verify) | Unmapped precisions fall back to `UNKNOWN_PRIORITY`, a safe default. Since llama.cpp/EPYC profiles won't compete for selection against GPU-precision profiles, this is low-risk as-is. |
| aim-build | `docs/metadata_overview.md` | UPDATE | Document the quant-bucketing + `variant` slug convention so future profile authors don't reinvent it per-model. |

## 5. Profile assets

| Repo | Path | Action | Summary |
|---|---|---|---|
| aim-build | `assets/epyc/base/llamacpp/config/engines.yaml` | CREATE | Named-base engines.yaml (mirrors `assets/instinct/base/vllm-omni/config/engines.yaml` and `.../bentoml/config/engines.yaml`) declaring only `llamacpp:`. Preferred over reusing the shared `assets/epyc/base/config/engines.yaml`, because `resolve_base_assets_dir()` (`src/aim_utils/specialized_utils.py:240-284`) already resolves named base dirs first — this is the existing, intended extension point, not a workaround. |
| aim-build | `assets/epyc/base/llamacpp/config.yaml` | CREATE | `base_image:` block pointing at the Layer 1 compiled-llama.cpp image (Group 6). |
| aim-build | `assets/epyc/base/llamacpp/profiles/general/.gitkeep` | CREATE | Empty general-profiles dir (required directory shape; no general profile shipped yet — model-specific only, matching current MVP scope). |
| aim-build | `assets/epyc/<org>/<model>/profiles/llamacpp-<accelerator_model_or_none>-<precision>-tp1-<metric>.yaml` | CREATE | The model-specific profile: `aim_id`, `model_id`, `metadata` (`engine: llamacpp`, `accelerator_type: cpu`, `precision`, `metric`, `type: unoptimized`), `engine_args` (`host: 0.0.0.0`, `ctx-size`, `threads`, …), `env_vars`. |
| aim-build | `assets/epyc/<org>/<model>/config.yaml` | CREATE | `base_image:` block pointing at the Layer 2 `aim-epyc-llamacpp-base` image built from the dir above. |

## 6. Image build

| Repo | Path | Action | Summary |
|---|---|---|---|
| aim-build | `assets/epyc/engines/llamacpp/image/Dockerfile` | CREATE | Layer 1: compiles/installs `llama-server` for EPYC (CPU backend, BLAS/march flags). Placing it under `assets/<acc>/engines/<engine>/image/` means `enumerate_specialized_base_targets()` (`src/aim_utils/specialized_utils.py:135-193`) auto-discovers it as a real CI build target — no manual "build and push it yourself" step, unlike the 3-day MVP's shortcut. |
| aim-build | `docker/Dockerfile.aim-epyc-base` | No content change | Already fully build-arg driven (`AIM_BASE_CONFIG_DIR`, `AIM_BASE_PROFILES_DIR`, `PARENT_REGISTRY_PREFIX/REPOSITORY/TAG`). Building the Layer 2 llama.cpp base just means invoking it with those args pointed at the new `assets/epyc/base/llamacpp/` dir and the Layer 1 image — a CI/build-invocation concern, not a Dockerfile edit. The unconditional `VLLM_CPU_OMP_THREADS_BIND=auto` env var it sets is a harmless no-op for llama-server. |
| aim-build | `src/aim_utils/config_utils.py:37` (`NON_VLLM_BASE_TARGET_IDS`) | **UPDATE — easy to miss** | Add `"llamacpp"` to this frozenset. It's the single source of truth gating whether CI runs the generic vLLM model-service smoke test against a base; leaving it out means CI silently tries to smoke-test the llamacpp base as if it were vLLM and fails. |
| aim-build | `requirements/epyc-requirements.txt` | No change | `llama-server` is a compiled binary, not a pip package — nothing to add. |

## 7. Kubernetes deployment

**Out of scope for aim-build by design** — this repo ships no Helm charts and no K8s manifests (confirmed: no `charts/`, no manifest templates anywhere in the tree). Flagging what the *other* repos/artifacts need, since it can't be done here:

| Repo | What | Note |
|---|---|---|
| *(not aim-build)* | Standalone Deployment + Service manifests | Per your plan to hand-write these rather than adapt `aimchart-llm`: readiness/liveness probe path must be `llama-server`'s `/health` (not vLLM's), CPU requests must equal limits (Guaranteed QoS) for pinning, `containerPort: 8000` with the engine bound to `0.0.0.0` (already covered by the `host: 0.0.0.0` engine_arg in Group 5's profile), `imagePullSecrets` for the private registry holding the llama.cpp base/model images built in Group 6. |
| aim-build | `docs/kubernetes_deployment.md` | OPTIONAL, doc-only | This file already documents a plain example `deployment.yaml`/`service.yaml` for illustration (same pattern as `docs/custom_profiles.md`'s K8s section). Could add a llama.cpp variant here purely as documentation — this is not a chart and doesn't conflict with the "no charts in aim-build" boundary. |

Node labeling needs no change: llama.cpp profiles reuse the same `AcceleratorModel`/`AcceleratorType` enum values as vLLM EPYC profiles, so the existing CPU-detection DaemonSet labels match without new code.

## 8. Profile generation and tiering

| Repo | Path | Action | Summary |
|---|---|---|---|
| aim-build | `src/aim_utils/profile_utils.py` | No change expected (verify) | Its maintenance commands (`sync_profiles_with_file`, `clone_profiles`/`clone-profiles-gpu-name`, `check_profile_metadata`) operate generically over `ProfileMetadata`/YAML and don't hardcode an engine allowlist anywhere found — new `llamacpp-*` profiles should pass through unchanged. Worth a smoke-test pass once real profiles exist, not a known required edit. |
| — | — | Process note | New profiles should ship `metadata.type: unoptimized` until real benchmarking exists — `type` gates aim-engine's `selector.minimumType` (default `"optimized"`), so an unoptimized profile won't be auto-selected there, only reachable via explicit `AIM_ID`/custom-profile paths, which matches where this project is today. |
| *(cannot determine from this repo)* | — | — | No benchmarking-driven profile generator/pipeline exists anywhere in aim-build — `profile_utils.py` is sync/maintenance tooling only, not a generator. If such a pipeline exists, it's in a repo not cloned here; flag rather than assume absent. |

## 9. Tests and CI

| Repo | Path | Action | Summary |
|---|---|---|---|
| aim-build | `tests/aim_runtime/engines/test_llamacpp_engine.py` | CREATE | Engine class attributes (`ARGS_MODEL`, `ARGS_FORMAT`, `requires_aiter_kernels`), `resolve_model_path` glob logic (0 / 1 / 2+ `.gguf` files), `gguf_filename` override path if implemented (Group 3). |
| aim-build | `tests/aim_runtime/engines/test_engines.py` | UPDATE | The `@pytest.mark.parametrize` lists in `TestFactory` (`test_engine_class_for`, `test_build_engine_instantiates`, ~lines 44-66) need an explicit `(Engine.LLAMACPP, LlamaCppEngine)` tuple added; `test_every_engine_enum_is_mapped` already iterates `list(Engine)` so it covers the new member automatically once `ENGINE_CLASSES` is updated. |
| aim-build | `tests/aim_utils/test_specialized_utils.py` | UPDATE | Add a case asserting `assets/epyc/engines/llamacpp/image/` (Group 6) is discovered by `enumerate_specialized_base_targets()` once that Dockerfile exists. |
| aim-build | `tests/aim_utils/test_image_naming.py:331-333` | No change | Already asserts `llamacpp` base naming (radeon-scoped) — pre-existing evidence this was anticipated; nothing to add for EPYC unless you want a parallel EPYC-scoped assertion. |
| *(not aim-build)* | CI workflow / pipeline definitions | **Cannot determine from this repo** | No `.github/` or `ci/` directory exists in aim-build at all — `config_utils.py`'s own comments reference `ci/discover_base_build_targets.py`, which lives in a separate CI/pipeline repo not cloned here. Any matrix/workflow wiring needed to actually build the new Layer 1/2/3 images in CI has to be done there. |

---

## Summary of open design decisions (not yet resolved, need your call before implementation)

1. **Multi-quant HF repos** (Group 3): pick the `gguf_filename` engine_args convention above, or restrict to single-file-GGUF repos only for now.
2. **Precision bucketing** (Group 4): bucket GGUF quant → nearest existing `Precision` enum value + `variant` slug (recommended), vs. extending the `Precision` enum with GGUF-native values.
3. **Base layout** (Group 5/6): named base dir (`assets/epyc/base/llamacpp/`, recommended, matches existing `vllm-omni`/`bentoml` pattern) vs. folding into the shared `assets/epyc/base/config/engines.yaml`.
4. **`LlamaCppEngineArgsModel` strictness** (Group 2): permissive pass-through (recommended for v1) vs. a fully-typed field list mirroring `VllmEngineArgsModel`.
