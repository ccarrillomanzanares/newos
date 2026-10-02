"""Tool ``app_launcher``: lanza y cierra aplicaciones gráficas en Wayland (Sway) o X11.

En AgentOS el compositor es Sway: el agente delega en ``swaymsg exec``.
Fuera de Sway se lanza el proceso directamente heredando DISPLAY/WAYLAND_DISPLAY.
"""

from __future__ import annotations

import asyncio
import configparser
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .base import RiskLevel, Tool, ToolResult

DESKTOP_DIRS = [Path("/usr/share/applications"), Path("/usr/local/share/applications"),
                Path.home() / ".local/share/applications", Path("/var/lib/flatpak/exports/share/applications")]


def detect_display() -> str:
    """Devuelve 'sway', 'wayland', 'x11' o 'none'."""
    if os.environ.get("SWAYSOCK") and shutil.which("swaymsg"):
        return "sway"
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "none"


def list_desktop_apps() -> dict[str, dict[str, str]]:
    """Lee los ficheros .desktop y devuelve {id: {name, exec}}."""
    apps: dict[str, dict[str, str]] = {}
    for directory in DESKTOP_DIRS:
        if not directory.is_dir():
            continue
        for desktop in directory.glob("*.desktop"):
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(desktop, encoding="utf-8")
                entry = parser["Desktop Entry"]
            except (configparser.Error, KeyError, UnicodeDecodeError):
                continue
            if entry.get("NoDisplay", "false").lower() == "true" or entry.get("Type") != "Application":
                continue
            # Quitar códigos de campo (%U, %f...) de la línea Exec
            exec_line = " ".join(t for t in entry.get("Exec", "").split() if not t.startswith("%"))
            if exec_line:
                apps[desktop.stem] = {"name": entry.get("Name", desktop.stem), "exec": exec_line}
    return apps


class AppLauncherTool(Tool):
    name = "app_launcher"
    description = (
        "Launch or close graphical applications (Wayland/Sway or X11). Actions: launch (open an app by name, "
        "e.g. 'firefox', or by full command), list (installed desktop applications), "
        "close (close an app window/process by name)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["launch", "list", "close"]},
            "app": {"type": "string", "description": "Application name, desktop id or command line."},
            "args": {"type": "string", "description": "Extra arguments (e.g. a URL or file path)."},
            "query": {"type": "string", "description": "Filter for list."},
        },
        "required": ["action"],
    }
    risk = RiskLevel.SAFE

    async def execute(self, action: str, app: str | None = None, args: str | None = None,
                      query: str | None = None) -> ToolResult:
        if action == "list":
            apps = await asyncio.to_thread(list_desktop_apps)
            items = sorted(apps.items())
            if query:
                q = query.lower()
                items = [(k, v) for k, v in items if q in k.lower() or q in v["name"].lower()]
            lines = [f"{k}: {v['name']} ({v['exec']})" for k, v in items[:100]]
            return ToolResult(True, output="\n".join(lines) or "No se encontraron aplicaciones",
                              data={"count": len(items), "display": detect_display()})
        if not app:
            return ToolResult(False, error="'app' es obligatorio")
        if action == "launch":
            return await asyncio.to_thread(self._launch, app, args)
        if action == "close":
            return await asyncio.to_thread(self._close, app)
        return ToolResult(False, error=f"Acción no soportada: {action}")

    @staticmethod
    def _resolve(app: str) -> str:
        """Convierte un nombre/desktop-id en la línea de comando a ejecutar."""
        apps = list_desktop_apps()
        if app in apps:
            return apps[app]["exec"]
        lowered = app.lower()
        for key, info in apps.items():
            if lowered in (key.lower(), info["name"].lower()):
                return info["exec"]
        return app

    def _launch(self, app: str, args: str | None) -> ToolResult:
        display = detect_display()
        if display == "none":
            return ToolResult(False, error="No hay sesión gráfica (ni WAYLAND_DISPLAY ni DISPLAY definidos)")
        command = self._resolve(app)
        if args:
            command = f"{command} {args}"
        binary = shlex.split(command)[0]
        if not shutil.which(binary):
            return ToolResult(False, error=f"La aplicación '{binary}' no está instalada")
        if display == "sway":
            res = subprocess.run(["swaymsg", "exec", command], capture_output=True, text=True, timeout=10)
            ok = res.returncode == 0
            return ToolResult(ok, output=f"Lanzado vía Sway: {command}" if ok else res.stdout,
                              error=None if ok else res.stderr, data={"display": display})
        proc = subprocess.Popen(shlex.split(command), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        return ToolResult(True, output=f"Lanzado '{command}' (PID {proc.pid}) en {display}",
                          data={"pid": proc.pid, "display": display})

    @staticmethod
    def _close(app: str) -> ToolResult:
        if detect_display() == "sway":
            res = subprocess.run(["swaymsg", f'[app_id="(?i){app}"] kill'], capture_output=True, text=True,
                                 timeout=10)
            if res.returncode == 0:
                return ToolResult(True, output=f"Ventana '{app}' cerrada vía Sway")
        res = subprocess.run(["pkill", "-f", app], capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            return ToolResult(True, output=f"Procesos '{app}' terminados")
        return ToolResult(False, error=f"No se encontró ninguna aplicación '{app}' en ejecución")

    def needs_confirmation(self, args: dict[str, Any]) -> str | None:
        return None
