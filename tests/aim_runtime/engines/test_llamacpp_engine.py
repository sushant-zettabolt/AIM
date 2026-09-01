# Copyright © Advanced Micro Devices, Inc., or its affiliates.
#
# SPDX-License-Identifier: MIT

"""Tests for LlamaCppEngineArgsModel validation, LlamaCppEngine.resolve_model_path,
and LlamaCppEngine.apply_engine_defaults (HF_TOKEN plumbing)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aim_runtime.engine_config import EngineConfig
from aim_runtime.engines.llamacpp import LlamaCppEngine, LlamaCppEngineArgsModel
from aim_runtime.model_cache_resolver import ResolvedModelPath


class TestLlamaCppEngineArgsModel:
    def test_empty_args_accepted(self):
        m = LlamaCppEngineArgsModel.model_validate({})
        assert m.ctx_size is None
        assert m.threads is None

    def test_kebab_case_keys_accepted(self):
        m = LlamaCppEngineArgsModel.model_validate({"ctx-size": 8192, "n-gpu-layers": 0, "host": "0.0.0.0"})
        assert m.ctx_size == 8192
        assert m.n_gpu_layers == 0
        assert m.host == "0.0.0.0"

    def test_bad_type_rejected(self):
        with pytest.raises(ValidationError):
            LlamaCppEngineArgsModel.model_validate({"ctx-size": "not-a-number"})

    def test_bad_literal_choice_rejected(self):
        with pytest.raises(ValidationError):
            LlamaCppEngineArgsModel.model_validate({"numa": "bogus-mode"})

    def test_valid_literal_choice_accepted(self):
        m = LlamaCppEngineArgsModel.model_validate({"numa": "distribute", "split-mode": "row"})
        assert m.numa == "distribute"
        assert m.split_mode == "row"

    def test_unknown_extra_key_passes_through(self):
        # extra="allow" (inherited from EngineArgsModel): an unmodeled-but-real
        # llama-server flag isn't rejected outright.
        m = LlamaCppEngineArgsModel.model_validate({"some-future-flag": "value"})
        assert m.model_extra == {"some-future-flag": "value"}

    def test_gguf_filename_accepted(self):
        m = LlamaCppEngineArgsModel.model_validate({"gguf-filename": "model-q8_0.gguf"})
        assert m.gguf_filename == "model-q8_0.gguf"


def _engine() -> LlamaCppEngine:
    from aim_runtime.config import AIMConfig
    from aim_common.object_model import Engine

    config = AIMConfig(aim_id="aim-smoketest/llamacpp-tiny", engine=Engine.LLAMACPP)
    engine_config = EngineConfig(engine=Engine.LLAMACPP, launch="llama-server", model_arg="-m")
    return LlamaCppEngine(config, engine_config)


class TestResolveModelPath:
    def test_raises_when_not_pre_staged(self):
        eng = _engine()
        resolved = ResolvedModelPath(path="Qwen/Qwen2.5-0.5B-Instruct-GGUF", is_local_dir=False, model_id="Qwen/Qwen2.5-0.5B-Instruct-GGUF")
        with pytest.raises(ValueError, match="not pre-staged"):
            eng.resolve_model_path(resolved, {})

    def test_gguf_filename_hint_selects_exact_file(self, tmp_path):
        (tmp_path / "qwen2.5-0.5b-instruct-fp16.gguf").write_bytes(b"x")
        (tmp_path / "qwen2.5-0.5b-instruct-q8_0.gguf").write_bytes(b"x")
        eng = _engine()
        resolved = ResolvedModelPath(path=str(tmp_path), is_local_dir=True, model_id="Qwen/Qwen2.5-0.5B-Instruct-GGUF")
        engine_args = {"gguf_filename": "qwen2.5-0.5b-instruct-q8_0.gguf", "ctx-size": 8192}

        result = eng.resolve_model_path(resolved, engine_args)

        assert result == str(tmp_path / "qwen2.5-0.5b-instruct-q8_0.gguf")
        # Popped so it never leaks onto the llama-server command line.
        assert "gguf_filename" not in engine_args
        assert engine_args == {"ctx-size": 8192}

    def test_gguf_filename_hint_missing_file_raises(self, tmp_path):
        eng = _engine()
        resolved = ResolvedModelPath(path=str(tmp_path), is_local_dir=True, model_id="org/model")
        with pytest.raises(ValueError, match="not found"):
            eng.resolve_model_path(resolved, {"gguf_filename": "does-not-exist.gguf"})

    def test_single_gguf_file_globbed_without_hint(self, tmp_path):
        (tmp_path / "model.gguf").write_bytes(b"x")
        eng = _engine()
        resolved = ResolvedModelPath(path=str(tmp_path), is_local_dir=True, model_id="org/model")

        result = eng.resolve_model_path(resolved, {})

        assert result == str(tmp_path / "model.gguf")

    def test_no_gguf_file_raises(self, tmp_path):
        eng = _engine()
        resolved = ResolvedModelPath(path=str(tmp_path), is_local_dir=True, model_id="org/model")
        with pytest.raises(ValueError, match="No .gguf file"):
            eng.resolve_model_path(resolved, {})

    def test_multiple_gguf_files_without_hint_raises(self, tmp_path):
        (tmp_path / "a.gguf").write_bytes(b"x")
        (tmp_path / "b.gguf").write_bytes(b"x")
        eng = _engine()
        resolved = ResolvedModelPath(path=str(tmp_path), is_local_dir=True, model_id="org/model")
        with pytest.raises(ValueError, match="Multiple .gguf files"):
            eng.resolve_model_path(resolved, {})


class TestApplyEngineDefaultsHfToken:
    def test_injects_hf_token_from_env(self, monkeypatch):
        monkeypatch.setenv("HF_TOKEN", "secret-token")
        eng = _engine()
        engine_args: dict = {}

        eng.apply_engine_defaults(engine_args, ["model-a"])

        assert engine_args["hf-token"] == "secret-token"

    def test_no_env_var_no_injection(self, monkeypatch):
        monkeypatch.delenv("HF_TOKEN", raising=False)
        eng = _engine()
        engine_args: dict = {}

        eng.apply_engine_defaults(engine_args, ["model-a"])

        assert "hf-token" not in engine_args

    def test_explicit_kebab_value_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("HF_TOKEN", "from-env")
        eng = _engine()
        engine_args = {"hf-token": "from-profile"}

        eng.apply_engine_defaults(engine_args, ["model-a"])

        assert engine_args["hf-token"] == "from-profile"

    def test_explicit_snake_value_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("HF_TOKEN", "from-env")
        eng = _engine()
        engine_args = {"hf_token": "from-profile"}

        eng.apply_engine_defaults(engine_args, ["model-a"])

        assert "hf-token" not in engine_args
        assert engine_args["hf_token"] == "from-profile"
