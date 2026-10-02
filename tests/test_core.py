"""Tests básicos de AgentOS (backend mock, sin modelo)."""
import asyncio, os
os.environ.setdefault("AGENTOS_DATA_DIR", "/home/ubuntu/.tmp/agentos_pytest")
os.environ["AGENTOS_LLM_BACKEND"] = "mock"
from core.llm.tool_calling import parse_tool_calls


def test_parse_tool_call():
    calls = parse_tool_calls('hola <tool>{"name": "bash_exec", "args": {"command": "ls"}}</tool>')
    assert calls and calls[0].name == "bash_exec"


def test_agent_mock_loop():
    from core.agent.tools import build_default_registry
    from core.agent.config import get_config
    reg = build_default_registry(get_config())
    assert "system_monitor" in reg.names()
    res = asyncio.run(reg.execute("system_monitor", {"sections": ["memory"]}))
    assert res.success
