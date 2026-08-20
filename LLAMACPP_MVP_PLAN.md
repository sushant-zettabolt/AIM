# llama.cpp MVP — 3-Day Plan

Target: EPYC (CPU), model-specific profile, GGUF weights provided directly
(no conversion), llama.cpp compiled base image built separately. Goal is
`docker run` (or local `entrypoint.py serve`) starting `llama-server` against
a real GGUF file end-to-end, via `AIM_ID` + `AIM_ENGINE=llamacpp`.

## Definition of done (MVP, not more)

- [ ] `llama-server` starts inside the container and serves the one
      hand-written profile's model.
- [ ] `curl localhost:8000/v1/models` and a `/v1/completions` (or
      `/v1/chat/completions`) call return real output.
- [ ] `entrypoint.py dry-run` and `list-profiles` both work against the new
      profile without crashing.
- [ ] One documented, repeatable command to reproduce the demo.

Explicitly **not** in scope: GGUF conversion, general (non-model-specific)
profiles, LoRA/adapters, engine-args Pydantic validation model, multiple
accelerator families, CI wiring, k8s manifests (aim-engine side).

**Biggest schedule risk**: compiling `llama-server` for EPYC (right CPU
backend/BLAS/march flags) and getting it into an image on a reachable
registry is the least predictable task in this plan. It has no dependency on
the code-side tasks, which is why it's scheduled first and in parallel with
them on Day 1 — if it slips, everything after it slips too.

---

## Day 1

Two independent tracks, run in parallel — neither blocks the other today.

| Time | Task | Duration (pessimistic) |
|---|---|---|
| 09:00 | Add `Engine.LLAMACPP` enum member (`aim_common/object_model.py`) | 15m |
| 09:15 | New `engines/llamacpp.py`: `LlamaCppEngineArgsModel` (pass-through) + `LlamaCppEngine` (`ARGS_MODEL=None` for MVP) | 45m |
| 10:00 | Register in `ENGINE_CLASSES` + `__all__` (`engines/__init__.py`) | 15m |
| 10:15 | `resolve_model_path` hook on `BaseEngine` (identity) + override in `LlamaCppEngine` (glob for single `*.gguf`, raise clearly on 0/2+ matches) + wire into `command_generator.py` | 2h |
| 12:15 | Lunch / natural checkpoint | — |
| 13:00 | Unit tests: engine registration/dispatch, `resolve_model_path` glob logic (1 file / 0 files / 2 files) | 1.5h |
| 14:30 | Buffer — pydantic quirks, import cycles, existing test suite breakage from touching `command_generator.py` | 2h |
| 16:30 | Checkpoint: `aim-runtime dry-run` against a fake `engine: llamacpp` profile (local dir, one dummy `.gguf`-named file) produces `llama-server -m <path> --host 0.0.0.0 --port 8000 ...` | 30m |
| — | *(parallel, all day)* Compile `llama-server` for EPYC (CPU backend, OpenMP/BLAS flags) | half day+, treat as open-ended |
| — | *(parallel)* Push to a reachable registry | 1-2h, includes auth/network friction |
| — | *(parallel)* Standalone sanity check: run the binary outside AIM against any GGUF file, confirm `/v1/models` responds | 1h |

**Hard gate**: the 16:30 dry-run checkpoint must pass before Day 2 starts —
it proves the plumbing without needing the compiled binary. If it doesn't
pass by EOD, Day 2 opens with finishing it, not with integration.

**If the compile track isn't done by EOD**: that's expected often enough to
plan for. Day 2 opens with finishing it before any integration step, and Day
3's buffer absorbs the resulting pressure.

---

## Day 2

Assumes both Day 1 tracks landed. If either didn't, spend the first block
finishing it — don't integrate against an unfinished half.

