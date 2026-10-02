"""Parser de tool calls generadas por el LLM.

Formato esperado (una llamada por turno):

    <tool>{"name": "bash_exec", "args": {"command": "ls -la"}}</tool>

El parser es tolerante con los errores típicos de modelos pequeños:
etiqueta de cierre ausente (por usar ``</tool>`` como stop sequence),
bloques ```json, claves ``arguments``/``parameters`` y argumentos
serializados como string.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

TOOL_OPEN = "<tool>"
TOOL_CLOSE = "</tool>"

_BLOCK_RE = re.compile(r"<tool>(.*?)(?:</tool>|$)", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class ToolCallError(ValueError):
    """La respuesta contenía un bloque <tool> pero no se pudo interpretar."""

    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


@dataclass
class ToolCall:
    """Llamada a una tool extraída de la respuesta del LLM."""

    name: str
    args: dict[str, Any]
    raw: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "args": self.args}

    def to_tag(self) -> str:
        """Representación canónica para reinsertar en el historial."""
        return f"{TOOL_OPEN}{json.dumps({'name': self.name, 'args': self.args}, ensure_ascii=False)}{TOOL_CLOSE}"


def has_tool_call(text: str) -> bool:
    return TOOL_OPEN in text


def _parse_json_object(payload: str) -> dict[str, Any]:
    payload = _FENCE_RE.sub("", payload.strip()).strip()
    start = payload.find("{")
    if start < 0:
        raise ToolCallError("No hay ningún objeto JSON dentro de <tool>", payload)
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(payload[start:])
    except json.JSONDecodeError as exc:
        # Reparación mínima: comas finales y comillas simples
        repaired = re.sub(r",\s*([}\]])", r"\1", payload[start:])
        try:
            obj, _ = decoder.raw_decode(repaired)
        except json.JSONDecodeError:
            raise ToolCallError(f"JSON inválido en tool call: {exc.msg} (pos {exc.pos})", payload) from exc
    if not isinstance(obj, dict):
        raise ToolCallError("El contenido de <tool> debe ser un objeto JSON", payload)
    return obj


def parse_tool_call_block(payload: str) -> ToolCall:
    """Convierte el contenido de un bloque <tool> en ToolCall."""
    obj = _parse_json_object(payload)
    name = obj.get("name") or obj.get("tool") or obj.get("function")
    if isinstance(name, dict):  # {"function": {"name":..., "arguments":...}}
        obj, name = name, name.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ToolCallError("Falta el campo 'name' en la tool call", payload)
    args = obj.get("args", obj.get("arguments", obj.get("parameters", {})))
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError as exc:
            raise ToolCallError(f"'args' no es JSON válido: {exc.msg}", payload) from exc
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolCallError("'args' debe ser un objeto JSON", payload)
    return ToolCall(name=name.strip(), args=args, raw=payload)


def parse_tool_calls(text: str) -> list[ToolCall]:
    """Extrae todas las tool calls de un texto. Lanza ToolCallError si alguna es inválida."""
    if TOOL_OPEN not in text:
        return []
    return [parse_tool_call_block(m.group(1)) for m in _BLOCK_RE.finditer(text)]


def strip_tool_calls(text: str) -> str:
    """Devuelve el texto visible (sin bloques <tool>)."""
    return _BLOCK_RE.sub("", text).strip()


# ---------------------------------------------------------------------------
# Validación contra JSON Schema (subconjunto suficiente para las tools)
# ---------------------------------------------------------------------------

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
    "array": (list,), "object": (dict,),
}


def _coerce(value: Any, expected: str) -> Any:
    """Coerción suave de tipos habituales en LLMs ("30" -> 30, "true" -> True)."""
    if expected == "integer" and isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    if expected == "integer" and isinstance(value, float) and value.is_integer():
        return int(value)
    if expected == "number" and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    if expected == "boolean" and isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if expected == "array" and isinstance(value, str):
        return [value]
    if expected == "string" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return value


def validate_args(schema: dict[str, Any], args: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Valida y normaliza argumentos. Devuelve (args_normalizados, errores)."""
    errors: list[str] = []
    props: dict[str, Any] = schema.get("properties", {})
    out: dict[str, Any] = {}
    for req in schema.get("required", []):
        if req not in args or args[req] is None:
            errors.append(f"falta el parámetro obligatorio '{req}'")
    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            if schema.get("additionalProperties", False) is False:
                errors.append(f"parámetro desconocido '{key}' (válidos: {', '.join(props) or 'ninguno'})")
            continue
        if value is None:
            continue
        expected = spec.get("type")
        if expected:
            value = _coerce(value, expected)
            py_types = _TYPES.get(expected, (object,))
            if not isinstance(value, py_types) or (expected in {"integer", "number"} and isinstance(value, bool)):
                errors.append(f"'{key}' debe ser de tipo {expected}, recibido {type(value).__name__}")
                continue
            if expected == "array" and "items" in spec:
                item_spec = spec["items"]
                value = [_coerce(v, item_spec.get("type", "")) for v in value]
                if "enum" in item_spec:
                    bad = [v for v in value if v not in item_spec["enum"]]
                    if bad:
                        errors.append(f"valores no válidos en '{key}': {bad}; permitidos: {item_spec['enum']}")
        if "enum" in spec and value not in spec["enum"]:
            errors.append(f"'{key}' debe ser uno de {spec['enum']}, recibido {value!r}")
        out[key] = value
    return out, errors


