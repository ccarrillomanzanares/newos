"""AgentD: loop ReAct principal del agente.

Ciclo por mensaje del usuario:

1. Guarda el mensaje (memoria episódica + memoria de trabajo).
2. Construye el prompt: system prompt con tools + historial ajustado al contexto.
3. Llama al LLM en streaming (los bloques <tool> se ocultan al usuario).
4. Si la respuesta contiene una tool call: valida, pide confirmación si es
   destructiva, ejecuta, registra en el audit log y añade la observación.
5. Repite hasta que el LLM da una respuesta final sin tool calls
   (o se alcanza ``max_iterations``).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..llm.context_manager import ContextManager
from ..llm.inference import LLMEngine, create_engine
from ..llm.prompts import build_system_prompt, format_tool_error
from ..llm.tool_calling import (
    TOOL_CLOSE, ToolCall, ToolCallError, ToolStreamFilter, build_tool_call_grammar, parse_tool_calls,
    strip_tool_calls, validate_args,
)
from .config import AgentConfig, get_config
from .memory.episodic import EpisodicMemory, new_session_id
from .memory.working import WorkingMemory
from .tools import ToolRegistry, ToolResult, build_default_registry

logger = logging.getLogger("agentos.agent")

# Callback de confirmación: recibe la tool call y el motivo, devuelve True si el usuario aprueba.
ConfirmCallback = Callable[[ToolCall, str], Awaitable[bool]]


@dataclass
class AgentEvent:
    """Evento emitido durante el procesamiento de un mensaje.

    Tipos: token, tool_call, confirm_request, tool_result, error, final.
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, **self.data}


