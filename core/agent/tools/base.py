"""Sistema base de tools: clase ``Tool``, ``ToolResult`` y ``ToolRegistry``."""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("agentos.tools")


class RiskLevel(str, Enum):
    """Nivel de riesgo de una tool (ver sección 7.3 de la arquitectura)."""

    SAFE = "safe"
    ELEVATED = "elevated"
    DESTRUCTIVE = "destructive"


@dataclass
class ToolResult:
    """Resultado de ejecutar una tool."""

    success: bool
    output: str = ""
    error: str | None = None
    data: dict[str, Any] | None = None
    return_code: int | None = None
    duration_ms: int = 0

    def to_observation(self, limit: int = 4000) -> str:
        """Texto que se devuelve al LLM como observación (truncado si es largo)."""
        parts: list[str] = [f"status: {'ok' if self.success else 'error'}"]
        if self.return_code is not None:
            parts.append(f"exit_code: {self.return_code}")
        if self.output:
            parts.append(self.output.rstrip())
        if self.error:
            parts.append(f"error: {self.error.rstrip()}")
        return truncate_middle("\n".join(parts), limit)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "data": self.data,
            "return_code": self.return_code,
            "duration_ms": self.duration_ms,
        }


def truncate_middle(text: str, limit: int) -> str:
    """Trunca por el medio conservando el principio y el final del texto."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head - 40
    omitted = len(text) - head - max(tail, 0)
    return f"{text[:head]}\n... [{omitted} caracteres omitidos] ...\n{text[-tail:] if tail > 0 else ''}"


class Tool(ABC):
    """Clase base de todas las tools del agente.

    Subclases deben definir ``name``, ``description`` y ``parameters``
    (JSON Schema de tipo object) e implementar ``execute``.
    """

    name: str = ""
    description: str = ""  # Texto que se muestra al LLM
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    requires_confirmation: bool = False  # Acción destructiva siempre confirmada
    risk: RiskLevel = RiskLevel.SAFE

    def needs_confirmation(self, args: dict[str, Any]) -> str | None:
        """Devuelve un motivo si esta invocación concreta requiere confirmación.

        Por defecto depende de ``requires_confirmation``; las subclases pueden
        decidir en función de los argumentos (p. ej. ``rm -rf``).
        """
        if self.requires_confirmation or self.risk == RiskLevel.DESTRUCTIVE:
            return f"La tool '{self.name}' está marcada como destructiva."
        return None

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """Ejecuta la tool con argumentos ya validados."""

    def schema(self) -> dict[str, Any]:
        """Esquema estilo OpenAI function-calling."""
        return {"name": self.name, "description": self.description, "parameters": self.parameters}

    def describe(self) -> str:
        """Descripción compacta para el system prompt."""
        props = self.parameters.get("properties", {})
        required = set(self.parameters.get("required", []))
        lines = [f"### {self.name}", self.description.strip()]
        if props:
            lines.append("Parameters:")
            for pname, spec in props.items():
                ptype = spec.get("type", "any")
                if "enum" in spec:
                    ptype += " (one of: " + ", ".join(map(str, spec["enum"])) + ")"
                req = "required" if pname in required else "optional"
                default = f", default={spec['default']!r}" if "default" in spec else ""
                lines.append(f"  - {pname}: {ptype}, {req}{default}. {spec.get('description', '')}".rstrip())
        else:
            lines.append("Parameters: none")
        return "\n".join(lines)


class ToolRegistry:
    """Registro central de tools disponibles para el agente."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if not tool.name:
            raise ValueError("La tool debe tener nombre")
        if tool.name in self._tools:
            logger.warning("Tool sobrescrita en el registro", extra={"tool": tool.name})
        self._tools[tool.name] = tool
        logger.debug("Tool registrada", extra={"tool": tool.name})
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def get_all_descriptions(self) -> str:
        """Bloque de texto con todas las tools para el system prompt."""
        return "\n\n".join(tool.describe() for tool in self._tools.values())

    def get_schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    async def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Ejecuta una tool capturando cualquier excepción y midiendo duración."""
        tool = self.get(name)
        if tool is None:
            return ToolResult(False, error=f"Tool desconocida '{name}'. Disponibles: {', '.join(self.names())}")
        start = time.monotonic()
        try:
            result = await tool.execute(**args)
        except TypeError as exc:  # argumentos inesperados
            result = ToolResult(False, error=f"Argumentos inválidos para {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - una tool nunca debe tumbar el agente
            logger.exception("Fallo ejecutando tool", extra={"tool": name})
            result = ToolResult(False, error=f"{type(exc).__name__}: {exc}")
        result.duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "Tool ejecutada",
            extra={"tool": name, "success": result.success, "duration_ms": result.duration_ms,
                   "args": json.dumps(args, ensure_ascii=False)[:300]},
        )
        return result
