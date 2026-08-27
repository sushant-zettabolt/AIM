# Copyright © Advanced Micro Devices, Inc., or its affiliates.
#
# SPDX-License-Identifier: MIT

"""llama.cpp engine.

Owns two llama.cpp-specific concerns: native argument validation
(:class:`LlamaCppEngineArgsModel`) and resolving a cached model directory down
to the single ``.gguf`` file ``llama-server -m`` needs
(:meth:`LlamaCppEngine.resolve_model_path`). CLI args serialize as standard
``--key value`` flags (``ARGS_FORMAT`` inherited from :class:`BaseEngine`),
and no AITER kernels are needed on CPU (``requires_aiter_kernels`` inherited
as ``False``).

Model loading goes through AIM's own model-cache resolver, the same as
vLLM/BentoML: the profile's ``engines.yaml`` entry sets ``model_arg: -m``, so
``CommandGenerator`` resolves the model id via ``ModelCacheResolver`` and
passes ``-m <path>`` on the command line. Unlike those engines, a bare model
id with no local cache hit is **not** a usable value for llama-server's
``-m`` (it expects a real file path, not a HF repo id string) — a model must
be pre-staged with ``./entrypoint.py download-to-cache`` before serving, and
:meth:`LlamaCppEngine.resolve_model_path` raises a clear error rather than
falling back to llama-server's own ``--hf-repo``/``--hf-file`` downloader.
Pre-staging avoids a cold-start HF download racing the pod's readiness probe
on every restart, and keeps deployments reproducible/air-gap-friendly.
"""

from __future__ import annotations

import glob
import logging
import os
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Optional

from aim_runtime.engines.base import BaseEngine
from aim_runtime.engines.engine_args_models import EngineArgsFormat, EngineArgsModel

if TYPE_CHECKING:
    from aim_runtime.model_cache_resolver import ResolvedModelPath

logger = logging.getLogger(__name__)


class LlamaCppEngineArgsModel(EngineArgsModel):
    """Pydantic model for ``llama-server`` CLI arguments.

    Unlike vLLM, llama.cpp is a C++ binary with no importable Python CLI
    parser to delegate to for authoritative validation — the same structural
    situation as BentoML (see :class:`~aim_runtime.engines.bentoml.BentomlEngineArgsModel`),
    so this uses **Pydantic-only validation**: fields below mirror
    ``llama-server``'s real ``tools/server`` CLI surface (flag names, types,
    and the small number of enumerated-choice flags) so common mistakes —
    wrong type, bad enum value — fail at profile-load time instead of
    surfacing only as an opaque runtime crash inside the pod.

    ``llama-server``'s flag set evolves across releases and this list isn't
    exhaustive; the base model's ``extra="allow"`` means an unmodeled (but
    real) flag still passes through untouched rather than being rejected —
    the same graceful-degradation posture vLLM's model takes when a native
    parser field is missing.
    """

    # Networking / serving
    host: str | None = None
    port: int | None = None
    api_key: str | None = None
    api_key_file: str | None = None
    timeout: int | None = None
    slots: bool | None = None
    no_slots: bool | None = None
    metrics: bool | None = None
    embedding: bool | None = None
    reranking: bool | None = None
    cont_batching: bool | None = None

    # Model loading (HF downloader flags — kept for non-AIM-managed use, e.g.
    # local/manual runs; the AIM-managed cache path uses `model_arg: -m`
    # instead, see module docstring)
    hf_repo: str | None = None
    hf_file: str | None = None
    hf_token: str | None = None
    model_url: str | None = None
    alias: str | None = None
    chat_template: str | None = None
    chat_template_file: str | None = None

    # AIM-side-only convention (not a real llama-server flag): picks the exact
    # .gguf file out of a multi-quant HF repo's cached directory. Popped from
    # engine_args by LlamaCppEngine.resolve_model_path before serialization —
    # see that method — so it never reaches the llama-server command line.
    gguf_filename: str | None = None

    # Context / performance
    ctx_size: int | None = None
    batch_size: int | None = None
    ubatch_size: int | None = None
    n_predict: int | None = None
    threads: int | None = None
    threads_batch: int | None = None
    n_gpu_layers: int | None = None
    main_gpu: int | None = None
    tensor_split: str | None = None
    split_mode: Literal["none", "layer", "row"] | None = None
    parallel: int | None = None
    mlock: bool | None = None
    no_mmap: bool | None = None
    numa: Literal["distribute", "isolate", "numactl"] | None = None
    flash_attn: bool | None = None
    defrag_thold: float | None = None
    cache_type_k: Literal["f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"] | None = None
    cache_type_v: Literal["f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"] | None = None
    rope_scaling: Literal["none", "linear", "yarn"] | None = None
    rope_freq_base: float | None = None
    rope_freq_scale: float | None = None
    yarn_ext_factor: float | None = None
    yarn_attn_factor: float | None = None
    yarn_beta_fast: float | None = None
    yarn_beta_slow: float | None = None
    grp_attn_n: int | None = None
    grp_attn_w: int | None = None

    # LoRA
    lora: str | None = None
    lora_scaled: list[str] | None = None

    # Logging
    verbose: bool | None = None
    log_disable: bool | None = None
    log_file: str | None = None


class LlamaCppEngine(BaseEngine):
    """llama.cpp-backed serving engine (``llama-server``)."""

    ARGS_MODEL: ClassVar[Optional[type[EngineArgsModel]]] = LlamaCppEngineArgsModel
    ARGS_FORMAT: ClassVar[EngineArgsFormat] = EngineArgsFormat.STANDARD

    def resolve_model_path(self, resolved: "ResolvedModelPath", engine_args: dict[str, Any]) -> str:
        """Resolve a cached model directory down to a single ``.gguf`` file.

        Requires the model to already be pre-staged in AIM's cache dir (see
        module docstring); raises rather than falling back to llama-server's
        own HF downloader, so a missing model fails fast and clearly instead
        of the pod cold-starting a download on every restart.

        File selection within the cached directory:
        - If ``engine_args`` has a ``gguf_filename`` hint (needed for
          multi-quant HF repos that ship several ``.gguf`` files as
          siblings), use that exact file. Popped in place so it never leaks
          onto the ``llama-server`` command line.
        - Otherwise, glob the directory for ``*.gguf`` and require exactly
          one match.
        """
        if not resolved.is_local_dir:
            raise ValueError(
                f"Model '{resolved.model_id}' is not pre-staged in the AIM model cache "
                f"(resolver returned a bare model reference, not a local directory). "
                f"llama-server's `-m` flag needs a real file on disk — run "
                f"`./entrypoint.py download-to-cache --model-id hf://{resolved.model_id}` "
                f"(or the equivalent init step) before serving with the llamacpp engine."
            )

        gguf_filename = engine_args.pop("gguf_filename", None)
        if gguf_filename:
            candidate = os.path.join(resolved.path, gguf_filename)
            if not os.path.isfile(candidate):
                raise ValueError(f"gguf_filename '{gguf_filename}' not found under '{resolved.path}'.")
            return candidate

        matches = sorted(glob.glob(os.path.join(resolved.path, "*.gguf")))
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(
                f"No .gguf file found under '{resolved.path}'. Pre-stage the model with "
                f"`./entrypoint.py download-to-cache`, or check the cache dir."
            )
        raise ValueError(
            f"Multiple .gguf files found under '{resolved.path}': {matches}. "
            f"Set `gguf_filename` in engine_args to pick one (this repo ships more than one quant)."
        )
