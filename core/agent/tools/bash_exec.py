"""Tool ``bash_exec``: ejecuta comandos de shell con timeout y captura de salida."""

from __future__ import annotations

import asyncio
import os
import re
import signal
from pathlib import Path
from typing import Any

from .base import RiskLevel, Tool, ToolResult

# Patrones de comandos potencialmente destructivos que exigen confirmación.
DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+", "borrado recursivo/forzado (rm -r/-f)"),
    (r"\bmkfs(\.\w+)?\b", "formateo de sistema de ficheros (mkfs)"),
    (r"\bdd\s+.*\bof=/dev/", "escritura directa a dispositivo (dd of=/dev/...)"),
    (r">\s*/dev/(sd|nvme|hd|vd|mmcblk)", "redirección a dispositivo de bloque"),
    (r"\b(fdisk|sfdisk|parted|gdisk|wipefs)\b", "particionado / borrado de disco"),
    (r"\b(shutdown|reboot|poweroff|halt)\b", "apagado o reinicio del sistema"),
    (r"\binit\s+[06]\b", "cambio de runlevel (apagado/reinicio)"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb"),
    (r"\bchmod\s+(-R\s+)?[0-7]*777\s+/", "permisos 777 sobre rutas del sistema"),
    (r"\bchown\s+-R\b.*\s/(\s|$)", "chown recursivo sobre /"),
    (r"\b(iptables|nft|ufw)\b.*\b(-F|flush|--flush|reset|disable)\b", "vaciado/desactivación del firewall"),
    (r"\bsystemctl\s+(stop|disable|mask)\s+(ssh|sshd|networking|NetworkManager|agentd)\b", "parada de servicio crítico"),
    (r"\b(userdel|deluser|passwd)\b", "gestión de cuentas de usuario"),
    (r"\b(apt|apt-get|dnf|yum|pacman)\b.*\b(remove|purge|autoremove|-R)\b", "desinstalación de paquetes"),
    (r"\bcurl\b.*\|\s*(sudo\s+)?(ba)?sh\b", "ejecución de script remoto (curl | sh)"),
    (r"\bwget\b.*\|\s*(sudo\s+)?(ba)?sh\b", "ejecución de script remoto (wget | sh)"),
    (r">\s*/etc/(passwd|shadow|sudoers|fstab)", "sobrescritura de fichero crítico de /etc"),
]
_COMPILED = [(re.compile(p), reason) for p, reason in DANGEROUS_PATTERNS]


def detect_dangerous(command: str) -> str | None:
    """Devuelve el motivo si el comando coincide con algún patrón peligroso."""
    for pattern, reason in _COMPILED:
        if pattern.search(command):
            return reason
    return None


class BashExecTool(Tool):
    name = "bash_exec"
    description = (
        "Execute a shell command (bash) on the local Linux system and return stdout, stderr and the exit code. "
        "Use it for any system administration task that has no dedicated tool. Commands run non-interactively: "
        "always pass flags like -y and never start interactive programs (vim, top, less)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "timeout": {"type": "integer", "description": "Timeout in seconds.", "default": 30},
            "cwd": {"type": "string", "description": "Working directory for the command."},
        },
        "required": ["command"],
    }
    risk = RiskLevel.ELEVATED

    def __init__(self, default_timeout: int = 30, default_cwd: str | Path | None = None,
                 max_output_chars: int = 20000) -> None:
        self.default_timeout = default_timeout
        self.default_cwd = Path(default_cwd).expanduser() if default_cwd else Path.home()
        self.max_output_chars = max_output_chars

    def needs_confirmation(self, args: dict[str, Any]) -> str | None:
        reason = detect_dangerous(str(args.get("command", "")))
        return f"Comando potencialmente destructivo: {reason}" if reason else None

    async def execute(self, command: str, timeout: int | None = None, cwd: str | None = None) -> ToolResult:
        timeout = int(timeout or self.default_timeout)
        workdir = Path(cwd).expanduser() if cwd else self.default_cwd
        if not workdir.is_dir():
            return ToolResult(False, error=f"El directorio de trabajo no existe: {workdir}")

        env = os.environ.copy()
        env.setdefault("DEBIAN_FRONTEND", "noninteractive")
        env["PAGER"] = "cat"
        env["SYSTEMD_PAGER"] = ""

        # start_new_session=True crea un grupo de procesos propio para poder
        # matar también a los hijos si se agota el timeout.
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "-c", command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workdir),
            env=env,
            start_new_session=True,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            self._kill_group(proc)
            stdout_b, stderr_b = await proc.communicate()
            return ToolResult(
                False,
                output=self._format(stdout_b, stderr_b),
                error=f"Timeout: el comando superó {timeout}s y fue terminado",
                return_code=-9,
                data={"command": command, "timed_out": True},
            )
        except asyncio.CancelledError:
            self._kill_group(proc)
            raise

        rc = proc.returncode if proc.returncode is not None else -1
        return ToolResult(
            success=rc == 0,
            output=self._format(stdout_b, stderr_b),
            return_code=rc,
            data={"command": command, "cwd": str(workdir)},
        )

    @staticmethod
    def _kill_group(proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def _format(self, stdout_b: bytes, stderr_b: bytes) -> str:
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        half = self.max_output_chars // 2
        if len(stdout) > half:
            stdout = stdout[: half // 2] + "\n...[truncado]...\n" + stdout[-half // 2:]
        if len(stderr) > half:
            stderr = stderr[: half // 2] + "\n...[truncado]...\n" + stderr[-half // 2:]
        out = f"stdout:\n{stdout.rstrip()}" if stdout.strip() else "stdout: (vacío)"
        if stderr.strip():
            out += f"\nstderr:\n{stderr.rstrip()}"
        return out
