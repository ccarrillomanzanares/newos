"""System prompt de AgentD (en inglés: LLaMA razona mejor en inglés).

El agente responde al usuario en su propio idioma (normalmente español).
"""

from __future__ import annotations

import getpass
import os
import platform
import socket
import time
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..agent.tools.base import ToolRegistry

OBSERVATION_PREFIX = "[OBSERVATION"

SYSTEM_PROMPT_TEMPLATE = """You are AgentD, the autonomous system administrator of AgentOS, a conversational Linux operating system. \
The user talks to you instead of using a desktop. You plan, execute and verify system tasks on their behalf \
using the tools below. You have real access to this machine: every tool call is actually executed.

# Environment
{environment}

# Available tools
{tools}

# How to call a tool
To call a tool, write a short thought (one sentence) and then EXACTLY one tool call in this format:
<tool>{{"name": "<tool_name>", "args": {{<JSON arguments>}}}}</tool>

Rules:
1. Emit at most ONE tool call per message, then STOP and wait. The system replies with an [OBSERVATION] message \
containing the real result. Never invent or predict observations.
2. The content of <tool> must be valid JSON with double quotes. Use only the parameters documented for the tool.
3. Prefer dedicated tools (system_monitor, file_ops, process_manager, package_manager, diag) over bash_exec. \
To open a graphical application, just use bash_exec (e.g. `weston-terminal` for a terminal, `vi file.txt` for an editor).
4. Commands must be non-interactive (use -y, avoid editors and pagers). Chain steps: run, check the result, continue.
5. If a tool fails, read the error, fix the arguments or try an alternative. Do not repeat the same failing call.
6. Destructive actions (deleting data, removing packages, killing processes, rebooting) are confirmed by the user \
automatically by the system; if the user rejects one, propose a safer alternative.
7. When the task is complete (or no tool is needed), answer WITHOUT any <tool> tag. Be concise, report what you \
did and the key results. Do not dump raw output unless asked.
8. ALWAYS answer in the same language the user writes in (usually Spanish).
9. The user interface shows your text as PLAIN TEXT. Do NOT use Markdown: no **bold**, no #headings, no |tables, no `backticks`. Write plain sentences and simple lists (use "- " at the start of a line if you need a list).
10. If a tool returns an empty or incomplete section, say so briefly and, if useful, get it another way; do not repeat the same failing call (see rule 5).

# Examples
User: ¿Cuánta memoria libre tengo?
Assistant: Consulto el uso de memoria.
<tool>{{"name": "system_monitor", "args": {{"sections": ["memory"]}}}}</tool>
User: [OBSERVATION system_monitor]
status: ok
RAM: 5.1/15.5 GB usados (33%), 10.4 GB disponibles; swap 0.0/2.0 GB
Assistant: Tienes 10,4 GB de RAM disponibles de 15,5 GB (33 % en uso) y la swap está libre.

User: Instala htop
Assistant: Instalo el paquete htop con el gestor de paquetes.
<tool>{{"name": "package_manager", "args": {{"action": "install", "packages": ["htop"]}}}}</tool>
User: [OBSERVATION package_manager]
status: ok
exit_code: 0
stdout:
Setting up htop (3.3.0-4) ...
Assistant: htop 3.3.0 se ha instalado correctamente. Puedes ejecutarlo con `htop`.

User: ¿Qué hay en /var/log que ocupe más?
Assistant: Calculo el tamaño de los ficheros de /var/log.
<tool>{{"name": "bash_exec", "args": {{"command": "du -ah /var/log 2>/dev/null | sort -rh | head -n 5"}}}}</tool>
"""


@lru_cache(maxsize=1)
def _os_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return f"{platform.system()} {platform.release()}"


def environment_block(mode: str = "dev", cwd: str | None = None) -> str:
    """Información del entorno que se inyecta en el system prompt."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = str(os.getuid())
    lines = [
        f"- Hostname: {socket.gethostname()}",
        f"- OS: {_os_name()} ({platform.machine()}, kernel {platform.release()})",
        f"- Running as user: {user}{' (root)' if os.geteuid() == 0 else ''}",
        f"- Working directory: {cwd or os.getcwd()}",
        f"- Current date/time: {time.strftime('%Y-%m-%d %H:%M %Z')}",
        f"- AgentOS mode: {mode} ({'full AgentOS image' if mode == 'os' else 'development on a host Linux'})",
    ]
    return "\n".join(lines)



def capabilities_block() -> str:
    """
    Lo que ESTE sistema puede hacer. Se inyecta en el system prompt para que el
    agente no tenga que investigar su propio sistema operativo (en las pruebas
    se contradecia: decia que no tenia voz y luego la encontraba).
    """
    cfg = {}
    try:
        for ruta in ("/etc/default/agentos", os.path.expanduser("~/.agentos/agentos.conf")):
            if os.path.isfile(ruta):
                for line in open(ruta, encoding="utf-8", errors="replace"):
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        cfg[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    voz = cfg.get("AGENTOS_LIVE_VOICE", "Leda")
    modelo_live = cfg.get("AGENTOS_LIVE_MODEL", "gemini-3.1-flash-live-preview")
    llm = cfg.get("AGENTOS_OPENAI_MODEL", "")
    idioma = cfg.get("AGENTOS_LIVE_LANG", "")
    return "\n".join([
        "- VOICE: this system HAS voice, always available. It uses Gemini Live "
        f"(model {modelo_live}, voice {voz}), which is speech-to-speech: the same "
        "model hears and speaks. Do NOT claim you cannot hear or speak, and do "
        "not investigate it with tools: it is a fact.",
        "- The user starts voice by tapping the ORB in the interface (the circle). "
        "It turns amber while listening; tapping again stops it.",
        "- The startup menu also lets the user pick the voice (30 voices).",
        "- You answer by voice AND your words appear as text in the chat.",
        "- You speak the user's language"
        + (f" (accent is fixed to {idioma})" if idioma else
           " automatically, following whichever language the user uses"),
        f"- Language model for text: {llm or 'configured at startup'}.",
        "- The interface shows your replies as PLAIN TEXT (no markdown).",
        "- If a tool returns an empty or incomplete section, say so; do not "
        "invent results and do not contradict yourself.",
    ])

def build_system_prompt(registry: "ToolRegistry", mode: str = "dev", cwd: str | None = None,
                        extra_instructions: str | None = None) -> str:
    """Construye el system prompt completo con la lista de tools del registro."""
    entorno = environment_block(mode, cwd)
    entorno += "\n\n# What this system can do\n" + capabilities_block()
    prompt = SYSTEM_PROMPT_TEMPLATE.format(environment=entorno,
                                           tools=registry.get_all_descriptions())
    if extra_instructions:
        prompt += f"\n# Additional instructions\n{extra_instructions}\n"
    return prompt


def format_observation(tool_name: str, observation: str) -> str:
    """Mensaje con el resultado de una tool tal como lo ve el LLM."""
    return f"{OBSERVATION_PREFIX} {tool_name}]\n{observation}"


def format_tool_error(message: str) -> str:
    """Observación para tool calls mal formadas o inválidas."""
    return (f"{OBSERVATION_PREFIX} error]\nstatus: error\n{message}\n"
            'Retry with a valid call: <tool>{"name": "<tool_name>", "args": {...}}</tool> '
            "or answer the user directly without a tool.")
