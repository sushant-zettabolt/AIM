# AIM Runtime — Annotated Code Walkthrough

Read the files in this order (this is the actual runtime execution order, not
alphabetical). Every code block is the real file content with `# <<` comments
inserted after the lines that matter — these comments are NOT in the real
source, this file is a reading aid only, generated on 2026-08-19.

Where a file is long, only the parts relevant to the llama.cpp integration
work are annotated line-by-line; the rest is included for context with lighter
comments.

---

## 1. `src/entrypoint.py` — CLI entry point

Everything starts here. `python entrypoint.py <command>` is what a Docker
`ENTRYPOINT` / k8s container command actually runs.

```python
import click                                    # << CLI framework: @click.group / @click.command turn functions into subcommands

from aim_runtime.logging_config import configure_logging
from aim_runtime.utils import dump_yaml

root_log_level = os.environ.get("AIM_LOG_LEVEL_ROOT", "WARNING")
configure_logging(...)                           # << logging is configured at IMPORT time, before any CLI parsing — so even --help gets sane log levels
os.environ["VLLM_LOGGING_LEVEL"] = root_log_level  # << vLLM reads its own env var for its internal logger; harmless no-op for llama.cpp

from aim_runtime.accelerator_detector import AcceleratorDetector
from aim_runtime.aim_runtime import AIMRuntime   # << the orchestrator class, see file #4 below
from aim_runtime.config import AIMConfig         # << env-var -> typed config, see file #2 below
from aim_runtime.object_model import AcceleratorFamily, AcceleratorModel, AcceleratorType
from aim_runtime.profile_selector import ProfileCompatibilityState, ProfileSelector


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """AIM Runtime - Profile selection and command generation."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(serve)                        # << running with NO subcommand ("python entrypoint.py") defaults to `serve` — this is what a container ENTRYPOINT normally triggers


@cli.command()
def serve():
    """Select profile and execute the inference server (default)."""
    try:
        config = AIMConfig.from_environment()    # << Step A: read every AIM_* env var into a typed AIMConfig (file #2)
        configure_logging(root_log_level=config.log_level_root, aim_log_level=config.log_level)

        runtime = AIMRuntime(config)              # << Step B: build the orchestrator — THIS is where the engine class gets selected/instantiated (see AIMRuntime.__init__, file #4)
        runtime.serve()                           # << Step C: everything downstream (profile selection, command building, os.execv) happens inside this call

    except ValueError as e:                       # << AIMConfig / engine_config raise plain ValueError for "bad config" — caught here and turned into a clean exit(1) instead of a stack trace
        ...
        sys.exit(1)
    except FileNotFoundError as e:
        sys.exit(1)
    except Exception as e:
        sys.exit(1)


@cli.command(name="dry-run")
@click.option("--format", type=click.Choice(["yaml", "json"]), default="yaml")
def dry_run(format):
    """Perform profile selection and display the selected profile without execution."""
    config = AIMConfig.from_environment()
    runtime = AIMRuntime(config)
    profiles_dict = runtime.dry_run()             # << same profile-selection + command-building logic as serve(), but returns data instead of exec'ing — this is your primary debugging tool while building the llamacpp engine (no need for a real GGUF file or even a real llama-server binary on PATH... actually it DOES need launch_prefix() to resolve, see below)
    print(dump_yaml(profiles_dict))


@cli.command(name="list-profiles")
...
def list_profiles(state, format, skip_compatibility_check, verbose):
    """List and categorize profiles by compatibility with current configuration."""
    config = AIMConfig.from_environment()
    selector = ProfileSelector(config)             # << lower-level than AIMRuntime — just profile discovery/filtering, no engine involved. Useful to sanity-check your new profile YAML is even being discovered before wiring the engine.
    ...


def main():
    cli()


if __name__ == "__main__":
    main()
```

**Takeaway**: `serve` and `dry-run` both do `AIMConfig.from_environment()` →
`AIMRuntime(config)` → a method call. `AIMRuntime.__init__` is where your new
`Engine.LLAMACPP` first gets turned into a `LlamaCppEngine` instance — if
registration is broken, it breaks here, immediately, for both commands.

---

## 2. `src/aim_runtime/config.py` — env vars → typed config

