"""Gestión de la ventana de contexto del LLM.

* Cuenta tokens de forma aproximada (caracteres / 4).
* Cuando el historial se acerca al límite (8192 por defecto) descarta los
  mensajes más antiguos y los sustituye por un resumen compacto.
* Siempre mantiene el system prompt y los últimos N mensajes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("agentos.context")

Message = dict[str, Any]
MESSAGE_OVERHEAD_TOKENS = 4  # cabeceras de rol de la plantilla de chat


def estimate_tokens(text: str) -> int:
    """Aproximación barata: 1 token ~ 4 caracteres."""
    return (len(text) + 3) // 4


def message_tokens(message: Message) -> int:
    return estimate_tokens(str(message.get("content") or "")) + MESSAGE_OVERHEAD_TOKENS


def total_tokens(messages: list[Message]) -> int:
    return sum(message_tokens(m) for m in messages)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.7)
    tail = max(max_chars - head - 60, 0)
    return f"{text[:head]}\n...[truncado para caber en el contexto]...\n{text[-tail:] if tail else ''}"


def extractive_summary(messages: list[Message], max_chars: int = 1200) -> str:
    """Resumen extractivo sin LLM: primera línea de cada mensaje descartado."""
    lines: list[str] = []
    for m in messages:
        content = " ".join(str(m.get("content") or "").split())
        if not content:
            continue
        role = m.get("role", "?")
        lines.append(f"- {role}: {content[:160]}{'…' if len(content) > 160 else ''}")
    summary = "\n".join(lines)
    return summary if len(summary) <= max_chars else "…\n" + summary[-max_chars:]


class ContextManager:
    """Ajusta el historial para que quepa en la ventana de contexto."""

    def __init__(
        self,
        max_tokens: int = 8192,
        reserve_tokens: int = 1024,
        keep_last_n: int = 6,
        max_message_tokens: int = 1500,
        summarizer: Callable[[list[Message]], str] | None = None,
    ) -> None:
        self.max_tokens = max_tokens
        self.reserve_tokens = reserve_tokens  # espacio para la respuesta del modelo
        self.keep_last_n = max(1, keep_last_n)
        self.max_message_tokens = max_message_tokens
        self.summarizer = summarizer or extractive_summary

    @property
    def budget(self) -> int:
        return self.max_tokens - self.reserve_tokens

    def fit(self, system_prompt: str, history: list[Message]) -> list[Message]:
        """Devuelve [system] + historial recortado que cabe en el presupuesto."""
        system = {"role": "system", "content": system_prompt}
        available = self.budget - message_tokens(system)
        if available <= 0:
            logger.warning("El system prompt no deja espacio para el historial")
            available = max(self.budget // 4, 256)

        msgs = [dict(m) for m in history]
        # 1) Recortar mensajes individuales enormes (salidas de tools), excepto el último
        for m in msgs[:-1]:
            if message_tokens(m) > self.max_message_tokens:
                m["content"] = _truncate_to_tokens(str(m.get("content") or ""), self.max_message_tokens)

        if total_tokens(msgs) <= available:
            return [system, *msgs]

        # 2) Descartar los más antiguos manteniendo siempre los últimos N
        dropped: list[Message] = []
        while len(msgs) > self.keep_last_n and total_tokens(msgs) > available:
            dropped.append(msgs.pop(0))
        # Evitar empezar con una observación huérfana: el primer mensaje debe ser del usuario
        while len(msgs) > 1 and msgs[0].get("role") != "user" and len(msgs) > self.keep_last_n // 2:
            dropped.append(msgs.pop(0))

        # 3) Insertar resumen de lo descartado si cabe
        if dropped:
            summary_text = self.summarizer(dropped)
            summary = {"role": "user",
                       "content": f"[Summary of earlier conversation, for context only]\n{summary_text}"}
            if total_tokens(msgs) + message_tokens(summary) <= available:
                msgs.insert(0, summary)
                # La plantilla de LLaMA requiere alternancia razonable: añadir acuse del asistente
                msgs.insert(1, {"role": "assistant", "content": "Understood, I have the earlier context."})
            logger.info("Contexto truncado", extra={"dropped": len(dropped), "kept": len(msgs)})

        # 4) Si aún no cabe, truncar el contenido de los mensajes conservados (del más antiguo al más nuevo)
        for m in msgs:
            excess = total_tokens(msgs) - available
            if excess <= 0:
                break
            current = message_tokens(m)
            m["content"] = _truncate_to_tokens(str(m.get("content") or ""), max(current - excess, 64))
        return [system, *msgs]

    def usage(self, messages: list[Message]) -> dict[str, int]:
        used = total_tokens(messages)
        return {"used": used, "max": self.max_tokens, "free": self.max_tokens - used}
