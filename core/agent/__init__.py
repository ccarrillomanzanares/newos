"""AgentD — demonio agente de AgentOS.

Las importaciones son perezosas para que ``python -m core.agent.agent`` y
los submódulos se puedan cargar sin dependencias circulares.
"""

from __future__ import annotations

from typing import Any

__all__ = ["Agent", "AgentEvent", "AgentConfig", "get_config"]


def __getattr__(name: str) -> Any:
    if name in ("Agent", "AgentEvent"):
        from . import agent

        return getattr(agent, name)
    if name in ("AgentConfig", "get_config"):
        from . import config

        return getattr(config, name)
    raise AttributeError(name)
