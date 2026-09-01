# Copyright © Advanced Micro Devices, Inc., or its affiliates.
#
# SPDX-License-Identifier: MIT

# Layer 1 (engine image) for llama.cpp/EPYC — LLAMACPP_FULL_PROJECT_CHANGES.md
# Group 6. Auto-discovered by enumerate_specialized_base_targets()
# (src/aim_utils/specialized_utils.py) purely by living at
# assets/<accelerator>/engines/<engine>/image/Dockerfile.
#
# Provides only `llama-server` on PATH, plus the python3/pip toolchain that
# docker/Dockerfile.aim-epyc-base (Layer 2) assumes is already on its parent
# image — mirrors how the vLLM Layer 1 image
# (docker.io/amdih/zentorch_vllm_upstream, see assets/epyc/base/config.yaml)
# ships python+vllm pre-installed. No AIM runtime code, no ENTRYPOINT: Layer 2
# owns both.
#
# ZenDNN-enabled by default (GGML_ZENDNN below). Earlier revisions of this
# file shipped a generic CPU build because no EPYC host was available to
# compile on; that is no longer true, and the generic build measured
# 0.26 tok/s for a 0.5B Q8_0 under the nested-K8s deployment. Build this on
# real Zen hardware — see deploy/epyc-standalone/Dockerfile, the throwaway
# single-stage predecessor this folds back into the real 3-layer pattern.
#
# Note GGML_NATIVE stays at llama.cpp's default (ON), i.e. -march=native, so
# the resulting binary is tuned for -- and only portable to -- the machine
# that compiled it. That is intended for a per-cluster EPYC image; it does
# mean the image is not portable to a different microarchitecture.
#
# apt note: ubuntu:24.04's default sources.list.d/ubuntu.sources points at
# plain-HTTP archive.ubuntu.com/security.ubuntu.com, which some egress paths
# 403 (observed consistently on the build host used for this image, even
# though a plain curl to the identical URL succeeds — looks like a
# fingerprint/rate-limit on apt's specific request pattern, not a real
# outage). HTTPS to the same mirrors works fine; the only wrinkle is the
# base image ships no CA bundle yet, so ca-certificates itself needs one
# bootstrap install with peer verification off (apt still validates the
# .deb via its own signed Release/hash chain regardless of TLS trust — this
# is the standard bootstrap pattern, not a security shortcut on the package
# contents). Every apt-get after that runs fully verified.

FROM ubuntu:24.04 AS build

RUN sed -i 's|http://archive.ubuntu.com|https://archive.ubuntu.com|g; s|http://security.ubuntu.com|https://security.ubuntu.com|g' \
      /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get -o Acquire::https::Verify-Peer=false update \
    && apt-get -o Acquire::https::Verify-Peer=false install -y --no-install-recommends ca-certificates \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential git python3-pip pkg-config \
      libcurl4-openssl-dev libssl-dev libomp-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --break-system-packages --no-cache-dir "cmake>=3.28" ninja

ARG LLAMACPP_REF=master
RUN git clone --depth 1 --branch "${LLAMACPP_REF}" https://github.com/ggml-org/llama.cpp.git /opt/llama.cpp
WORKDIR /opt/llama.cpp

# LLAMA_CURL=ON alone isn't enough for --hf-repo/-hf: llama.cpp's own repo
# metadata/commit-resolution client (get_repo_commit, separate from curl's
# actual file download) needs a TLS backend explicitly, and silently
# disables itself at configure time if it can't find one rather than
# failing the build -- hence libssl-dev above and -DLLAMA_OPENSSL=ON here,
# spelled out instead of relying on autodetection.
# GGML_ZENDNN=ON pulls in AMD's ZenDNN backend; the backend's CMakeLists
# downloads and builds ZenDNN itself as part of this step (see llama.cpp's
# docs/backend/ZenDNN.md), which is why the pip cmake>=3.28 above matters --
# apt's cmake can lag the >=3.25 that CMakeLists requires. Exposed as a
# build arg so the image can still be produced without ZenDNN
# (--build-arg GGML_ZENDNN=OFF) on a non-Zen builder.
ARG GGML_ZENDNN=ON
RUN cmake -B build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DGGML_ZENDNN=${GGML_ZENDNN} \
      -DLLAMA_CURL=ON \
      -DLLAMA_OPENSSL=ON \
    && cmake --build build --config Release -j"$(nproc)"

# build/bin/ (binary + every .so it linked, $ORIGIN-relative rpaths) copied
# whole rather than cmake --install, matching the layout already confirmed
# working in deploy/epyc-standalone/Dockerfile and the earlier laptop smoke
# test's prebuilt release tarball.

FROM ubuntu:24.04 AS final

RUN sed -i 's|http://archive.ubuntu.com|https://archive.ubuntu.com|g; s|http://security.ubuntu.com|https://security.ubuntu.com|g' \
      /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get -o Acquire::https::Verify-Peer=false update \
    && apt-get -o Acquire::https::Verify-Peer=false install -y --no-install-recommends ca-certificates \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      python3 python3-pip libgomp1 libcurl4 libssl3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/llama.cpp/build/bin/ /opt/llamacpp/
ENV PATH="/opt/llamacpp:${PATH}"
# Unlike the official prebuilt release tarballs (or the ZenDNN build in
# deploy/epyc-standalone/Dockerfile), a plain `cmake -B build` invocation
# doesn't set an $ORIGIN RPATH on the binaries -- llama-server can't find
# its sibling libllama-server-impl.so etc. without this.
ENV LD_LIBRARY_PATH="/opt/llamacpp:${LD_LIBRARY_PATH}"
# docker/Dockerfile.aim-epyc-base (Layer 2, "No content change" per
# LLAMACPP_FULL_PROJECT_CHANGES.md Group 6) runs a plain `pip3 install -r
# requirements.txt`, no --break-system-packages. Ubuntu 24.04's pip refuses
# that by default (PEP 668); the real vLLM Layer 1
# (docker.io/amdih/zentorch_vllm_upstream) evidently doesn't hit this, so
# it's this new Layer 1's job to make plain pip3 installs work, not Layer
# 2's Dockerfile to change.
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# requirements/epyc-requirements.txt (shared with the vLLM/EPYC path, not
# llamacpp-specific -- not ours to edit) has no huggingface-hub entry
# because on that path it arrives transitively via the vLLM parent image's
# own deps. aim_runtime/model_storage.py imports it unconditionally
# regardless of engine, so any Layer 1 that isn't the vLLM image has to
# provide it directly -- deploy/epyc-standalone/Dockerfile already hit and
# worked around this identical gap the same way.
RUN pip3 install --no-cache-dir "huggingface-hub==0.36.2"

# Matches deploy/epyc-standalone/Dockerfile's runtime env. Only meaningful
# when the ZenDNN backend is compiled in (GGML_ZENDNN=ON above); harmless
# otherwise, since a non-ZenDNN llama-server never reads it. Overridable per
# deployment through the chart's env_vars if a sweep wants to compare
# matmul algorithms.
ENV ZENDNNL_MATMUL_ALGO="1"
