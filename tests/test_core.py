"""Tests básicos de AgentOS (sin LLM: solo parser y herramientas)."""
import asyncio, os
os.environ.setdefault("AGENTOS_DATA_DIR", "/home/ubuntu/.tmp/agentos_pytest")
from core.llm.tool_calling import parse_tool_calls


def test_parse_tool_call():
    calls = parse_tool_calls('hola <tool>{"name": "bash_exec", "args": {"command": "ls"}}</tool>')
    assert calls and calls[0].name == "bash_exec"


def test_tool_registry_system_monitor():
    from core.agent.tools import build_default_registry
    from core.agent.config import get_config
    reg = build_default_registry(get_config())
    assert "system_monitor" in reg.names()
    res = asyncio.run(reg.execute("system_monitor", {"sections": ["memory"]}))
    assert res.success


def test_registry_logging_extra_no_conflict(tmp_path, monkeypatch):
    """Regresión: el log de ejecución de tools no debe chocar con atributos reservados de LogRecord."""
    import asyncio
    import logging
    from core.agent.config import AgentConfig
    from core.agent.tools import build_default_registry

    monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
    logging.getLogger().setLevel(logging.INFO)
    registry = build_default_registry(AgentConfig.from_env())
    result = asyncio.run(registry.execute("bash_exec", {"command": "echo ok"}))
    assert result.success, result