class AuditLog:
    """Audit log append-only en formato JSON Lines (sección 7.5 de la arquitectura)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, session_id: str, call: ToolCall, user_confirmed: bool | None, result: ToolResult) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session_id": session_id,
            "tool": call.name,
            "arguments": call.args,
            "user_confirmed": user_confirmed,
            "result_code": result.return_code if result.return_code is not None else (0 if result.success else 1),
            "success": result.success,
            "duration_ms": result.duration_ms,
        }
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("No se pudo escribir el audit log", extra={"path": str(self.path)})


class Agent:
    """Agente ReAct de AgentOS."""

    def __init__(
        self,
        config: AgentConfig | None = None,
        llm: LLMEngine | None = None,
        registry: ToolRegistry | None = None,
        episodic: EpisodicMemory | None = None,
        confirm_callback: ConfirmCallback | None = None,
    ) -> None:
        self.config = config or get_config()
        self.llm = llm
        self.registry = registry or build_default_registry(self.config)
        self.episodic = episodic or EpisodicMemory(self.config.db_path)
        self.confirm_callback = confirm_callback
        self.audit = AuditLog(self.config.audit_log_path)
        self.context = ContextManager(
            max_tokens=self.config.n_ctx,
            reserve_tokens=min(self.config.context_reserve_tokens, self.config.max_tokens),
            keep_last_n=self.config.keep_last_messages,
        )
        self._sessions: dict[str, WorkingMemory] = {}
        self._started = False

    # ---------------------------------------------------------------- ciclo de vida
    async def start(self) -> None:
        """Inicializa la base de datos y el LLM (si no se inyectó uno)."""
        if self._started:
            return
        self.config.ensure_dirs()
        await self.episodic.init()
        if self.llm is None:
            self.llm = await create_engine(self.config)
        logger.info("AgentD listo", extra={"llm": self.llm.name, "tools": self.registry.names(),
                                           "mode": self.config.mode})
        self._started = True

    async def close(self) -> None:
        if self.llm is not None:
            await self.llm.close()
        await self.episodic.close()
        self._started = False

    # ---------------------------------------------------------------- sesiones
    def new_session(self) -> str:
        session_id = new_session_id()
        self._sessions[session_id] = WorkingMemory(session_id, self.context)
        return session_id

    async def get_working_memory(self, session_id: str) -> WorkingMemory:
        """Devuelve la memoria de trabajo, recuperándola de SQLite si la sesión ya existía."""
        wm = self._sessions.get(session_id)
        if wm is None:
            wm = WorkingMemory(session_id, self.context)
            rows = await self.episodic.get_session_history(session_id, limit=200)
            if rows:
                wm.load_from_episodic(rows)
                logger.info("Sesión restaurada", extra={"session": session_id, "messages": len(rows)})
            self._sessions[session_id] = wm
        return wm

    def forget_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def system_prompt(self) -> str:
        return build_system_prompt(self.registry, mode=self.config.mode, cwd=str(self.config.bash_cwd))

    # ---------------------------------------------------------------- API pública
    async def chat(self, text: str, session_id: str | None = None,
                   on_token: Callable[[str], None] | None = None) -> str:
        """Procesa un mensaje y devuelve la respuesta final (conveniencia sin eventos)."""
        final = ""
        async for event in self.stream(text, session_id or self.new_session()):
            if event.type == "token" and on_token:
                on_token(event.data["text"])
            elif event.type == "final":
                final = event.data["text"]
        return final

    async def stream(self, text: str, session_id: str,
                     confirm: ConfirmCallback | None = None) -> AsyncIterator[AgentEvent]:
        """Loop ReAct completo como generador asíncrono de eventos."""
        await self.start()
        assert self.llm is not None
        confirm = confirm or self.confirm_callback
        wm = await self.get_working_memory(session_id)
        text = text.strip()
        wm.add_user(text)
        await self.episodic.add_message(session_id, "user", text)
        logger.info("Mensaje de usuario", extra={"session": session_id, "chars": len(text)})

        use_grammar = False
        system_prompt = self.system_prompt()
        for iteration in range(1, self.config.max_iterations + 1):
            messages = wm.build(system_prompt)
            overrides: dict[str, Any] = {"stop": [TOOL_CLOSE, "[OBSERVATION"]}
            if use_grammar and self.llm.supports_grammar:
                # Reintento tras una tool call mal formada: salida restringida por gramática
                overrides["grammar"] = build_tool_call_grammar(self.registry.names())
                overrides["stop"] = []
            use_grammar = False

            # --- 1) Generación en streaming
            filt = ToolStreamFilter()
            chunks: list[str] = []
            started = time.monotonic()
            try:
                async for chunk in self.llm.stream(messages, **overrides):
                    chunks.append(chunk)
                    visible = filt.feed(chunk)
                    if visible:
                        yield AgentEvent("token", {"text": visible})
            except Exception as exc:  # noqa: BLE001
                logger.exception("Error del LLM")
                msg = f"Error del modelo de lenguaje: {exc}"
                yield AgentEvent("error", {"message": msg})
                yield AgentEvent("final", {"text": msg, "iterations": iteration})
                return
            tail = filt.flush()
            if tail:
                yield AgentEvent("token", {"text": tail})
            raw = "".join(chunks)
            logger.debug("Respuesta LLM", extra={"iteration": iteration, "chars": len(raw),
                                                  "secs": round(time.monotonic() - started, 2)})

            # --- 2) ¿Hay tool calls?
            try:
                calls = parse_tool_calls(raw)
            except ToolCallError as exc:
                logger.warning("Tool call mal formada", extra={"error": str(exc)})
                wm.add_assistant(raw.strip())
                wm.add_observation("error", format_tool_error(str(exc)))
                await self.episodic.add_message(session_id, "assistant", raw.strip())
                yield AgentEvent("error", {"message": f"Tool call inválida: {exc}"})
                use_grammar = True
                continue

            if not calls:
                final = raw.strip() or "(sin respuesta)"
                wm.add_assistant(final)
                await self.episodic.add_message(session_id, "assistant", final)
                yield AgentEvent("final", {"text": final, "iterations": iteration})
                return

            # Solo se ejecuta la primera tool call por turno (disciplina ReAct)
            call = calls[0]
            thought = strip_tool_calls(raw)
            assistant_msg = (thought + "\n" if thought else "") + call.to_tag()
            wm.add_assistant(assistant_msg)
            await self.episodic.add_message(session_id, "assistant", assistant_msg, tool_name=call.name)

            # --- 3) Ejecutar la tool
            result, confirmed = None, None
            async for event in self._run_tool(session_id, call, confirm):
                if event.type == "_result":
                    result, confirmed = event.data["result"], event.data["confirmed"]
                else:
                    yield event
            assert result is not None
            observation = result.to_observation(self.config.tool_output_limit)
            wm.add_observation(call.name, observation)
            await self.episodic.add_message(session_id, "tool", observation, tool_name=call.name,
                                            tool_result=json.dumps(result.to_dict(), ensure_ascii=False,
                                                                   default=str)[:20000])
            yield AgentEvent("tool_result", {"id": call.id, "name": call.name, "success": result.success,
                                             "output": observation, "duration_ms": result.duration_ms,
                                             "confirmed": confirmed})

        final = ("He alcanzado el límite de pasos para esta tarea sin una respuesta final. "
                 "Revisa los resultados anteriores o reformula la petición.")
        wm.add_assistant(final)
        await self.episodic.add_message(session_id, "assistant", final)
        yield AgentEvent("final", {"text": final, "iterations": self.config.max_iterations})

    # ---------------------------------------------------------------- ejecución de tools
    async def _run_tool(self, session_id: str, call: ToolCall,
                        confirm: ConfirmCallback | None) -> AsyncIterator[AgentEvent]:
        """Valida, confirma y ejecuta una tool. Emite un evento interno '_result' al final."""
        tool = self.registry.get(call.name)
        if tool is None:
            result = ToolResult(False, error=f"Tool desconocida '{call.name}'. "
                                             f"Disponibles: {', '.join(self.registry.names())}")
            yield AgentEvent("tool_call", {**call.to_dict(), "valid": False})
            yield AgentEvent("_result", {"result": result, "confirmed": None})
            return

        args, errors = validate_args(tool.parameters, call.args)
        call.args = args
        yield AgentEvent("tool_call", {**call.to_dict(), "valid": not errors})
        if errors:
            result = ToolResult(False, error="Argumentos inválidos: " + "; ".join(errors))
            yield AgentEvent("_result", {"result": result, "confirmed": None})
            return

        confirmed: bool | None = None
        reason = tool.needs_confirmation(args)
        if reason:
            if self.config.auto_confirm:
                confirmed = True
                logger.warning("Acción destructiva auto-confirmada", extra={"tool": call.name, "reason": reason})
            else:
                yield AgentEvent("confirm_request", {**call.to_dict(), "reason": reason})
                confirmed = bool(await confirm(call, reason)) if confirm else False
            if not confirmed:
                result = ToolResult(False, error=f"El usuario NO autorizó esta acción ({reason}). "
                                                 "No la repitas; propone una alternativa segura o pregunta.")
                self.audit.record(session_id, call, False, result)
                yield AgentEvent("_result", {"result": result, "confirmed": False})
                return

        result = await self.registry.execute(call.name, args)
        self.audit.record(session_id, call, confirmed, result)
        yield AgentEvent("_result", {"result": result, "confirmed": confirmed})
