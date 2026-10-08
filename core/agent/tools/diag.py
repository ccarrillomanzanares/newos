"""Tool ``diag``: recoge un diagnostico del sistema y lo sube a la VPS.

El agente puede pedirla cuando algo va mal (audio, red, arranque, video, CPU de
la interfaz): junta sistema, logs, audio, GPU y errores del kernel en un tar.zst
y lo sube a la cuenta 'diag' de la VPS (solo escritura, con rrsync).

DOS COSAS QUE IMPORTAN (costaron tres diagnosticos perdidos):
  1. **Se lanza en SEGUNDO PLANO.** El diagnostico incluye pruebas de audio y de
     voz y tarda mas que el timeout de `bash_exec` (30 s): si se ejecuta en primer
     plano, lo matan a mitad y no sube nada (paso 3 veces).
  2. **Se espera hasta 15 MINUTOS** y se comprueba que el fichero haya llegado de
     verdad antes de decir "subido".
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from .base import Tool, ToolResult

SCRIPT = shutil.which("agentos-diag") or "/usr/bin/agentos-diag"
# El diagnostico tarda: pruebas de audio (~4 s), de voz (~30 s) y recolecta logs.
# En segundo plano no estorba, pero hay que darle tiempo de sobra.
TIMEOUT = 900


class DiagTool(Tool):
    name = "diag"
    description = (
        "Collect a full system diagnostic of AgentOS and upload it to the server. "
        "It gathers system info, agent/interface logs, audio (mixer state, micro and speaker), "
        "GPU/compositor and cog CPU usage, loaded modules and kernel errors into one compressed "
        "bundle, and uploads it. It runs in the BACKGROUND: it returns immediately and the bundle "
        "takes a couple of minutes to arrive. Use it when something does not work (no sound, no "
        "sound capture, no network, no image, high CPU) or when the user asks for a diagnosis."
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
        # setsid lo separa de nuestro grupo de procesos: aunque el agente o su
        # timeout desaparezcan, el diagnostico sigue y termina de subir.
        try:
            proc = await asyncio.create_subprocess_exec(
                "setsid", *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True)
        except OSError as exc:
            return ToolResult(False, error=f"no se pudo lanzar el diagnostico: {exc}")

        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
        except asyncio.TimeoutError:
            return ToolResult(False, error="el diagnostico tardo mas de 15 min; "
                                            "sigue en segundo plano, mira /tmp/agentos-diag-*.tar.*")
        texto = (out or b"").decode("utf-8", "replace").strip()
        ok = proc.returncode == 0
        subido = ("Subido a" in texto) or ("Subido por" in texto)
        aviso = "" if subido else "\n[AVISO: el diagnostico se genero pero NO consta que haya subido]"
        return ToolResult(ok, output=texto + aviso,
                          data={"uploaded": subido, "returncode": proc.returncode},
                          return_code=proc.returncode)

