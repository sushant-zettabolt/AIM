# Throwaway artifact, not the real production pattern — see
# LLAMACPP_FULL_PROJECT_CHANGES.md Group 5/6 for the real 3-layer
# (engine image -> docker/Dockerfile.aim-epyc-base -> docker/Dockerfile.aim)
# system this is meant to de-risk, not replace. This is a standalone,
# single-stage image for `docker run`-ing directly on the real EPYC box,
# compiling llama-server from source WITH the ZenDNN backend instead of
# the generic prebuilt binary used in the earlier laptop smoke test.
#
# Build:
#   docker build -t aim-llamacpp-zendnn deploy/epyc-standalone
# (build context must be the repo root — see run.sh)

# ---- build stage --------------------------------------------------------
FROM ubuntu:24.04 AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential git ca-certificates python3-pip pkg-config \
      libcurl4-openssl-dev libomp-dev \
    && rm -rf /var/lib/apt/lists/*

# Ubuntu 24.04 ships a recent-enough GCC (13.x, matches the compiler ZenDNN's
# own official builds use) but its apt cmake can lag the >=3.25 the ZenDNN
# backend's CMakeLists requires — get a current one via pip instead of
# fighting apt package versions.
RUN pip3 install --break-system-packages --no-cache-dir "cmake>=3.28" ninja

ARG LLAMACPP_REF=master
RUN git clone --depth 1 --branch "${LLAMACPP_REF}" https://github.com/ggml-org/llama.cpp.git /opt/llama.cpp
WORKDIR /opt/llama.cpp

# GGML_ZENDNN=ON: pulls in AMD's ZenDNN backend (auto-downloads and builds
# ZenDNN itself as part of this step — no separate ZenDNN build needed, see
# docs/backend/ZenDNN.md). LLAMA_CURL=ON: keeps the -hf runtime-download
# flag working, same as the prebuilt release binary used in the laptop test.
RUN cmake -B build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DGGML_ZENDNN=ON \
      -DLLAMA_CURL=ON \
    && cmake --build build --config Release -j"$(nproc)"

# Deliberately NOT using `cmake --install` here: llama.cpp's install rules
# split binaries into bin/ and shared libs into lib/, and whether the
# installed binary's RPATH correctly points back to ../lib in that layout
# wasn't something I could verify without the real build. build/bin/ is the
# raw build output — binary + every .so it linked, all in one flat
# directory with $ORIGIN-relative loading — which is the exact layout
# already confirmed working with the official prebuilt release tarball in
# the laptop smoke test. Copying that directly is the lower-risk choice.

# ---- runtime stage --------------------------------------------------------
FROM ubuntu:24.04-slim AS final

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONPATH="/workspace/aim-runtime/src" \
    HF_HOME="/workspace/model-cache" \
    HF_HUB_DISABLE_TELEMETRY="1" \
    AIM_ALLOW_GENERAL_PROFILE_FALLBACK="false" \
    AIM_CONFIG_PATH="/workspace/aim-runtime/config" \
    AIM_ACCELERATOR_FAMILY="epyc" \
    AIM_ACCELERATOR_TYPE="cpu" \
    AIM_ENGINE="llamacpp" \
    AIM_ID="aim-smoketest/llamacpp-tiny" \
    AIM_PORT="8000" \
    ZENDNNL_MATMUL_ALGO="1"

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip libgomp1 libcurl4 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# llama-server + every .so it built (libggml-cpu.so, the ZenDNN backend .so,
# etc.) live together under bin/ with $ORIGIN-relative rpaths, same layout
# as the prebuilt release tarball used in the laptop test — copy the whole
# directory rather than guessing which libs are needed.
COPY --from=build /opt/llama.cpp/build/bin/ /opt/llamacpp/
ENV PATH="/opt/llamacpp:${PATH}"

WORKDIR /workspace
COPY src/aim_runtime/ /workspace/aim-runtime/src/aim_runtime/
COPY src/aim_common/ /workspace/aim-runtime/src/aim_common/
COPY requirements/requirements.txt /workspace/aim-runtime/
COPY src/entrypoint.py /workspace/entrypoint.py
RUN chmod +x /workspace/entrypoint.py
RUN pip3 install --break-system-packages --no-cache-dir -r /workspace/aim-runtime/requirements.txt "huggingface-hub>=0.34.0"

COPY deploy/epyc-standalone/config /workspace/aim-runtime/config
COPY deploy/epyc-standalone/profiles /workspace/aim-runtime/profiles

RUN mkdir -p /workspace/model-cache

EXPOSE 8000
ENTRYPOINT ["./entrypoint.py"]
