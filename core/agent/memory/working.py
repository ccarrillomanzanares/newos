"""Memoria de trabajo: mensajes de la sesión activa listos para el LLM."""

from __future__ import annotations

from typing import Any

from ...llm.context_manager import ContextManager, total_tokens
from ...llm.prompts import format_observation

Message = dict[str, Any]


class WorkingMemory:
    """Historial en RAM de una sesión, ajustado a la ventana de contexto al construir el prompt."""

    def __init__(self, session_id: str, context_manager: ContextManager) -> None:
        self.session_id = session_id
        self.context = context_manager
        self.messages: list[Message] = []

    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, content: str) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def add_observation(self, tool_name: str, observation: str) -> None:
        # Las observaciones se envían con rol "user" para máxima compatibilidad
        # con plantillas de chat (LLaMA 3, Ollama, llama-server).
        self.messages.append({"role": "user", "content": format_observation(tool_name, observation)})

    def add_raw(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})

    def clear(self) -> None:
        self.messages.clear()

    def build(self, system_prompt: str) -> list[Message]:
        """Mensajes finales (system + historial recortado) para enviar al LLM."""
        return self.context.fit(system_prompt, self.messages)

    def load_from_episodic(self, rows: list[dict[str, Any]]) -> None:
        """Reconstruye la memoria de trabajo desde filas de la memoria episódica."""
        self.messages.clear()
        for row in rows:
            role = row.get("role")
            if role == "tool":
                self.add_observation(row.get("tool_name") or "tool", row.get("content") or row.get("tool_result") or "")
            elif role in ("user", "assistant"):
                self.add_raw(role, row.get("content") or "")

    @property
    def token_count(self) -> int:
        return total_tokens(self.messages)
