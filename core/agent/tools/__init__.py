"""Tools disponibles para AgentD."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .app_launcher import AppLauncherTool
from .base import RiskLevel, Tool, ToolRegistry, ToolResult
from .bash_exec import BashExecTool
from .file_ops import FileOpsTool
from .package_manager import PackageManagerTool
from .process_manager import ProcessManagerTool
from .system_monitor import SystemMonitorTool

if TYPE_CHECKING:
    from ..config import AgentConfig

__all__ = [
    "Tool", "ToolResult", "ToolRegistry", "RiskLevel", "BashExecTool", "FileOpsTool", "ProcessManagerTool",
    "SystemMonitorTool", "PackageManagerTool", "AppLauncherTool", "build_default_registry",
]


def build_default_registry(config: "AgentConfig | None" = None) -> ToolRegistry:
    """Crea un registro con todas las tools v0.1 configuradas."""
    from ..config import get_config

    cfg = config or get_config()
    registry = ToolRegistry()
    registry.register(BashExecTool(default_timeout=cfg.bash_timeout, default_cwd=cfg.bash_cwd))
    registry.register(FileOpsTool())
    registry.register(ProcessManagerTool())
    registry.register(SystemMonitorTool())
    registry.register(PackageManagerTool(timeout=cfg.package_timeout))
    registry.register(AppLauncherTool())
    return registry
