# Copyright © Advanced Micro Devices, Inc., or its affiliates.
#
# SPDX-License-Identifier: MIT

"""llama.cpp engine.

Phase 0 (local smoke-test) implementation: no native argument validation
(``ARGS_MODEL = None``) and the inherited defaults for everything else — CLI
args serialize as standard ``--key value`` flags (``ARGS_FORMAT`` inherited
from :class:`BaseEngine`), and no AITER kernels are needed on CPU
(``requires_aiter_kernels`` inherited as ``False``).

Model loading for Phase 0 goes through ``llama-server``'s own ``--hf-repo``
flag (a normal ``engine_args`` entry), not AIM's model-cache resolver — the
profile's ``engines.yaml`` entry sets ``model_arg: ""`` so
``CommandGenerator`` never prepends a ``-m <path>`` argument. See
``LLAMACPP_LOCAL_MVP_PLAN.md`` for the reasoning.
"""

from __future__ import annotations

from aim_runtime.engines.base import BaseEngine


class LlamaCppEngine(BaseEngine):
    """llama.cpp-backed serving engine (``llama-server``)."""
