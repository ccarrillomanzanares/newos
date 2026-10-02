"""Tool ``package_manager``: instalar, eliminar y buscar paquetes del sistema.

Detecta automáticamente el gestor disponible (apt, dnf, pacman, apk, opkg).
Si el agente no corre como root, antepone ``sudo -n`` (sin contraseña).
"""

from __future__ import annotations

import os
import re
import shutil
from typing import Any

from .base import RiskLevel, Tool, ToolResult
from .bash_exec import BashExecTool

_PKG_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_:@=~/-]*$")

# Plantillas de comandos por gestor. {pkgs} = lista de paquetes, {q} = consulta.
MANAGERS: dict[str, dict[str, str]] = {
    "apt": {
        "install": "apt-get install -y {pkgs}",
        "remove": "apt-get remove -y {pkgs}",
        "search": "apt-cache search --names-only {q} | head -n 40",
        "update": "apt-get update",
        "upgrade": "apt-get upgrade -y",
        "list_installed": "dpkg-query -W -f='${{Package}} ${{Version}}\\n' | grep -i -- {q} | head -n 100",
        "info": "apt-cache show {pkgs} | head -n 40",
    },
    "dnf": {
        "install": "dnf install -y {pkgs}", "remove": "dnf remove -y {pkgs}",
        "search": "dnf search -q {q} | head -n 40", "update": "dnf makecache",
        "upgrade": "dnf upgrade -y", "list_installed": "rpm -qa | grep -i -- {q} | head -n 100",
        "info": "dnf info {pkgs}",
    },
    "pacman": {
        "install": "pacman -S --noconfirm {pkgs}", "remove": "pacman -R --noconfirm {pkgs}",
        "search": "pacman -Ss {q} | head -n 40", "update": "pacman -Sy",
        "upgrade": "pacman -Syu --noconfirm", "list_installed": "pacman -Q | grep -i -- {q} | head -n 100",
        "info": "pacman -Si {pkgs}",
    },
    "apk": {
        "install": "apk add {pkgs}", "remove": "apk del {pkgs}", "search": "apk search {q} | head -n 40",
        "update": "apk update", "upgrade": "apk upgrade",
        "list_installed": "apk info | grep -i -- {q} | head -n 100", "info": "apk info -a {pkgs}",
    },
    "opkg": {
        "install": "opkg install {pkgs}", "remove": "opkg remove {pkgs}", "search": "opkg find '*{q}*'",
        "update": "opkg update", "upgrade": "opkg upgrade",
        "list_installed": "opkg list-installed | grep -i -- {q}", "info": "opkg info {pkgs}",
    },
}
_BINARIES = {"apt": "apt-get", "dnf": "dnf", "pacman": "pacman", "apk": "apk", "opkg": "opkg"}
_READ_ONLY = {"search", "list_installed", "info"}


def detect_manager() -> str | None:
    """Devuelve el primer gestor de paquetes disponible en el sistema."""
    for name, binary in _BINARIES.items():
        if shutil.which(binary):
            return name
    return None


class PackageManagerTool(Tool):
    name = "package_manager"
    description = (
        "Manage system packages with the native package manager (apt/dnf/pacman/apk/opkg, auto-detected). "
        "Actions: install, remove, search, update (refresh index), upgrade (upgrade all), "
        "list_installed (filter by query), info."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["install", "remove", "search", "update", "upgrade", "list_installed", "info"]},
            "packages": {"type": "array", "items": {"type": "string"},
                         "description": "Package names for install/remove/info."},
            "query": {"type": "string", "description": "Search term for search/list_installed."},
        },
        "required": ["action"],
    }
    risk = RiskLevel.ELEVATED

    def __init__(self, timeout: int = 900, manager: str | None = None) -> None:
        self.timeout = timeout
        self.manager = manager or detect_manager()
        self._bash = BashExecTool(default_timeout=timeout, default_cwd="/")

    def needs_confirmation(self, args: dict[str, Any]) -> str | None:
        action = args.get("action")
        if action == "remove":
            return f"Se desinstalarán los paquetes: {', '.join(args.get('packages') or [])}"
        if action == "upgrade":
            return "Se actualizarán todos los paquetes del sistema"
        return None

    def build_command(self, action: str, packages: list[str] | None, query: str | None) -> str:
        """Construye el comando de shell (lanza ValueError si los argumentos no son válidos)."""
        if not self.manager:
            raise ValueError("No se ha detectado ningún gestor de paquetes soportado")
        templates = MANAGERS[self.manager]
        if action not in templates:
            raise ValueError(f"Acción no soportada: {action}")
        pkgs = packages or []
        for pkg in pkgs:
            if not _PKG_NAME.match(pkg):
                raise ValueError(f"Nombre de paquete inválido: {pkg!r}")
        if action in {"install", "remove", "info"} and not pkgs:
            raise ValueError(f"'packages' es obligatorio para {action}")
        if action in {"search"} and not query:
            raise ValueError("'query' es obligatorio para search")
        q = query or ""
        if q and not _PKG_NAME.match(q):
            raise ValueError(f"Consulta inválida: {q!r}")
        cmd = templates[action].format(pkgs=" ".join(pkgs), q=q or "''")
        if action not in _READ_ONLY and os.geteuid() != 0:
            cmd = f"sudo -n sh -c {self._quote(cmd)}"
        return cmd

    @staticmethod
    def _quote(cmd: str) -> str:
        return "'" + cmd.replace("'", "'\"'\"'") + "'"

    async def execute(self, action: str, packages: list[str] | None = None, query: str | None = None) -> ToolResult:
        try:
            command = self.build_command(action, packages, query)
        except ValueError as exc:
            return ToolResult(False, error=str(exc))
        result = await self._bash.execute(command=command, timeout=self.timeout)
        result.data = {**(result.data or {}), "manager": self.manager, "action": action}
        if not result.success and "sudo" in command and "password" in (result.output or "").lower():
            result.error = "Se requieren privilegios de root (sudo pidió contraseña). Ejecuta AgentD como root."
        return result
