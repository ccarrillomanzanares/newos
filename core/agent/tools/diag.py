"""Tool ``diag``: recoge un diagnostico del sistema y lo sube a la VPS.

El agente puede pedirla cuando algo va mal (audio, red, arranque, video): junta
sistema, logs, audio, GPU y errores del kernel en un tar.zst y lo sube a la
cuenta 'diag' de la VPS (solo escritura, con rrsync). Es la pieza que evita
"a ciegas, dime lo que ves": con esto el diagnostico se trae entero.

El trabajo lo hace el script ``agentos-diag`` (en /usr/bin), que se puede
ejecutar tambien a mano desde la consola.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from .base import Tool, ToolResult

SCRIPT = shutil.which("agentos-diag") or "/usr/bin/agentos-diag"


class DiagTool(Tool):
    name = "diag"
    description = (
        "Collect a full system diagnostic of AgentOS and upload it to the server. "
        "It gathers system info, agent/interface logs, audio (micro and speaker), GPU/compositor "
        "state, loaded modules and kernel errors into one compressed bundle, and uploads it. "
        "Use it when something does not work (no sound, no sound capture, no network, no image) "
        "or when the user asks for a diagnosis/report."
    )
    parameters = {
        "type": "object",
        "properties": {
            "upload": {
                "type": "boolean",
                "description": "Upload the bundle to the server (default true). Set false to only build it locally.",
                "default": True,
            },
        },
        "required": [],
    }

    async def execute(self, upload: bool = True) -> ToolResult:
        if not Path(SCRIPT).is_file():
            return ToolResult(False, error=f"no existe el script de diagnostico ({SCRIPT})")
        args = [SCRIPT] if upload else [SCRIPT, "--local"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            return ToolResult(False, error="el diagnostico tardo mas de 180 s")
        except OSError as exc:
            return ToolResult(False, error=f"no se pudo ejecutar el diagnostico: {exc}")
        texto = (out or b"").decode("utf-8", "replace").strip()
        ok = proc.returncode == 0
        subido = "Subido a" in texto
        return ToolResult(ok, output=texto,
                          data={"uploaded": subido, "returncode": proc.returncode},
                          return_code=proc.returncode)
