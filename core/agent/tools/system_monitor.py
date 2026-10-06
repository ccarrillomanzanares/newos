"""Tool ``system_monitor``: CPU, RAM, disco, red, top procesos y uptime vía psutil."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import psutil

from .base import Tool, ToolResult

SECTIONS = ["cpu", "memory", "disk", "network", "processes", "uptime"]

# Sistemas de ficheros virtuales que no interesan en el informe de disco
# Solo se descartan los fs VIRTUALES que no aportan nada al informe. OJO: tmpfs NO
# se descarta: en el sistema live (todo en RAM) la raiz y /data son tmpfs, y
# filtrarlos dejaba la seccion de discos VACIA.
_IGNORED_FS = {"squashfs", "devtmpfs", "proc", "sysfs", "cgroup", "cgroup2", "autofs",
               "nsfs", "tracefs", "debugfs", "fusectl", "configfs", "securityfs", "pstore", "bpf", "mqueue"}


def _gb(n: float) -> float:
    return round(n / 1024 ** 3, 2)


def _mb(n: float) -> float:
    return round(n / 1024 ** 2, 1)


class SystemMonitorTool(Tool):
    name = "system_monitor"
    description = (
        "Get a snapshot of system resources: CPU usage, RAM, disk usage per partition, network interfaces "
        "with traffic counters, top 5 processes by CPU and by memory, and uptime. "
        "Use the 'sections' parameter to request only part of the report."
    )
    parameters = {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "items": {"type": "string", "enum": SECTIONS},
                "description": f"Sections to include (default all): {', '.join(SECTIONS)}",
            },
        },
        "required": [],
    }

    async def execute(self, sections: list[str] | None = None) -> ToolResult:
        wanted = [s for s in (sections or SECTIONS) if s in SECTIONS] or SECTIONS
        data = await asyncio.to_thread(self.collect, wanted)
        return ToolResult(True, output=self.render(data), data=data)

    # ------------------------------------------------------------------ datos
    @staticmethod
    def collect(sections: list[str]) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if "cpu" in sections:
            load = psutil.getloadavg() if hasattr(psutil, "getloadavg") else (0.0, 0.0, 0.0)
            freq = psutil.cpu_freq()
            data["cpu"] = {
                "percent": psutil.cpu_percent(interval=0.5),
                "per_core": psutil.cpu_percent(interval=None, percpu=True),
                "cores_logical": psutil.cpu_count(),
                "cores_physical": psutil.cpu_count(logical=False),
                "load_avg": [round(x, 2) for x in load],
                "freq_mhz": round(freq.current) if freq else None,
            }
        if "memory" in sections:
            vm, sw = psutil.virtual_memory(), psutil.swap_memory()
            data["memory"] = {
                "total_gb": _gb(vm.total), "used_gb": _gb(vm.used), "free_gb": _gb(vm.available),
                "percent": vm.percent, "swap_total_gb": _gb(sw.total), "swap_used_gb": _gb(sw.used),
            }
        if "disk" in sections:
            disks = []
            # all=True: si no, en un sistema live (raiz en RAM) NO devuelve NADA,
            # porque psutil solo lista dispositivos fisicos con all=False.
            for part in psutil.disk_partitions(all=True):
                if part.fstype in _IGNORED_FS or part.mountpoint.startswith(("/snap", "/proc", "/sys")):
                    continue
                try:
                    u = psutil.disk_usage(part.mountpoint)
                except (PermissionError, OSError):
                    continue
                disks.append({"device": part.device, "mountpoint": part.mountpoint, "fstype": part.fstype,
                              "total_gb": _gb(u.total), "used_gb": _gb(u.used), "free_gb": _gb(u.free),
                              "percent": u.percent})
            data["disk"] = disks
        if "network" in sections:
            addrs = psutil.net_if_addrs()
            stats = psutil.net_if_stats()
            counters = psutil.net_io_counters(pernic=True)
            ifaces = []
            for name, addr_list in addrs.items():
                ipv4 = [a.address for a in addr_list if a.family.name == "AF_INET"]
                c = counters.get(name)
                st = stats.get(name)
                ifaces.append({"name": name, "ipv4": ipv4, "up": bool(st and st.isup),
                               "sent_mb": _mb(c.bytes_sent) if c else 0, "recv_mb": _mb(c.bytes_recv) if c else 0})
            data["network"] = ifaces
        if "processes" in sections:
            procs = list(psutil.process_iter(["pid", "name", "memory_percent"]))
            for p in procs:
                try:
                    p.cpu_percent(None)
                except psutil.Error:
                    pass
            time.sleep(0.3)
            rows = []
            for p in procs:
                try:
                    rows.append({"pid": p.pid, "name": p.info["name"], "cpu": p.cpu_percent(None),
                                 "mem": round(p.info["memory_percent"] or 0, 1)})
                except psutil.Error:
                    continue
            data["top_cpu"] = sorted(rows, key=lambda r: r["cpu"], reverse=True)[:5]
            data["top_memory"] = sorted(rows, key=lambda r: r["mem"], reverse=True)[:5]
        if "uptime" in sections:
            seconds = int(time.time() - psutil.boot_time())
            d, rem = divmod(seconds, 86400)
            h, rem = divmod(rem, 3600)
            data["uptime"] = {"seconds": seconds, "human": f"{d}d {h}h {rem // 60}m"}
        return data

    # ---------------------------------------------------------------- render
    @staticmethod
    def render(data: dict[str, Any]) -> str:
        out: list[str] = []
        if "cpu" in data:
            c = data["cpu"]
            out.append(f"CPU: {c['percent']}% ({c['cores_logical']} hilos / {c['cores_physical']} núcleos), "
                       f"load avg {c['load_avg']}, {c['freq_mhz']} MHz")
        if "memory" in data:
            m = data["memory"]
            out.append(f"RAM: {m['used_gb']}/{m['total_gb']} GB usados ({m['percent']}%), {m['free_gb']} GB "
                       f"disponibles; swap {m['swap_used_gb']}/{m['swap_total_gb']} GB")
        if "disk" in data:
            out.append("Discos:")
            for d in data["disk"]:
                ram = " (RAM)" if d["fstype"] in ("tmpfs", "rootfs", "ramfs") else ""
                out.append(f"  {d['mountpoint']:<20} {d['device']:<18} {d['used_gb']}/{d['total_gb']} GB "
                           f"({d['percent']}%), libres {d['free_gb']} GB [{d['fstype']}]{ram}")
        if "network" in data:
            out.append("Red:")
            for n in data["network"]:
                out.append(f"  {n['name']:<12} {'UP' if n['up'] else 'DOWN':<5} {','.join(n['ipv4']) or '-':<18} "
                           f"tx {n['sent_mb']} MB / rx {n['recv_mb']} MB")
        if "top_cpu" in data:
            out.append("Top 5 CPU: " + ", ".join(f"{p['name']}({p['pid']}) {p['cpu']}%" for p in data["top_cpu"]))
            out.append("Top 5 RAM: " + ", ".join(f"{p['name']}({p['pid']}) {p['mem']}%" for p in data["top_memory"]))
        if "uptime" in data:
            out.append(f"Uptime: {data['uptime']['human']}")
        return "\n".join(out)
