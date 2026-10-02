"""Configuración global de AgentOS / AgentD.

Toda la configuración se puede sobreescribir mediante variables de entorno
con prefijo ``AGENTOS_``. Existen dos modos:

* ``dev``: el agente corre sobre un Ubuntu/Debian normal. Los datos viven en
  ``~/.agentos`` y el socket en ``~/.agentos/run/agentos.sock``.
* ``os``: el agente corre dentro de la imagen AgentOS (Buildroot). Los datos
  viven en ``/data/agent`` y el socket en ``/run/agentos/input.sock``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

# Raíz del repositorio (core/agent/config.py -> ../../..)
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

DEFAULT_MODEL_FILE = "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"


def _env(name: str, default: str | None = None) -> str | None:
    """Lee una variable de entorno con el prefijo AGENTOS_."""
    return os.environ.get(f"AGENTOS_{name}", default)


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "si", "sí"}


@dataclass
class AgentConfig:
    """Parámetros globales del agente, del LLM y de las tools."""

    # --- Modo y rutas -----------------------------------------------------
    mode: str = "dev"
    data_dir: Path = field(default_factory=lambda: Path.home() / ".agentos")
    socket_path: Path = field(default_factory=lambda: Path.home() / ".agentos" / "run" / "agentos.sock")
    db_path: Path = field(default_factory=lambda: Path.home() / ".agentos" / "memory.db")
    audit_log_path: Path = field(default_factory=lambda: Path.home() / ".agentos" / "audit.log")
    log_file: Path | None = None

    # --- LLM ----------------------------------------------------------------
    # auto | llama_cpp | openai | mock
    llm_backend: str = "auto"
    model_path: Path = field(default_factory=lambda: PROJECT_ROOT / "models" / DEFAULT_MODEL_FILE)
    chat_format: str | None = None  # None = usar la plantilla incluida en el GGUF
    n_ctx: int = 8192
    n_threads: int | None = None
    n_gpu_layers: int = -1  # -1 = todas las capas a GPU si llama.cpp se compiló con CUDA
    temperature: float = 0.2
    top_p: float = 0.9
    max_tokens: int = 1024
    repeat_penalty: float = 1.1

    # Fallback OpenAI-compatible (Ollama, llama-server, vLLM...)
    openai_base_url: str = "http://127.0.0.1:11434/v1"
    openai_model: str = "llama3.1:8b"
    openai_api_key: str = "ollama"
    openai_timeout: float = 300.0

    # --- Agente -------------------------------------------------------------
    max_iterations: int = 8
    context_reserve_tokens: int = 1024
    keep_last_messages: int = 6
    tool_output_limit: int = 4000  # caracteres máximos de una observación
    auto_confirm: bool = False  # ¡peligroso! ejecuta acciones destructivas sin preguntar

    # --- Tools --------------------------------------------------------------
    bash_timeout: int = 30
    bash_cwd: Path = field(default_factory=Path.home)
    package_timeout: int = 900

    # --- Logging ------------------------------------------------------------
    log_level: str = "INFO"
    log_format: str = "text"  # text | json

    @classmethod
    def from_env(cls) -> "AgentConfig":
        """Construye la configuración a partir de variables de entorno."""
        mode = (_env("MODE") or "dev").lower()
        if mode == "os":
            data_dir = Path(_env("DATA_DIR") or "/data/agent")
            socket_default = Path("/run/agentos/input.sock")
            model_default = Path("/data/models") / DEFAULT_MODEL_FILE
            cwd_default = Path("/root")
        else:
            data_dir = Path(_env("DATA_DIR") or (Path.home() / ".agentos")).expanduser()
            socket_default = data_dir / "run" / "agentos.sock"
            model_default = PROJECT_ROOT / "models" / DEFAULT_MODEL_FILE
            cwd_default = Path.home()

        n_threads_raw = _env("N_THREADS")
        log_file = _env("LOG_FILE")
        return cls(
            mode=mode,
            data_dir=data_dir,
            socket_path=Path(_env("SOCKET") or socket_default).expanduser(),
            db_path=Path(_env("DB_PATH") or data_dir / "memory.db").expanduser(),
            audit_log_path=Path(_env("AUDIT_LOG") or data_dir / "audit.log").expanduser(),
            log_file=Path(log_file).expanduser() if log_file else None,
            llm_backend=(_env("LLM_BACKEND") or "auto").lower(),
            model_path=Path(_env("MODEL_PATH") or model_default).expanduser(),
            chat_format=_env("CHAT_FORMAT") or None,
            n_ctx=_env_int("N_CTX", 8192),
            n_threads=int(n_threads_raw) if n_threads_raw and n_threads_raw.isdigit() else None,
            n_gpu_layers=_env_int("N_GPU_LAYERS", -1),
            temperature=_env_float("TEMPERATURE", 0.2),
            top_p=_env_float("TOP_P", 0.9),
            max_tokens=_env_int("MAX_TOKENS", 1024),
            repeat_penalty=_env_float("REPEAT_PENALTY", 1.1),
            openai_base_url=(_env("OPENAI_BASE_URL") or "http://127.0.0.1:11434/v1").rstrip("/"),
            openai_model=_env("OPENAI_MODEL") or "llama3.1:8b",
            openai_api_key=_env("OPENAI_API_KEY") or "ollama",
            openai_timeout=_env_float("OPENAI_TIMEOUT", 300.0),
            max_iterations=_env_int("MAX_ITERATIONS", 8),
            context_reserve_tokens=_env_int("CONTEXT_RESERVE", 1024),
            keep_last_messages=_env_int("KEEP_LAST", 6),
            tool_output_limit=_env_int("TOOL_OUTPUT_LIMIT", 4000),
            auto_confirm=_env_bool("AUTO_CONFIRM", False),
            bash_timeout=_env_int("BASH_TIMEOUT", 30),
            bash_cwd=Path(_env("BASH_CWD") or cwd_default).expanduser(),
            package_timeout=_env_int("PACKAGE_TIMEOUT", 900),
            log_level=(_env("LOG_LEVEL") or "INFO").upper(),
            log_format=(_env("LOG_FORMAT") or "text").lower(),
        )

    def ensure_dirs(self) -> None:
        """Crea los directorios de datos necesarios."""
        for path in (self.data_dir, self.socket_path.parent, self.db_path.parent, self.audit_log_path.parent):
            path.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict:
        return {k: str(v) if isinstance(v, Path) else v for k, v in asdict(self).items()}


@lru_cache(maxsize=1)
def get_config() -> AgentConfig:
    """Devuelve la configuración global (cacheada)."""
    return AgentConfig.from_env()


# ---------------------------------------------------------------------------
# Logging estructurado
# ---------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    """Formatea cada registro de log como una línea JSON."""

    _RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Campos extra pasados con logger.info("...", extra={...})
        for key, value in vars(record).items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class KeyValueFormatter(logging.Formatter):
    """Formato texto legible que añade los campos extra como clave=valor."""

    _RESERVED = JsonFormatter._RESERVED

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = " ".join(
            f"{k}={v!r}" for k, v in vars(record).items() if k not in self._RESERVED and not k.startswith("_")
        )
        return f"{base} {extras}" if extras else base


def setup_logging(config: AgentConfig | None = None, level: str | None = None) -> None:
    """Configura el logging raíz según la configuración."""
    config = config or get_config()
    lvl = getattr(logging, (level or config.log_level).upper(), logging.INFO)
    if config.log_format == "json":
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = KeyValueFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if config.log_file:
        config.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(config.log_file, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(lvl)
    for handler in handlers:
        root.addHandler(handler)
    # Librerías ruidosas
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