# ---------------------------------------------------------------------------
# Filtro de streaming: oculta los bloques <tool> al usuario en tiempo real
# ---------------------------------------------------------------------------

class ToolStreamFilter:
    """Recibe fragmentos del LLM y devuelve solo el texto visible.

    Gestiona etiquetas partidas entre fragmentos (p. ej. "<to" + "ol>").
    """

    def __init__(self) -> None:
        self._pending = ""
        self._inside = False

    def feed(self, chunk: str) -> str:
        self._pending += chunk
        visible: list[str] = []
        while self._pending:
            if self._inside:
                end = self._pending.find(TOOL_CLOSE)
                if end < 0:
                    # Conservar un posible prefijo de </tool> al final
                    self._pending = self._pending[-(len(TOOL_CLOSE) - 1):]
                    break
                self._pending = self._pending[end + len(TOOL_CLOSE):]
                self._inside = False
                continue
            start = self._pending.find(TOOL_OPEN)
            if start >= 0:
                visible.append(self._pending[:start])
                self._pending = self._pending[start + len(TOOL_OPEN):]
                self._inside = True
                continue
            # ¿El final podría ser el comienzo de "<tool>"?
            keep = 0
            for i in range(1, len(TOOL_OPEN)):
                if self._pending.endswith(TOOL_OPEN[:i]):
                    keep = i
            visible.append(self._pending[: len(self._pending) - keep])
            self._pending = self._pending[len(self._pending) - keep:]
            break
        return "".join(visible)

    def flush(self) -> str:
        rest = "" if self._inside else self._pending
        self._pending = ""
        return rest


# ---------------------------------------------------------------------------
# Gramática GBNF para salida restringida (llama.cpp)
# ---------------------------------------------------------------------------

_GBNF_JSON = r'''
object ::= "{" ws ( pair ( ws "," ws pair )* )? ws "}"
pair ::= string ws ":" ws value
value ::= object | array | string | number | "true" | "false" | "null"
array ::= "[" ws ( value ( ws "," ws value )* )? ws "]"
string ::= "\"" ( [^"\\\x7F\x00-\x1F] | "\\" ( ["\\/bfnrt] | "u" hex hex hex hex ) )* "\""
hex ::= [0-9a-fA-F]
number ::= "-"? ( "0" | [1-9] [0-9]* ) ( "." [0-9]+ )? ( [eE] [-+]? [0-9]+ )?
ws ::= [ \t\n]*
'''


def build_tool_call_grammar(tool_names: list[str]) -> str:
    """Gramática GBNF que obliga al modelo a emitir exactamente una tool call válida."""
    names = " | ".join(f'"\\"{n}\\""' for n in tool_names) or "string"
    root = ('root ::= "<tool>" ws "{" ws "\\"name\\"" ws ":" ws toolname ws "," ws '
            '"\\"args\\"" ws ":" ws object ws "}" ws "</tool>"\n')
    return root + f"toolname ::= {names}\n" + _GBNF_JSON.strip() + "\n"