```python
from .object_model import AcceleratorFamily, AcceleratorModel, AcceleratorType, Engine, Metric, Precision
# << Engine here is imported from aim_runtime.object_model, which re-exports the
#    aim_common.object_model.Engine StrEnum — this is the SAME enum you'd extend
#    with a LLAMACPP member.

@dataclass
class AIMConfig:
    """Configuration class for AIM runtime parameters."""

    aim_id: Optional[str] = None        # << set when this is a MODEL-SPECIFIC container (AIM_ID env var)
    model_id: Optional[str] = None      # << set when this is a BASE container (AIM_MODEL_ID env var) — mutually exclusive with aim_id, enforced in __post_init__
    precision: Optional[Precision] = None
    accelerator_type: AcceleratorType = AcceleratorType.GPU
    accelerator_family: AcceleratorFamily = AcceleratorFamily.INSTINCT   # << EPYC is what you want for llama.cpp/CPU
    engine: Engine = Engine.VLLM        # << THE FIELD THAT MATTERS: this is set from AIM_ENGINE env var, defaults to vllm if unset. This is what selects LlamaCppEngine vs VllmEngine — NOT anything in the profile YAML.
    ...
    port: int = 8000
    engine_args_override: Optional[Dict[str, Any]] = None   # << populated from AIM_ENGINE_ARGS (JSON) — lets you override e.g. --ctx-size at container-run time without editing the profile

    def __post_init__(self):
        if not self.aim_id and not self.model_id:
            raise ValueError("Either AIM_MODEL_ID or AIM_ID must be provided")
        if self.aim_id and self.model_id:
            raise ValueError("Cannot set both AIM_ID and AIM_MODEL_ID. Only one should be set.")
            # << this is why the MVP plan uses AIM_ID (model-specific container), not AIM_MODEL_ID

    @classmethod
    def _read_enum(cls, name: str, default: str, enum: Type[EnumerationType]) -> EnumerationType:
        value = os.environ.get(name, default)
        try:
            return enum(value.lower())
        except ValueError:
            logger.warning(f"{name} must be one of {[e.value for e in enum]}. Was {value}. Defaulting to {default}.")
            return enum(default.lower())
            # << THIS is the exact behavior when AIM_ENGINE=llamacpp is set but
            #    Engine.LLAMACPP doesn't exist yet: enum(value.lower()) raises
            #    ValueError, caught here, and it silently falls back to "vllm"
            #    with only a WARNING log — no hard crash. Easy to miss during
            #    testing: you'll get vLLM's engine_config lookup failing later
            #    with a confusing error, not an obvious "unknown engine" message.

    @classmethod
    def from_environment(cls, model_id_param: Optional[str] = None) -> "AIMConfig":
        """Create configuration from environment variables."""
        aim_id = os.environ.get("AIM_ID")
        model_id = os.environ.get("AIM_MODEL_ID")
        ...
        return cls(
            aim_id=aim_id,
            model_id=model_id,
            precision=cls._read_precision(),
            accelerator_type=accelerator_type,
            accelerator_family=accelerator_family,
            ...
            engine=cls._read_enum("AIM_ENGINE", "vllm", Engine),   # << the ONE line that reads AIM_ENGINE
            ...
            port=int(os.environ.get("AIM_PORT", 8000)),
            engine_args_override=cls._read_engine_args_override(),
            ...
        )
```

**Takeaway**: `AIM_ENGINE=llamacpp` env var → `Engine("llamacpp")` must exist
as an enum member, or this silently downgrades to `vllm` with just a log
warning. This is the very first thing to verify once you add the enum member:
run with `AIM_ENGINE=llamacpp` and confirm `config.engine == Engine.LLAMACPP`,
not a silent fallback.

---

## 3. `src/aim_common/object_model.py` — shared vocabulary

