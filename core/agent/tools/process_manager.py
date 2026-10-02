"""Tool ``process_manager``: listar, inspeccionar, matar y lanzar procesos."""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import subprocess
import time
from typing import Any

import psutil

from .base import RiskLevel, Tool, ToolResult

SIGNALS = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL, "INT": signal.SIGINT, "HUP": signal.SIGHUP,
           "STOP": signal.SIGSTOP, "CONT": signal.SIGCONT}


class ProcessManagerTool(Tool):
    name = "process_manager"
    description = (
        "Manage processes. Actions: list (list processes, optionally filtered by name and sorted by cpu/memory), "
        "info (details of a PID), kill (send a signal to a PID or to all processes matching a name), "
        "launch (start a background command and return its PID)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "info", "kill", "launch"]},
            "pid": {"type": "integer", "description": "Process ID for info/kill."},
            "name": {"type": "string", "description": "Process name filter for list/kill."},
            "signal": {"type": "string", "enum": list(SIGNALS), "default": "TERM"},
            "command": {"type": "string", "description": "Command line to launch."},
            "sort_by": {"type": "string", "enum": ["cpu", "memory", "pid"], "default": "cpu"},
            "limit": {"type": "integer", "default": 20},
        },
        "required": ["action"],
    }
    risk = RiskLevel.ELEVATED

    def needs_confirmation(self, args: dict[str, Any]) -> str | None:
        if args.get("action") == "kill":
            target = args.get("pid") or args.get("name")
            return f"Se enviará la señal {args.get('signal', 'TERM')} a '{target}'"
        return None

    async def execute(self, action: str, pid: int | None = None, name: str | None = None, signal: str = "TERM",
                      command: str | None = None, sort_by: str = "cpu", limit: int = 20) -> ToolResult:
        if action == "list":
            return await asyncio.to_thread(self._list, name, sort_by, limit)
        if action == "info":
            if pid is None:
                return ToolResult(False, error="'pid' es obligatorio para info")
            return await asyncio.to_thread(self._info, pid)
        if action == "kill":
            return await asyncio.to_thread(self._kill, pid, name, signal)
        if action == "launch":
            if not command:
                return ToolResult(False, error="'command' es obligatorio para launch")
            return self._launch(command)
        return ToolResult(False, error=f"Acción no soportada: {action}")

    @staticmethod
    def _snapshot() -> list[dict[str, Any]]:
        procs = list(psutil.process_iter(["pid", "name", "username", "memory_percent", "status", "cmdline"]))
        # Primera medición de CPU (no bloqueante) y segunda tras un intervalo corto
        for p in procs:
            try:
                p.cpu_percent(None)
            except psutil.Error:
                pass
        time.sleep(0.3)
        rows = []
        for p in procs:
            try:
                info = p.info
                info["cpu_percent"] = p.cpu_percent(None)
                rows.append(info)
            except psutil.Error:
                continue
        return rows

    def _list(self, name: str | None, sort_by: str, limit: int) -> ToolResult:
        rows = self._snapshot()
        if name:
            needle = name.lower()
            rows = [r for r in rows if needle in (r.get("name") or "").lower()
                    or needle in " ".join(r.get("cmdline") or []).lower()]
        key = {"cpu": "cpu_percent", "memory": "memory_percent", "pid": "pid"}.get(sort_by, "cpu_percent")
        rows.sort(key=lambda r: r.get(key) or 0, reverse=sort_by != "pid")
        rows = rows[: max(1, limit)]
        lines = [f"{'PID':>7} {'CPU%':>6} {'MEM%':>6} {'USER':<12} {'STATUS':<9} NAME"]
        for r in rows:
            lines.append(f"{r['pid']:>7} {r['cpu_percent']:>6.1f} {(r.get('memory_percent') or 0):>6.1f} "
                         f"{(r.get('username') or '?')[:12]:<12} {(r.get('status') or '')[:9]:<9} {r.get('name')}")
        data = [{k: r.get(k) for k in ("pid", "name", "cpu_percent", "memory_percent", "username")} for r in rows]
        return ToolResult(True, output="\n".join(lines), data={"processes": data})

    @staticmethod
    def _info(pid: int) -> ToolResult:
        try:
            p = psutil.Process(pid)
            with p.oneshot():
                info = {
                    "pid": p.pid,
                    "name": p.name(),
                    "status": p.status(),
                    "user": p.username(),
                    "cmdline": " ".join(p.cmdline()),
                    "ppid": p.ppid(),
                    "cpu_percent": p.cpu_percent(interval=0.2),
                    "memory_rss_mb": round(p.memory_info().rss / 1024 / 1024, 1),
                    "threads": p.num_threads(),
                    "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.create_time())),
                }
        except psutil.NoSuchProcess:
            return ToolResult(False, error=f"No existe el proceso {pid}")
        except psutil.AccessDenied:
            return ToolResult(False, error=f"Acceso denegado al proceso {pid}")
        return ToolResult(True, output="\n".join(f"{k}: {v}" for k, v in info.items()), data=info)

    @staticmethod
    def _kill(pid: int | None, name: str | None, sig_name: str) -> ToolResult:
        sig = SIGNALS.get(sig_name.upper(), signal.SIGTERM)
        targets: list[psutil.Process] = []
        if pid is not None:
            if pid in (0, 1, os.getpid()):
                return ToolResult(False, error=f"Rechazado: no se puede matar el PID {pid}")
            try:
                targets = [psutil.Process(pid)]
            except psutil.NoSuchProcess:
                return ToolResult(False, error=f"No existe el proceso {pid}")
        elif name:
            targets = [p for p in psutil.process_iter(["name"])
                       if (p.info.get("name") or "") == name and p.pid not in (1, os.getpid())]
            if not targets:
                return ToolResult(False, error=f"No hay procesos llamados '{name}'")
        else:
            return ToolResult(False, error="Indica 'pid' o 'name'")

        done, failed = [], []
        for p in targets:
            try:
                p.send_signal(sig)
                done.append(p.pid)
            except psutil.Error as exc:
                failed.append(f"{p.pid}: {exc}")
        out = f"Señal {sig_name} enviada a: {done}" + (f"\nFallos: {failed}" if failed else "")
        return ToolResult(bool(done), output=out, data={"killed": done, "failed": failed})

    @staticmethod
    def _launch(command: str) -> ToolResult:
        try:
            proc = subprocess.Popen(shlex.split(command), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        except (FileNotFoundError, PermissionError, ValueError) as exc:
            return ToolResult(False, error=f"No se pudo lanzar '{command}': {exc}")
        return ToolResult(True, output=f"Lanzado '{command}' con PID {proc.pid}", data={"pid": proc.pid})
