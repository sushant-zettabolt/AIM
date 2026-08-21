# Standalone Docker on the real EPYC box — notes

## No root on this machine → rootless Docker via `setup.sh`

`setup.sh` installs Docker Engine from the official static binary tarballs
(`docker-*.tgz` + `docker-rootless-extras-*.tgz`) in rootless mode, entirely
under `$HOME` — no `sudo` anywhere in the script. Two things it genuinely
cannot do without root, checked up front rather than failing mid-download:

1. `newuidmap`/`newgidmap` (the `uidmap` package) must already be installed.
2. Your user needs a range in `/etc/subuid` and `/etc/subgid`.

Many distros/cloud images set both up automatically per-user at account
creation — if so, the script needs zero root involvement and just works. If
not, it prints the exact one-time commands for whoever has root, and stops
rather than guessing around it.

```bash
./deploy/epyc-standalone/setup.sh
# then open a new shell (or `source ~/.bashrc`) so PATH/DOCKER_HOST stick
./deploy/epyc-standalone/run.sh
```

Rootless mode has one real-world caveat worth knowing about, not just a
theoretical one: the storage driver defaults to `vfs` (slower, more disk
use per layer) unless `fuse-overlayfs` happens to be present on the box,
since overlayfs itself needs root. Fine for a one-off build/run like this;
would matter more for repeated rebuilds.

## Getting the repo onto the new machine

```bash
git clone git@github.com:sushant-zettabolt/AIM.git   # or the https URL
cd AIM
```

That's it for the Docker path below — the Dockerfile builds everything
(llama.cpp+ZenDNN, AIM runtime, dependencies) inside the image, so you don't
need a local Python venv on this machine unless you also want to run
`entrypoint.py` bare (outside Docker) like the laptop test did — if so, see
`LLAMACPP_LOCAL_MVP_PLAN.md`'s Phase 0 steps, same idea, just `pip install -e
".[epyc]"` instead of `.[cpu]`/nothing.

## What changed from the laptop smoke test

1. **`llama-server` is compiled from source with `-DGGML_ZENDNN=ON`**
   instead of downloading the generic prebuilt release binary. That generic
   binary has no ZenDNN backend at all — it was fine for proving the AIM
   plumbing works, but gets zero benefit from EPYC-specific optimization.
2. **The model file changed from fp16 to Q8_0.** ZenDNN only accelerates
   `MUL_MAT`/`MUL_MAT_ID` for FP32, BF16, and Q8_0 — everything else
   (including the fp16 file the laptop test used) falls back to the plain
   CPU backend even with the ZenDNN build. Q8_0 was chosen over BF16 for the
   first test specifically because BF16 acceleration additionally requires
   Zen4/Zen5 hardware (`avx512bf16`) — Q8_0 works regardless, so it's the
   safer thing to point at first. `run.sh` checks for `avx512bf16` and tells
   you whether BF16 is worth switching to afterward.
3. **No Kubernetes.** Plain `docker build` + `docker run`, per your ask —
   the kind/local-cluster work stays in `deploy/local/`, unrelated to this.

## Things worth knowing before running `run.sh`

- **Build time is real**, not laptop-smoke-test fast: ZenDNN's own
  auto-download-and-build step is documented as 5-10 minutes, plus
  llama.cpp's own compile on top. Budget 15-25 minutes for the first
  `docker build`, even with 32 cores.
- **`GGML_ZENDNN=ON` doesn't guarantee the backend is actually used at
  runtime** — it has to register successfully and then actually get picked
  for the matmul ops in your specific model/quant. `run.sh` step 3 greps the
  startup logs for "zendnn" as a first check; if that's inconclusive, the
  clearest signal is a real prompt-processing speed comparison against a
  non-ZenDNN build (not done here — that's a deliberate next step, not
  assumed to already be proven).
- **Known upstream bug, shouldn't apply here but worth knowing:**
  ggml-org/llama.cpp#19134 — a dynamic-linker symbol mismatch
  (`undefined symbol: ggml_get_type_traits_cpu`) when loading the ZenDNN
  backend, but only triggers with `-DGGML_BACKEND_DL=ON` (dynamic backend
  loading), which this Dockerfile doesn't set. If you ever add that flag and
  hit this, the documented workaround is
  `LD_PRELOAD=<path-to>/libggml-cpu.so`. It's also already fixed upstream
  (ggml-org/llama.cpp#19159).
- **NUMA:** not needed here — confirmed single socket, 32 cores. If this
  ever moves to a multi-socket box, wrap the launch with
  `numactl --cpunodebind=0 --membind=0` (would need a small entrypoint
  change since AIM currently execs `llama-server` directly, not through
  `numactl` — flag if that becomes relevant).
- **`ZENDNNL_MATMUL_ALGO=1`** (blocked AOCL DLP algorithm) is set as a
  baked-in `ENV` in the runtime image per ZenDNN's own docs as the
  recommended default — not something you need to set yourself.

## If the container fails to start with a missing shared library error

Same failure mode hit in the laptop smoke test (`llama-server: error while
loading shared libraries: libgomp.so.1: cannot open shared object file`,
fixed there by adding `libgomp1` to the runtime image). Already included
`libgomp1`/`libcurl4`/`ca-certificates` here, but ZenDNN itself might pull in
something not anticipated (e.g. a BLAS/Fortran runtime) since this hasn't
been run on real EPYC hardware yet. If you see `error while loading shared
libraries: <name>.so.N`, the fix is the same pattern: figure out which apt
package ships that `.so` (`apt-cache search`/`apt-file search` on a matching
Ubuntu 24.04 box, or just search the filename), add it to the runtime
stage's `apt-get install` line in the Dockerfile, rebuild.

## Not done here (deliberately, matches the standalone-Docker ask)

- The real 3-layer production image pattern
  (`assets/epyc/engines/llamacpp/image/Dockerfile` as the actual CI-discovered
  engine image, `docker/Dockerfile.aim-epyc-base` on top of it) — this
  Dockerfile is a standalone stand-in for that, not a replacement. Worth
  promoting to the real pattern once this is validated, per
  `LLAMACPP_FULL_PROJECT_CHANGES.md` Group 5/6.
- A real BF16-vs-Q8_0-vs-no-ZenDNN performance comparison.
- Precision bucketing beyond this one profile — same `variant` slug
  convention as the rest of the project (`int8` + `variant: q8-0`).
