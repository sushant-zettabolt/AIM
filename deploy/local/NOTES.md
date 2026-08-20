# Notes from getting this working (local kind smoke test)

Throwaway local artifacts — see `00-namespace.yaml` for scope. These are the
things that needed adjusting versus the plain Docker container test
(`LLAMACPP_LOCAL_MVP_PLAN.md`'s container-verification section), kept here
because they're the parts most likely to bite again on a real cluster later.

## 1. `kind load docker-image` failed on the OpenWebUI image

```
ERROR: failed to load image: command "docker exec --privileged -i aim-local-control-plane
ctr --namespace=k8s.io images import --all-platforms --digests --snapshotter=overlayfs -"
failed with error: exit status 1
Command Output: ctr: content digest sha256:...: not found
```

Cause: this Docker Engine version keeps a locally-pulled multi-platform
manifest index (`linux/amd64,linux/arm64` — visible via
`docker exec <node> ctr --namespace=k8s.io images ls`) rather than collapsing
to one platform. `kind load docker-image` passes `--all-platforms` to `ctr
images import`, which then tries to import referenced platform/attestation
manifests whose blobs aren't fully present in the saved tar, and fails.

The AIM image (`aim-llamacpp-smoketest`, built locally via a plain single-arch
Dockerfile) didn't hit this — only the multi-arch `ghcr.io/open-webui/open-webui`
pull did. `docker pull --platform linux/amd64` did not change anything (image
was already "up to date" for that digest — the daemon still keeps a
multi-platform index).

**Workaround** (see `load_image()` in `run.sh`): `docker save` the image to a
tar, copy it into the kind node container, and run `ctr images import`
directly *without* `--all-platforms`. That succeeds because it only imports
the platform actually needed.

Likely irrelevant on a real cluster with a real registry (nodes pull images
themselves via containerd, not through this docker-save/kind-load path) — but
worth knowing if anyone else hits it building multi-arch images for kind.

## 2. `docker cp` into the kind node's `/tmp` silently drops the file

`docker cp <tar> aim-local-control-plane:/tmp/foo.tar` returned exit 0, but
the file was never there (`ls` inside the container showed nothing, even for
a 5-byte test file). `/tmp` inside the kind node is a `tmpfs,noexec` mount;
`docker cp` to `/root/` instead worked immediately, same command otherwise.
Root cause not fully diagnosed — flagging in case it recurs.

## 3. `WEBUI_AUTH=False` does not disable OpenWebUI's API auth

It hides the login wall/DB bootstrap check, but `/api/models`,
`/api/chat/completions`, etc. still returned `401 Not Authenticated` without a
real bearer token. Worked around it for scripted verification by calling
OpenWebUI's own signup endpoint (`POST /api/v1/auths/signup`) to create the
first (auto-admin) user and using the returned JWT as `Authorization: Bearer
...` for the rest of the check. This matches a known upstream report
(open-webui/open-webui#15254) that the flag doesn't fully disable auth.
Opening the UI in an actual browser and signing up once is the normal path —
not a bug in our setup, just worth knowing before assuming something's
broken.

## 4. Model download takes 1–3+ minutes inside the pod

Same `-hf` runtime-download behavior as the bare container test — nothing
baked into the image, nothing bind-mounted from the host, the pod just needs
outbound network access to huggingface.co. That's why `startupProbe` on the
AIM Deployment has `failureThreshold: 120` at `periodSeconds: 5` (~10 min
ceiling) — a default/tight startup probe would restart-loop the pod before
the download finishes. On a real cluster with a real registry-baked model or
persistent volume this goes away; here it's the deliberate "less work" choice
per the smoke test's own instructions.

## 5. Resource headroom

This laptop's Docker allocation: 16 CPUs / 22.31GiB RAM — comfortably fits
kind's control-plane node + both pods + the ~1.2GB model, no OOMKills
observed. Called out per the original ask to check this before creating the
cluster rather than debugging OOMKills later.