```python
class Precision(StrEnum):
    """Supported precision types."""
    FP4 = "fp4"
    FP8 = "fp8"
    FP16 = "fp16"
    FP32 = "fp32"
    BF16 = "bf16"
    INT4 = "int4"
    INT8 = "int8"
    # << NOTE: no GGUF quant types (Q4_K_M, Q5_K_S, etc). ProfileMetadata.precision
    #    below is a REQUIRED field, so every llamacpp profile must map its GGUF
    #    quant onto the nearest one of these 7 values — there's no "gguf" bucket.
    #    The exact quant string can go in metadata.variant instead (see below).


class Engine(StrEnum):
    """Supported engine types.

    Keep in sync with ``ENGINE_CLASSES`` in ``aim_runtime.engines``: every
    member here must map to a concrete engine class there, or ``build_engine``
    will raise at runtime for that engine.
    """
    BENTOML = "bentoml"
    VLLM = "vllm"
    VLLM_OMNI = "vllm_omni"
    # << ADD HERE: LLAMACPP = "llamacpp"  (or "llama_cpp" — pick one, it becomes
    #    the AIM_ENGINE value AND the engines.yaml top-level key AND (per the
    #    docstring in specialized_utils.py) the profile filename prefix
    #    convention "{engine}-{accelerator}-{precision}-...")


class ProfileType(StrEnum):
    """Profile type categories."""
    OPTIMIZED = "optimized"
    UNOPTIMIZED = "unoptimized"   # << what AIM_ALLOW_UNOPTIMIZED=true (set by global.platform=epyc in the Helm chart) unlocks — your hand-written MVP profile should probably declare this type
    GENERAL = "general"
    PREVIEW = "preview"


class AdapterToken(StrEnum):
    """Per-profile LoRA adapter capability tokens (ADR-0004).

    ADAPTERS_SCALE_ONLY — LoRA with limited dynamic support (static +
        scale-toggle only, no runtime add/remove), e.g. llama.cpp.
    """
    # << the docstring EXPLICITLY names llama.cpp as the intended consumer of
    #    ADAPTERS_SCALE_ONLY. Not needed for the MVP (no adapter support), but
    #    the design already anticipated this engine.
    ADAPTERS = "adapters"
    ADAPTERS_SCALE_ONLY = "adapters-scale-only"


class ProfileMetadata(BaseModel):
    """Metadata information from a profile."""

    model_config = ConfigDict(frozen=True, use_enum_values=False, populate_by_name=True)

    engine: Engine                       # << must be one of the Engine enum values — this is what profile_registry.py reads to pick engine_class_for(metadata.engine) at DISCOVERY time (separate from AIM_ENGINE at runtime — see note below)
    accelerator_type: Optional[AcceleratorType] = None
    accelerator_model: Optional[AcceleratorModel] = Field(
        default=None,
        validation_alias=AliasChoices("accelerator_model", "gpu"),   # << accepts legacy YAML key "gpu" too
    )
    precision: Precision                 # << REQUIRED, no default — every llamacpp profile YAML must set this to one of the 7 Precision values
    accelerator_count: int = Field(ge=0, validation_alias=AliasChoices("accelerator_count", "gpu_count"))
    metric: Metric
    manual_selection_only: bool
    type: ProfileType
    capabilities: ProfileCapabilities = Field(default_factory=ProfileCapabilities)
    features: List[AdapterToken] = Field(default_factory=list)
    primary: Optional[bool] = None
    variant: Optional[str] = Field(default=None, pattern=r"^[a-z][a-z0-9-]*$")
    # << variant is the free-form slug field — recommended home for the exact
    #    GGUF quant name (e.g. "q4-k-m") since Precision can't hold it directly.
    #    Already used today for gpt-oss's mxfp4-vs-fp4 filename mismatch, so
    #    this is an established pattern, not a new hack.

    @property
    def accelerator_label(self) -> str:
        """Human-readable label mirroring the profile id format
        (e.g. 'vllm-mi300x-fp16-tp1-latency')."""
        acc_segment = self.accelerator_model.value.lower() if self.accelerator_model else "none"
        tp = 1 if self.accelerator_type == AcceleratorType.CPU else self.accelerator_count
        # << CPU profiles always render as tp1 in the label regardless of
        #    accelerator_count, because accelerator_count means "recommended
        #    core count" for CPU, not tensor-parallel degree. Your llamacpp/EPYC
        #    profile's label will be e.g. "llamacpp-epyc9575-int4-tp1-latency"
        #    even if accelerator_count is, say, 32 (cores).
        base = f"{self.engine.value.lower()}-{acc_segment}-{self.precision.value.lower()}-tp{tp}-{self.metric.value.lower()}"
        if self.variant:
            return f"{base}-{self.variant}"
        return base
        # << this label pattern is also the PROFILE FILENAME convention per
        #    specialized_utils.py's _infer_model_engine() docstring: profiles are
        #    named "{engine}-{accelerator}-{precision}-tp{n}-{metric}[-{variant}].yaml"
        #    and the engine PREFIX of the filename is what that function reads
        #    to infer which engine a model-dedicated base image serves.
```

**Important nuance to hold in your head**: there are TWO separate "which
engine" signals in this system:
1. `AIM_ENGINE` env var → `AIMConfig.engine` → which `BaseEngine` subclass
   gets **instantiated** to build/launch the process (config.py, aim_runtime.py).
2. `profile.metadata.engine` (from the profile YAML) → used at **profile
   discovery time** (`profile_registry.py`) to validate that profile's env
   vars with the right engine's validator, and used to build the
   `accelerator_label`/filename.