| Time | Task | Duration (pessimistic) |
|---|---|---|
| 09:00 | *(if needed)* Finish whatever didn't land on Day 1 | up to 2h, cuts into the rest of the day |
| 09:00/11:00 | `assets/epyc/base/llamacpp/config/engines.yaml` + `config.yaml` + `profiles/general/.gitkeep` | 30m |
| +30m | Hand-write the model-specific profile YAML (HF GGUF repo/file, cores/threads, ctx-size) | 1h |
| +1h | Point `PARENT_REGISTRY_PREFIX/PARENT_REPOSITORY/PARENT_TAG` + `AIM_BASE_CONFIG_DIR/AIM_BASE_PROFILES_DIR` build args at the compiled image + new assets dir, build the Layer-2 AIM base image | 1.5h |
| +1.5h | First `docker run` with `AIM_ID` + `AIM_ENGINE=llamacpp` | 1h, expect at least one failed attempt |
| +1h | Debug pass — budgeted explicitly, not folded into buffer | 2h |
| +2h | Buffer | 2h |

**Known likely failure points, check in this order before treating anything
as a mystery bug:**
1. Missing `AIM_ENGINE=llamacpp` → clear `ValueError` naming available
   engines, not a mystery.
2. `llama-server` not on `PATH` in the final image → `shutil.which` failure
   at exec time — confirm the compiled image installs it somewhere on PATH,
   not just in a build stage that got discarded.
3. GGUF repo has more than one `.gguf` file → the glob-based
   `resolve_model_path` raises on purpose; pick a single-file repo or
   hardcode the exact filename for the demo (see cut list).
4. `host: 0.0.0.0` missing from `engine_args` → server starts but is
   unreachable from outside the container.
5. HF download slow/large → eats wall-clock time; kick off the download
   early in the day, not right before the demo.

**EOD checkpoint**: `curl /v1/models` returns the model, `/v1/completions`
(or chat) returns real generated text. Landing only "container starts,
`/v1/models` returns 200" by EOD is an acceptable fallback checkpoint —
completions working is the stretch goal for today, hard requirement for Day
3 morning.

---

## Day 3

Entire day is buffer first, polish second. Do not start new scope in the
morning if Day 2's checkpoint wasn't hit — finish Day 2 first.

| Time | Task | Duration (pessimistic) |
|---|---|---|
| 09:00 | Finish/stabilize whatever didn't work by EOD Day 2 | up to half the day |
| (after stabilizing) | Re-run `dry-run`, `list-profiles`, full serve end-to-end at least twice from a clean container | 1h |
| +1h | Write the exact reproducible demo command sequence, then verify by running it fresh | 30m |
| +30m | Minimal doc note on known MVP limitations (single-file GGUF repo requirement, model-specific only, no validation model, EPYC-only) | 1h |
| +1h | Run existing test suite to confirm no regressions from touching `command_generator.py` | 30m |
| remainder | Buffer / demo rehearsal — assume something that worked yesterday breaks during rehearsal | rest of day |

**EOD Day 3 = ship the demo as-is.** No scope-creep into the general
profile, the validation model, or a second accelerator family this week —
capture those as a short "next steps" list instead of attempting them.

---

## Cut list — de-scope options if behind by end of Day 2

In priority order, cut these before cutting "completions actually return
text":

1. Skip the glob-based `resolve_model_path` — hardcode the exact `.gguf`
   filename as a constant in `LlamaCppEngine` for the demo model only. Saves
   ~1.5h; reintroduce the general version as a fast-follow.
2. Skip unit tests for the new engine — rely on the manual dry-run/serve
   checkpoints instead. Saves ~1.5h; real risk (regressions won't be caught
   automatically), acceptable for a 3-day MVP.
3. Skip `config.yaml` / CI-facing metadata correctness (only consumed by CI
   tooling not run this week) — just get the Docker build working with
   manually-supplied `--build-arg` values.
4. Skip `host: 0.0.0.0` container-network testing beyond localhost — demo
   from inside the container/pod network if external routing has problems.

Do **not** cut: the `AIM_ENGINE=llamacpp` env var (nothing works without
it), or the `host: 0.0.0.0` engine_arg (silent failure mode, hard to debug
live during a demo).
