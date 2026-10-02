"""Capa de inferencia LLM con streaming.

Backends disponibles:

* ``llama_cpp``: carga un GGUF en proceso con llama-cpp-python (preferido).
* ``openai``: cualquier servidor compatible OpenAI (Ollama, llama-server, vLLM).
  Se usa como fallback automático cuando no hay modelo GGUF.
* ``mock``: backend determinista sin modelo, para tests y demos.

Todos exponen ``stream_chat(messages, params) -> AsyncIterator[str]``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..agent.config import AgentConfig

logger = logging.getLogger("agentos.llm")

Message = dict[str, Any]


class LLMUnavailableError(RuntimeError):
    """No hay ningún backend LLM utilizable."""


@dataclass
class GenerationParams:
    """Parámetros de muestreo para una generación."""

    temperature: float = 0.2
    top_p: float = 0.9
    max_tokens: int = 1024
    repeat_penalty: float = 1.1
    stop: list[str] = field(default_factory=list)
    grammar: str | None = None  # GBNF (solo llama.cpp / llama-server)
    json_mode: bool = False  # fuerza salida JSON válida

    def with_(self, **kwargs: Any) -> "GenerationParams":
        return replace(self, **kwargs)


class LLMBackend(ABC):
    """Interfaz común de backends."""

    name: str = "base"
    supports_grammar: bool = False

    @abstractmethod
    def stream_chat(self, messages: list[Message], params: GenerationParams) -> AsyncIterator[str]:
        """Genera la respuesta fragmento a fragmento."""

    def describe(self) -> str:
        return self.name

    async def close(self) -> None:  # pragma: no cover - opcional
        return None


# ---------------------------------------------------------------------------
# llama-cpp-python
# ---------------------------------------------------------------------------

_END = object()


class LlamaCppBackend(LLMBackend):
    """Inferencia local con llama-cpp-python sobre un fichero GGUF."""

    name = "llama_cpp"
    supports_grammar = True

    def __init__(self, model_path: str | Path, n_ctx: int = 8192, n_threads: int | None = None,
                 n_gpu_layers: int = -1, chat_format: str | None = None, verbose: bool = False) -> None:
        try:
            from llama_cpp import Llama
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise LLMUnavailableError("llama-cpp-python no está instalado (pip install llama-cpp-python)") from exc
        path = Path(model_path)
        if not path.is_file():
            raise LLMUnavailableError(f"No existe el modelo GGUF: {path}")
        logger.info("Cargando modelo GGUF", extra={"model": str(path), "n_ctx": n_ctx, "n_gpu_layers": n_gpu_layers})
        self.model_path = path
        self.llm = Llama(model_path=str(path), n_ctx=n_ctx, n_threads=n_threads, n_gpu_layers=n_gpu_layers,
                         chat_format=chat_format, verbose=verbose)
        # Llama no es thread-safe: un único hilo dedicado serializa las inferencias
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llama")

    def describe(self) -> str:
        return f"llama_cpp:{self.model_path.name}"

    async def stream_chat(self, messages: list[Message], params: GenerationParams) -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        cancel = threading.Event()

        def put(item: Any) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        def worker() -> None:
            try:
                kwargs: dict[str, Any] = dict(
                    messages=messages, temperature=params.temperature, top_p=params.top_p,
                    max_tokens=params.max_tokens, repeat_penalty=params.repeat_penalty,
                    stop=params.stop or None, stream=True,
                )
                if params.grammar:
                    from llama_cpp import LlamaGrammar
                    kwargs["grammar"] = LlamaGrammar.from_string(params.grammar, verbose=False)
                elif params.json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                for chunk in self.llm.create_chat_completion(**kwargs):
                    if cancel.is_set():
                        break
                    delta = chunk["choices"][0].get("delta", {}).get("content")
                    if delta:
                        put(delta)
            except Exception as exc:  # noqa: BLE001 - se re-lanza en el lado async
                put(exc)
            finally:
                put(_END)

        future = loop.run_in_executor(self._executor, worker)
        try:
            while True:
                item = await queue.get()
                if item is _END:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            cancel.set()
            await asyncio.shield(future)

    async def close(self) -> None:
        self._executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# API compatible OpenAI (Ollama / llama-server)
# ---------------------------------------------------------------------------

class OpenAICompatBackend(LLMBackend):
    """Cliente streaming para /v1/chat/completions."""

    name = "openai"

    def __init__(self, base_url: str, model: str, api_key: str = "none", timeout: float = 300.0) -> None:
        import httpx

        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0),
                                         headers={"Authorization": f"Bearer {api_key}"})
        # llama-server acepta el campo "grammar"; Ollama lo ignora
        self.supports_grammar = True

    def describe(self) -> str:
        return f"openai:{self.model}@{self.base_url}"

    @staticmethod
    async def is_available(base_url: str, timeout: float = 2.0) -> bool:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(f"{base_url.rstrip('/')}/models")
                return resp.status_code < 500
        except httpx.HTTPError:
            return False

    async def stream_chat(self, messages: list[Message], params: GenerationParams) -> AsyncIterator[str]:
        import httpx

        body: dict[str, Any] = {
            "model": self.model, "messages": messages, "stream": True,
            "temperature": params.temperature, "top_p": params.top_p, "max_tokens": params.max_tokens,
            "frequency_penalty": max(0.0, params.repeat_penalty - 1.0),
        }
        if params.stop:
            body["stop"] = params.stop[:4]
        if params.grammar:
            body["grammar"] = params.grammar
        elif params.json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            async with self._client.stream("POST", f"{self.base_url}/chat/completions", json=body) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", errors="replace")[:500]
                    raise LLMUnavailableError(f"El servidor LLM respondió {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if choices:
                        delta = (choices[0].get("delta") or {}).get("content")
                        if delta:
                            yield delta
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"Error de conexión con {self.base_url}: {exc}") from exc

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Backend simulado
# ---------------------------------------------------------------------------

class MockBackend(LLMBackend):
    """Backend determinista para tests/demos sin modelo.

    * Con ``script``: devuelve las respuestas en orden.
    * Sin script: si el último mensaje es una observación de tool, la resume;
      si no, pide un ``system_monitor`` (o ``bash_exec`` si el usuario escribe ``$ comando``).
    """

    name = "mock"

    def __init__(self, script: list[str] | None = None,
                 responder: Callable[[list[Message]], str] | None = None, chunk_size: int = 6) -> None:
        self.script = list(script or [])
        self.responder = responder
        self.chunk_size = chunk_size
        self.calls: list[list[Message]] = []

    def _default(self, messages: list[Message]) -> str:
        last = str(messages[-1].get("content", "")) if messages else ""
        if last.startswith("[OBSERVATION"):
            body = last.split("\n", 1)[1] if "\n" in last else last
            return "Resultado de la herramienta:\n" + body.strip()[:800]
        if last.strip().startswith("$ "):
            cmd = json.dumps(last.strip()[2:])
            return f'Ejecuto el comando.\n<tool>{{"name": "bash_exec", "args": {{"command": {cmd}}}}}</tool>'
        return ('Voy a consultar el estado del sistema.\n'
                '<tool>{"name": "system_monitor", "args": {"sections": ["cpu", "memory", "uptime"]}}</tool>')

    async def stream_chat(self, messages: list[Message], params: GenerationParams) -> AsyncIterator[str]:
        self.calls.append(messages)
        if self.script:
            text = self.script.pop(0)
        elif self.responder:
            text = self.responder(messages)
        else:
            text = self._default(messages)
        # Simular stop sequences como haría el backend real
        for stop in params.stop:
            idx = text.find(stop)
            if idx >= 0:
                text = text[:idx]
        for i in range(0, len(text), self.chunk_size):
            await asyncio.sleep(0)
            yield text[i:i + self.chunk_size]


# ---------------------------------------------------------------------------
# Motor de alto nivel
# ---------------------------------------------------------------------------

class LLMEngine:
    """Fachada sobre un backend: parámetros por defecto, lock y utilidades."""

    def __init__(self, backend: LLMBackend, defaults: GenerationParams | None = None) -> None:
        self.backend = backend
        self.defaults = defaults or GenerationParams()
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self.backend.describe()

    @property
    def supports_grammar(self) -> bool:
        return self.backend.supports_grammar

    async def stream(self, messages: list[Message], **overrides: Any) -> AsyncIterator[str]:
        """Streaming de la respuesta. Las generaciones concurrentes se serializan."""
        params = self.defaults.with_(**overrides) if overrides else self.defaults
        async with self._lock:
            async for chunk in self.backend.stream_chat(messages, params):
                yield chunk

    async def complete(self, messages: list[Message], **overrides: Any) -> str:
        """Respuesta completa (sin streaming)."""
        return "".join([chunk async for chunk in self.stream(messages, **overrides)])

    async def close(self) -> None:
        await self.backend.close()


async def create_engine(config: "AgentConfig") -> LLMEngine:
    """Crea el motor según la configuración, con fallback automático.

    ``auto``: GGUF local con llama-cpp-python -> servidor OpenAI-compatible (Ollama).
    """
    defaults = GenerationParams(temperature=config.temperature, top_p=config.top_p,
                                max_tokens=config.max_tokens, repeat_penalty=config.repeat_penalty)
    choice = config.llm_backend
    errors: list[str] = []

    if choice == "mock":
        return LLMEngine(MockBackend(), defaults)

    if choice in ("auto", "llama_cpp"):
        if Path(config.model_path).is_file():
            try:
                backend = await asyncio.to_thread(
                    LlamaCppBackend, config.model_path, config.n_ctx, config.n_threads,
                    config.n_gpu_layers, config.chat_format,
                )
                return LLMEngine(backend, defaults)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"llama_cpp: {exc}")
                logger.warning("No se pudo cargar llama.cpp", extra={"error": str(exc)})
        else:
            errors.append(f"llama_cpp: no existe {config.model_path}")
        if choice == "llama_cpp":
            raise LLMUnavailableError("; ".join(errors))

    if choice in ("auto", "openai"):
        if await OpenAICompatBackend.is_available(config.openai_base_url):
            logger.info("Usando servidor OpenAI-compatible", extra={"url": config.openai_base_url,
                                                                   "model": config.openai_model})
            return LLMEngine(OpenAICompatBackend(config.openai_base_url, config.openai_model,
                                                 config.openai_api_key, config.openai_timeout), defaults)
        errors.append(f"openai: servidor no disponible en {config.openai_base_url}")

    raise LLMUnavailableError(
        "No hay ningún backend LLM disponible:\n  - " + "\n  - ".join(errors)
        + "\nDescarga un modelo con 'make download-model', arranca Ollama ('ollama serve' + "
          "'ollama pull llama3.1:8b') o usa AGENTOS_LLM_BACKEND=mock para probar."
    )