They are supposed to agree for a given profile to actually work end-to-end,
but the code does not currently cross-check them at profile-selection time —
only `build_engine()` checks `engine_config.engine != config.engine` (see
file #6 below), which is a different comparison (engines.yaml's stamped
engine vs AIM_ENGINE, not the profile's declared engine vs AIM_ENGINE).

---

## 4. `src/aim_runtime/engine_config.py` — engines.yaml loader

```python
class EngineConfig(BaseModel):
    """Engine launch configuration, loaded from engines.yaml."""
    model_config = ConfigDict(frozen=True, extra="ignore")

    engine: Engine | None = None
    launch: str          # << e.g. "llama-server" or "python -m vllm.entrypoints.openai.api_server" — shell-split later by BaseEngine.launch_prefix()
    model_arg: str = ""  # << e.g. "-m" for llama-server, "--model" for vllm — the CLI flag that precedes the model path


def load_engine_config(engine: Engine, config_dir: str) -> EngineConfig:
    """Load engine configuration from engines.yaml."""
    engines_path = Path(config_dir) / "engines.yaml"
    engines = read_yaml(engines_path)                # << config_dir is AIMConfig.config_path = "/workspace/aim-runtime/config" inside the container — baked in from assets/<acc>/base/.../config/engines.yaml at image build time

    if engine.value not in engines:
        available = list(engines.keys())
        raise ValueError(
            f"No configuration for engine '{engine.value}' in {engines_path}. Available engines: {available}"
        )
        # << THIS is the clear, loud failure mode when AIM_ENGINE=llamacpp is
        #    correctly recognized as an enum value but the image's engines.yaml
        #    has no "llamacpp:" stanza (e.g. still commented out, as it
        #    currently is in every assets/**/config/engines.yaml). Good error
        #    message — lists available engines. This is what the MVP plan's
        #    "known failure point #1" refers to.

    raw = engines[engine.value]
    return EngineConfig(**raw, engine=engine)
```

**engines.yaml shape** (from `assets/epyc/base/config/engines.yaml`, currently
commented out):
```yaml
# llamacpp:
#   launch: llama-server
#   model_arg: -m
```
Uncommenting this (or adding a real stanza) is the whole content of this file
for the MVP.

---

## 5. `src/aim_runtime/aim_runtime.py` — orchestrator

```python
class AIMRuntime:
    """Main orchestrator for AIM runtime operations."""

    def __init__(self, config: AIMConfig):
        self.config = config
        self.profile_selector = ProfileSelector(config)         # << discovers + filters profile YAML files (uses config.profile_base_path, hardware detection, etc.)
        engine_config = load_engine_config(config.engine, config.config_path)  # << file #4 above — raises ValueError HERE if engines.yaml has no entry for config.engine
        self.engine = build_engine(config, engine_config)        # << file #6 below — instantiates LlamaCppEngine (or VllmEngine, etc). THIS is the concrete object everything downstream calls methods on.
        self.command_generator = CommandGenerator(config, self.engine)  # << file #8 below — the class that actually builds the argv list
        self.storage_registry = StorageBackendRegistry()

    def serve(self) -> None:
        """
        1. Select the appropriate profile
        2. Determine actual model and auto-download if needed
        3. Generate execution parameters
        4. Set environment variables
        5. Execute the inference server command (via os.execv)
        """
        self.config.log_debug_info()

        profile = self.profile_selector.find_profile()          # << raises if no compatible profile found — for the MVP, your hand-written llamacpp profile must actually be discovered and pass compatibility filtering (accelerator match, etc.)

        model_id = profile.model_id or self.config.model_id or self.config.aim_id
        if not model_id:
            raise ValueError("Model not specified in profile or configuration")

        if self.engine.requires_aiter_kernels and profile.metadata and profile.metadata.accelerator_model:
            _install_aiter_prebuilt_kernels(profile.metadata.accelerator_model)
            # << requires_aiter_kernels defaults to False on BaseEngine — no-op
            #    for llamacpp unless you deliberately set it True (you won't).

        command_list, env_vars = self.command_generator.generate_execution_params(profile)
        # << THIS is where model-path resolution + CLI arg building + env var
        #    resolution all happen — see file #8 for the actual logic, and
        #    file #7 for why "the model path" is currently a directory, not a
        #    single GGUF file (the core gap for llama.cpp).

        if self.config.accelerator_type == AcceleratorType.CPU and hasattr(self.profile_selector, "detected_cpu_cores"):
            EpycDetector.override_cpu_env_vars(
                env_vars, self.profile_selector.detected_cpu_cores, cpuset_bind=self.profile_selector.cpuset_bind,
            )
            # << this ALWAYS runs for any CPU-accelerator profile regardless of
            #    engine — it force-overrides OMP_NUM_THREADS /
            #    VLLM_CPU_OMP_THREADS_BIND based on detected core count. Harmless
            #    no-op for llama-server (which doesn't read those env vars), but
            #    worth knowing it runs unconditionally — nothing to change here.

        for key, value in env_vars.items():
            os.environ[key] = str(value)

        executable_path = shutil.which(command_list[0])
        if not executable_path:
            raise FileNotFoundError(f"Could not find executable: {command_list[0]}")
            # << THIS is "known failure point #2" from the MVP plan: if
            #    llama-server isn't on PATH inside the final image (e.g. built
            #    in an intermediate Docker stage that got discarded), this is
            #    exactly where it surfaces, with a clear message.

        if self.engine.needs_runtime_supervisor(profile):   # << False for llamacpp (base class default) — no dynamic adapter watcher needed for the MVP
            self._serve_supervised(command_list)
        else:
            os.execv(executable_path, command_list)          # << process REPLACEMENT (not subprocess) — the Python process becomes llama-server, same PID. This is why command_list must be a flat argv list, no shell string.

    def dry_run(self) -> List[Dict[str, Any]]:
        """Same profile-selection + param-generation as serve(), but returns
        data (including a generated shell script string) instead of exec'ing."""
        profile = self.profile_selector.find_profile()
        ...
        script_path = self.command_generator.generate_command_script(profile, env_vars=script_env_override)
        # << generate_command_script calls the SAME _build_command_list() as
        #    serve() (file #8), so dry-run is a faithful preview of what serve
        #    would actually run — good primary tool for verifying your new
        #    engine's argv construction without needing a compiled llama-server
        #    binary at all (it doesn't check the binary exists, only serve()'s
        #    shutil.which does).
```

---

## 6. `src/aim_runtime/engines/__init__.py` — engine dispatch table

```python
from aim_runtime.engines.base import BaseEngine
from aim_runtime.engines.bentoml import BentomlEngine, BentomlEngineArgsModel
from aim_runtime.engines.vllm import VllmEngine, VllmEngineArgsModel, validate_vllm_env_vars
from aim_runtime.engines.vllm_omni import VllmOmniEngine, VllmOmniEngineArgsModel
# << ADD: from aim_runtime.engines.llamacpp import LlamaCppEngine, LlamaCppEngineArgsModel

ENGINE_CLASSES: dict[Engine, type[BaseEngine]] = {
    Engine.VLLM: VllmEngine,
    Engine.VLLM_OMNI: VllmOmniEngine,
    Engine.BENTOML: BentomlEngine,
    # << ADD: Engine.LLAMACPP: LlamaCppEngine,
    #    Every Engine enum member MUST have an entry here or the two functions
    #    below raise for that engine. This dict is the single dispatch point —
    #    nothing else in the codebase branches on engine name via if/elif.
}


def engine_class_for(engine: Engine) -> type[BaseEngine]:
    """Return the engine class for an ``Engine`` enum value.
    Usable without an instance (e.g. profile-load-time validation)."""
    try:
        return ENGINE_CLASSES[engine]
    except KeyError as exc:
        available = ", ".join(sorted(e.value for e in ENGINE_CLASSES))
        raise ValueError(f"No engine class registered for '{engine}'. Available: {available}") from exc
        # << called by profile_registry.py at DISCOVERY time (file #9) — every
        #    profile's metadata.engine is looked up here even before AIM_ENGINE
        #    is considered. If you add Engine.LLAMACPP to the enum but forget
        #    this dict entry, EVERY profile discovery pass fails for any
        #    llamacpp profile with this exact error.


def build_engine(config: "AIMConfig", engine_config: "EngineConfig") -> BaseEngine:
    """Instantiate the concrete engine for ``config.engine``."""
    if engine_config.engine is not None and engine_config.engine != config.engine:
        raise ValueError(
            f"Engine mismatch: AIM config declares '{config.engine}' but engine_config is for '{engine_config.engine}'."
        )
        # << sanity check: load_engine_config() stamps engine_config.engine =
        #    the SAME `engine` arg it was called with (engine_config.py line
        #    "return EngineConfig(**raw, engine=engine)"), so this branch is
        #    effectively unreachable in normal flow — it's a defensive check
        #    against future refactors, not something you need to worry about.
    return engine_class_for(config.engine)(config, engine_config)
    # << the actual instantiation: LlamaCppEngine(config, engine_config)
```

---

## 7. `src/aim_runtime/engines/base.py` — the extension point

This is the class you subclass for `LlamaCppEngine`. Read every method —
each is a hook the runtime calls at a specific point in the flow above.

```python
class BaseEngine(ABC):
    """Abstract base for serving engines."""

    ARGS_MODEL: ClassVar[Optional[type[EngineArgsModel]]] = None
    # << pydantic model used to validate engine_args from the profile YAML +
    #    AIM_ENGINE_ARGS override. None = skip validation entirely (fine for
    #    MVP — "pass-through" mode). A stricter LlamaCppEngineArgsModel is a
    #    later-phase nice-to-have, not required for anything to work.

    ARGS_FORMAT: ClassVar[EngineArgsFormat] = EngineArgsFormat.STANDARD
    # << STANDARD = "--key value" flags. This is llama-server's format too
    #    (confirmed in engine_args_models.py's docstring, which explicitly
    #    lists llama.cpp as a STANDARD-format consumer) — so you do NOT
    #    override this for LlamaCppEngine, just inherit the default.

    requires_aiter_kernels: ClassVar[bool] = False   # << leave as default (False) — AITER is AMD-GPU-specific, irrelevant to CPU/llama.cpp

    def __init__(self, config: "AIMConfig", engine_config: "EngineConfig") -> None:
        self.config = config
        self.engine_config = engine_config

    def launch_prefix(self) -> list[str]:
        """Return the launch command prefix as an argv list.
        Splits ``engine_config.launch`` and resolves a leading ``python`` to
        ``python``/``python3`` depending on what is on PATH."""
        launch = shlex.split(self.engine_config.launch)   # << e.g. "llama-server" -> ["llama-server"]; base implementation is generic enough that LlamaCppEngine likely does NOT need to override this at all
        if launch and launch[0] == "python":
            launch[0] = "python" if shutil.which("python") else "python3"
        return launch

    @property
    def model_arg(self) -> str:
        """CLI flag used to pass the model path."""
        return self.engine_config.model_arg   # << "-m" from engines.yaml — again, base implementation is sufficient, no override needed

    def apply_engine_defaults(self, engine_args: dict[str, Any], served_model_names: list[str]) -> None:
        """Apply engine-specific argument defaults in place. Base: no-op."""
        return None
        # << e.g. VllmEngine overrides this to inject --served-model-name.
        #    llama-server has its own --alias / -a flag for a similar purpose
        #    if you want it — optional, not required for MVP.

    @classmethod
    def validate_engine_args(cls, engine_args: dict[str, Any]) -> None:
        """Validate engine args via ``cls.ARGS_MODEL`` (no-op when unset)."""
        if cls.ARGS_MODEL is not None:
            cls.ARGS_MODEL.model_validate(engine_args)
        # << with ARGS_MODEL = None (MVP choice), this is a no-op — any
        #    engine_args dict passes through unvalidated, good enough to unblock
        #    the demo, revisit later.

    @classmethod
    def validate_env_vars(cls, env_vars: dict[str, str], source: str = "") -> None:
        """Validate engine-specific env vars. Base: no-op."""
        return None
        # << called at PROFILE DISCOVERY time (file #9) for every profile,
        #    keyed by that profile's metadata.engine. Base no-op is fine —
        #    llama-server has no required env vars analogous to vLLM's VLLM_*.

    def serialize_engine_args(self, engine_args: dict[str, Any]) -> list[str]:
        """Serialize engine args to a CLI list using this engine's format."""
        return engine_args_to_cli_list(engine_args, self.ARGS_FORMAT)
        # << inherited as-is; converts {"ctx_size": 4096, "host": "0.0.0.0"}
        #    into ["--ctx-size", "4096", "--host", "0.0.0.0"] (STANDARD format,
        #    see file #8's engine_args_to_cli_list for the exact rules)

    def needs_runtime_supervisor(self, profile: "Profile") -> bool:
        """Whether serve() must supervise the engine (vs. os.execv replace).
        Base engines run via os.execv."""
        return False   # << correct default for llamacpp MVP — no LoRA supervisor needed
```

**What's MISSING here** (per the full-project plan, not the 3-day MVP): there
is **no `resolve_model_path` hook** on `BaseEngine`. `command_generator.py`
(file #8) currently calls `ModelCacheResolver.resolve_model_path()` directly
and uses its `.path` (a **directory**) as the model argument unconditionally.
For engines that need a single **file** (llama-server's `-m` wants one
`.gguf` file, not a directory), this needs a new overridable hook — see the
exact line in file #8 below.

---

## 8. `src/aim_runtime/engines/engine_args_models.py` — CLI serialization

```python
class EngineArgsFormat(StrEnum):
    """How engine_args are serialized on the command line.
    STANDARD:   --key value  (default, used by vLLM, sglang, llama.cpp, …)
    FORWARDED:  --arg key=value  (used by e.g. BentoML ``serve --arg …``)
    """
    STANDARD = "standard"
    FORWARDED = "forwarded"
    # << confirms: llamacpp needs no new format, use STANDARD (the default)


def engine_args_to_cli_list(engine_args: dict[str, Any], args_format=EngineArgsFormat.STANDARD) -> list[str]:
    cli_args: list[str] = []
    for key, value in engine_args.items():
        if args_format == EngineArgsFormat.FORWARDED:
            ...   # << not relevant to llamacpp
            continue
        flag = f"--{key.replace('_', '-')}"        # << engine_args keys can be snake_case in YAML; always rendered as kebab-case flags
        if value is None:
            cli_args.append(flag)                   # << bare flag, e.g. {"verbose": None} -> ["--verbose"]
        elif isinstance(value, bool):
            if value:
                cli_args.append(flag)               # << {"flash-attn": true} -> ["--flash-attn"]; {"flash-attn": false} -> nothing at all (NOT "--flash-attn false")
        elif isinstance(value, (list, tuple)):
            cli_args.append(flag)
            for item in value:
                cli_args.append(str(item))          # << {"foo": [1,2]} -> ["--foo", "1", "2"]
        elif isinstance(value, dict):
            cli_args.extend([flag, json.dumps(value)])
        else:
            cli_args.extend([flag, str(value)])     # << the common case: {"ctx-size": 4096} -> ["--ctx-size", "4096"]
    return cli_args
```

**Takeaway for writing your MVP profile YAML `engine_args`**: any key you put
under `engine_args:` becomes a `--kebab-case value` flag verbatim (with the
boolean/list/dict special cases above). No llama.cpp-specific mapping code is
needed — this generic serializer already produces valid `llama-server` flags
as long as your YAML keys match real `llama-server` flag names.

---

## 9. `src/aim_runtime/profile_registry.py` — profile discovery/validation

```python
@dataclass
class ProfileRegistry:
    profiles: List[Profile]
    ...

    @classmethod
    def discover_and_validate(cls, search_paths: List[str], validator: ProfileValidator) -> "ProfileRegistry":
        for priority, search_path in enumerate(search_paths, 1):
            for profile_file in search_path_obj.rglob("*.yaml"):    # << recursively finds every *.yaml under profiles/ — your new profile file just needs to exist somewhere under the configured search paths, any subdirectory depth
                try:
                    profile = cls._load_and_validate_profile(str(profile_file), validator, priority)
                    ...
                except (PydanticValidationError, ValueError) as e:
                    logger.warning(f"✗ Invalid profile: {profile_file} - Validation error: {e}")
                    # << IMPORTANT: a broken profile YAML does NOT crash the
                    #    whole runtime — it's logged as a WARNING and SKIPPED.
                    #    If your llamacpp profile silently doesn't show up in
                    #    `list-profiles`, check the logs at DEBUG level for
                    #    this warning rather than assuming it's a discovery
                    #    path problem.
                except Exception as e:
                    logger.warning(f"Failed to process profile file {profile_file}: {e}")

    @staticmethod
    def _load_and_validate_profile(profile_path: str, validator: ProfileValidator, priority: int) -> Profile:
        profile_data = read_yaml(Path(profile_path))
        profile_handling = ProfileHandling(path=profile_path, filename=profile_file.name, priority=priority)
        is_general = profile_handling.is_general

        validator.validate(profile_data, is_general_profile=is_general)   # << generic Pydantic-schema-level validation (required top-level keys etc.) — separate from engine-specific validation below

        metadata = ProfileMetadata.from_dict(profile_data["metadata"])    # << parses the metadata: block — this is where an invalid `engine: llamacpp` (before you've added the enum member) would raise a Pydantic ValidationError, caught above and logged as a warning, profile silently skipped

        engine_class_for(metadata.engine).validate_env_vars(profile_data.get("env_vars", {}), source=profile_path)
        # << dispatches to LlamaCppEngine.validate_env_vars() (inherited
        #    no-op from BaseEngine unless you override it) for every llamacpp
        #    profile, AT DISCOVERY TIME — i.e. before any profile is even
        #    selected for execution. This is the metadata.engine field being
        #    used, NOT AIM_ENGINE. It's why engine_class_for() must resolve
        #    llamacpp correctly even if AIM_ENGINE happens to be unset or set
        #    to something else at discovery time (discovery runs for ALL
        #    profiles regardless of which one ends up selected).

        if not is_general:
            aim_id = profile_data["aim_id"]
            model_id = profile_data["model_id"]
        else:
            aim_id = ""
            model_id = ""

        return Profile(
            profile_handling=profile_handling,
            aim_id=aim_id,
            model_id=model_id,
            metadata=metadata,
            engine_args=profile_data["engine_args"],
            env_vars=profile_data["env_vars"],
        )
```

---

## 10. `src/aim_runtime/model_cache_resolver.py` — the core MVP gap

```python
@dataclass
class ResolvedModelPath:
    path: str            # << the resolved path — see below, this is a DIRECTORY, never a specific file
    is_local_dir: bool
    model_id: str


class ModelCacheResolver:
    def resolve_model_path(self, model_id: str) -> Optional[ResolvedModelPath]:
        """
        1. Check local directory format (cache_dir/org/model/)
        2. Fall back to model_id (HuggingFace handles cache/download transparently)
        """
        local_path = self._get_local_dir_path(model_id)   # << cache_dir/org/model/ e.g. /workspace/model-cache/TheBloke/Llama-2-7B-GGUF
        if local_path and os.path.isdir(local_path):
            return ResolvedModelPath(path=local_path, is_local_dir=True, model_id=model_id)
            # << THIS is the line that matters: if the model was already
            #    downloaded, `.path` is a DIRECTORY containing (possibly
            #    several) files, not a single .gguf file. vLLM/HF loaders
            #    accept a directory (they know which files inside it matter).
            #    llama-server's -m flag needs ONE FILE PATH. Nothing here
            #    picks a specific .gguf out of that directory — that logic
            #    doesn't exist anywhere yet.

        return ResolvedModelPath(path=model_id, is_local_dir=False, model_id=model_id)
        # << fallback: bare "org/model" string, handed to the engine to resolve
        #    itself (HF Python libs do this internally for vLLM). llama-server
        #    has NO equivalent "give me an org/model string and I'll figure it
        #    out" behavior — it strictly wants a real file path (or its own
        #    --hf-repo/--hf-file flags, which this generic string doesn't map
        #    to at all).
```

---

## 11. `src/aim_runtime/command_generator.py` — where it all comes together

```python
class CommandGenerator:
    def __init__(self, config: AIMConfig, engine: BaseEngine):
        self.config = config
        self.engine = engine
        self.cache_resolver = ModelCacheResolver(config.cache_path)   # << file #10 above
        ...

    def generate_execution_params(self, profile: Profile) -> tuple[List[str], Dict[str, str]]:
        command_list = self._build_command_list(profile)   # << the interesting method, below
        env_vars = self._resolve_env_vars(profile)
        return command_list, env_vars

    def _build_command_list(self, profile: Profile) -> List[str]:
        model_id = profile.model_id or self.config.model_id or self.config.aim_id
        if not model_id:
            raise ValueError("Model not specified in profile or configuration")

        resolved_model = self.cache_resolver.resolve_model_path(model_id)   # << file #10
        if resolved_model is None:
            model_path = model_id
        else:
            model_path = resolved_model.path
            # <<<<<< THE EXACT LINE TO CHANGE for llama.cpp support. <<<<<<
            # Currently: always takes the resolver's .path directly, no matter
            # what engine is running. For llamacpp you need something like:
            #   model_path = self.engine.resolve_model_path(resolved_model, profile)
            # where BaseEngine.resolve_model_path() is a NEW hook you add
            # (default: return resolved_model.path, i.e. today's behavior,
            # so vLLM/BentoML are unaffected), and LlamaCppEngine overrides it
            # to glob resolved_model.path for a single *.gguf file (raising a
            # clear error on 0 or 2+ matches — see the MVP plan's cut-list
            # item #1 for the "just hardcode the filename" shortcut).

        served_model_name_list = [model_id]
        if self.config.aim_id and self.config.aim_id != model_id:
            served_model_name_list.append(self.config.aim_id)

        engine_args = self._merge_and_validate_engine_args(profile)   # << profile's engine_args: block, merged with AIM_ENGINE_ARGS override, in that precedence order

        adapter_args, _ = self._adapter_runtime(profile)   # << no-op unless profile.metadata.supports_adapters and the engine implements build_adapter_runtime — both false for llamacpp MVP
        if adapter_args:
            engine_args.update(adapter_args)
            self.engine.validate_engine_args(engine_args)

        engine_args["port"] = self.config.port    # << system override, always wins — --port <AIM_PORT> always gets added regardless of what's in engine_args, so don't set "port" in your profile YAML (it'll just get silently overwritten anyway, at this exact point)

        self.engine.apply_engine_defaults(engine_args, served_model_name_list)  # << no-op for LlamaCppEngine unless you override it

        args_list = self.engine.serialize_engine_args(engine_args)   # << file #8's engine_args_to_cli_list

        launch = self.engine.launch_prefix()      # << ["llama-server"] from engines.yaml

        if self.engine.model_arg:                 # << "-m", truthy
            command_list = launch + [self.engine.model_arg, model_path] + args_list
            # << final assembled command, e.g.:
            #    ["llama-server", "-m", "/workspace/model-cache/org/model/model.Q4_K_M.gguf",
            #     "--ctx-size", "4096", "--host", "0.0.0.0", "--port", "8000"]
        else:
            command_list = launch + args_list

        return command_list
```

**This is the single most important file to understand for the MVP** — it's
the one concrete place where "directory vs. GGUF file" needs a fix, and
everything upstream (profile, engine registration, engines.yaml) exists to
feed data into this method correctly.

---

## Suggested reading/debugging loop once you start coding

1. Add `Engine.LLAMACPP` to the enum (file #3). Confirm
   `AIM_ENGINE=llamacpp python entrypoint.py dry-run` no longer silently
   falls back to vllm (check `_read_enum`'s warning log is gone) — it'll now
   fail differently, at `load_engine_config` (file #4), because
   `engines.yaml` has no `llamacpp:` entry yet. That's the expected next
   failure.
2. Register `LlamaCppEngine` in `ENGINE_CLASSES` (file #6) even before it's
   fully implemented (a stub with `ARGS_MODEL = None` is enough) — this
   unblocks `build_engine()`.
3. Uncomment/add `llamacpp:` in `assets/epyc/base/config/engines.yaml` (file
   #4's data source). Now `load_engine_config` succeeds.
4. Write a minimal profile YAML with a fake local `model_id` dir containing a
   dummy `*.gguf`-named file, run `dry-run` again, and read the printed
   `script` field — that's `_build_command_list()`'s output (file #11) in
   human-readable form. Iterate here until the command looks right, all
   without needing a real compiled `llama-server` binary.
