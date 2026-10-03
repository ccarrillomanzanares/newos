"""Servidor UNIX socket para la comunicación UI <-> AgentD.

Protocolo: JSON por líneas (NDJSON), un objeto por línea.

Cliente -> servidor:
    {"type": "message", "text": "...", "session_id": "opcional"}
    {"type": "confirm", "id": "<tool_call_id>", "approved": true}
    {"type": "command", "name": "ping|status|history|sessions|clear|session", "session_id": "..."}

Servidor -> cliente:
    {"type": "hello", "session_id": "...", "llm": "...", "version": "..."}
    {"type": "token", "text": "..."}            (streaming)
    {"type": "tool_call", "id", "name", "args"}
    {"type": "confirm_request", "id", "name", "args", "reason"}
    {"type": "tool_result", "id", "name", "success", "output"}
    {"type": "final", "text": "...", "session_id": "..."}
    {"type": "error", "message": "..."}
    + respuestas a comandos ("pong", "status", "history", "sessions", "session")

Ejecución como demonio:  python -m core.ipc.socket_server
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ..agent.agent import Agent
from ..agent.config import AgentConfig, get_config, setup_logging
from ..llm.tool_calling import ToolCall

logger = logging.getLogger("agentos.ipc")

VERSION = "0.1.0"
STREAM_LIMIT = 4 * 1024 * 1024  # tamaño máximo de una línea JSON


def encode(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode("utf-8")


class ClientConnection:
    """Estado de una conexión de cliente (sesión, confirmaciones pendientes)."""

    def __init__(self, agent: Agent, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 confirm_timeout: float = 300.0) -> None:
        self.agent = agent
        self.reader = reader
        self.writer = writer
        self.session_id = agent.new_session()
        self.confirm_timeout = confirm_timeout
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()

    async def send(self, obj: dict[str, Any]) -> None:
        async with self._write_lock:
            self.writer.write(encode(obj))
            await self.writer.drain()

    async def confirm(self, call: ToolCall, reason: str) -> bool:
        """Callback de confirmación: espera la respuesta del cliente."""
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[call.id] = future
        try:
            return await asyncio.wait_for(future, timeout=self.confirm_timeout)
        except asyncio.TimeoutError:
            logger.warning("Confirmación expirada", extra={"tool": call.name})
            return False
        finally:
            self._pending.pop(call.id, None)

    async def process_message(self, text: str) -> None:
        try:
            async for event in self.agent.stream(text, self.session_id, confirm=self.confirm):
                payload = event.to_dict()
                if event.type == "final":
                    payload["session_id"] = self.session_id
                await self.send(payload)
        except (ConnectionError, BrokenPipeError):
            logger.info("Cliente desconectado durante el procesamiento")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error procesando mensaje")
            with contextlib.suppress(Exception):
                await self.send({"type": "error", "message": str(exc)})
                await self.send({"type": "final", "text": f"Error interno: {exc}", "session_id": self.session_id})

    async def handle_command(self, msg: dict[str, Any]) -> None:
        name = msg.get("name")
        if name == "ping":
            await self.send({"type": "pong"})
        elif name == "status":
            await self.send({"type": "status", "llm": self.agent.llm.name if self.agent.llm else None,
                             "tools": self.agent.registry.names(), "session_id": self.session_id,
                             "mode": self.agent.config.mode, "busy": self.busy})
        elif name == "history":
            sid = msg.get("session_id") or self.session_id
            rows = await self.agent.episodic.get_session_history(sid, limit=int(msg.get("limit", 50)))
            await self.send({"type": "history", "session_id": sid, "messages": rows})
        elif name == "sessions":
            await self.send({"type": "sessions", "sessions": await self.agent.episodic.list_sessions()})
        elif name == "clear":
            self.agent.forget_session(self.session_id)
            self.session_id = self.agent.new_session()
            await self.send({"type": "session", "session_id": self.session_id, "cleared": True})
        elif name == "session":  # reanudar una sesión existente
            sid = str(msg.get("session_id") or "")
            if sid:
                await self.agent.get_working_memory(sid)
                self.session_id = sid
            await self.send({"type": "session", "session_id": self.session_id})
        else:
            await self.send({"type": "error", "message": f"Comando desconocido: {name}"})

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run(self) -> None:
        await self.send({"type": "hello", "session_id": self.session_id, "version": VERSION,
                         "llm": self.agent.llm.name if self.agent.llm else None})
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                    if not isinstance(msg, dict):
                        raise ValueError("se esperaba un objeto")
                except (json.JSONDecodeError, ValueError) as exc:
                    await self.send({"type": "error", "message": f"JSON inválido: {exc}"})
                    continue
                mtype = msg.get("type")
                if mtype == "message":
                    if self.busy:
                        await self.send({"type": "error", "message": "AgentD está ocupado con otra petición"})
                        continue
                    if msg.get("session_id") and msg["session_id"] != self.session_id:
                        await self.agent.get_working_memory(msg["session_id"])
                        self.session_id = msg["session_id"]
                    self._task = asyncio.create_task(self.process_message(str(msg.get("text", ""))))
                elif mtype == "confirm":
                    future = self._pending.get(str(msg.get("id")))
                    if future and not future.done():
                        future.set_result(bool(msg.get("approved")))
                elif mtype == "command":
                    await self.handle_command(msg)
                else:
                    await self.send({"type": "error", "message": f"Tipo de mensaje desconocido: {mtype}"})
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_result(False)
            if self._task and not self._task.done():
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._task
            with contextlib.suppress(Exception):
                self.writer.close()
                await self.writer.wait_closed()


class AgentServer:
    """Servidor asyncio sobre socket UNIX."""

    def __init__(self, agent: Agent | None, socket_path: str | Path, error: str | None = None) -> None:
        self.agent = agent
        self.socket_path = Path(socket_path)
        # Si el LLM no estaba disponible al arrancar, se guarda el motivo para
        # poder responder a la interfaz en vez de dejarla sin explicación.
        self.error = error
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if self.agent is not None:
            await self.agent.start()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()  # socket huérfano de una ejecución anterior
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path), limit=STREAM_LIMIT)
        os.chmod(self.socket_path, 0o660)
        logger.info("Escuchando en socket UNIX", extra={"socket": str(self.socket_path)})
        if self.error:
            logger.warning("AgentD escucha SIN agente: los mensajes del usuario serán rechazados con el motivo")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        logger.info("Cliente conectado")
        if self.agent is None:
            # Sin backend LLM el socket sigue vivo, pero no se puede atender.
            # Se responde con el motivo para que la interfaz pueda mostrarlo en
            # pantalla en vez de quedarse muda.
            with contextlib.suppress(Exception):
                writer.write(encode({"type": "error", "message": self.error or "La IA no está disponible (sin backend LLM)."}))
                await writer.drain()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        await ClientConnection(self.agent, reader, writer).run()
        logger.info("Cliente desconectado")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        if self.agent is not None:
            await self.agent.close()


# ---------------------------------------------------------------------------
# Cliente (usado por la TUI y la interfaz de voz)
# ---------------------------------------------------------------------------

class AgentClient:
    """Cliente asíncrono del socket de AgentD."""

    def __init__(self, socket_path: str | Path) -> None:
        self.socket_path = Path(socket_path)
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.session_id: str | None = None
        self.llm: str | None = None

    async def connect(self) -> dict[str, Any]:
        self.reader, self.writer = await asyncio.open_unix_connection(str(self.socket_path), limit=STREAM_LIMIT)
        hello = await self.receive()
        self.session_id = hello.get("session_id")
        self.llm = hello.get("llm")
        return hello

    async def send(self, obj: dict[str, Any]) -> None:
        assert self.writer is not None, "No conectado"
        self.writer.write(encode(obj))
        await self.writer.drain()

    async def receive(self) -> dict[str, Any]:
        assert self.reader is not None, "No conectado"
        line = await self.reader.readline()
        if not line:
            raise ConnectionError("AgentD cerró la conexión")
        return json.loads(line)

    async def ask(self, text: str) -> AsyncIterator[dict[str, Any]]:
        """Envía un mensaje y produce eventos hasta el evento 'final'."""
        await self.send({"type": "message", "text": text})
        while True:
            event = await self.receive()
            yield event
            if event.get("type") == "final":
                if event.get("session_id"):
                    self.session_id = event["session_id"]
                return

    async def confirm(self, call_id: str, approved: bool) -> None:
        await self.send({"type": "confirm", "id": call_id, "approved": approved})

    async def command(self, name: str, **kwargs: Any) -> dict[str, Any]:
        await self.send({"type": "command", "name": name, **kwargs})
        response = await self.receive()
        if response.get("type") == "session":
            self.session_id = response.get("session_id")
        return response

    async def close(self) -> None:
        if self.writer:
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()


# ---------------------------------------------------------------------------
# Punto de entrada del demonio
# ---------------------------------------------------------------------------

async def serve(config: AgentConfig, pidfile: Path | None = None) -> None:
    # AgentD no se cae si el LLM no está disponible: mantiene el socket abierto y
    # deja que la interfaz arranque y lo diga en pantalla. Antes, sin backend, el
    # proceso moría aquí y la GUI se quedaba sin arrancar — el SO entero se
    # quedaba en negro por un fallo del modelo.
    agent: Agent | None = None
    agent_error: str | None = None
    try:
        agent = Agent(config)
    except Exception as exc:  # noqa: BLE001
        agent_error = str(exc)
        logger.error("AgentD arranca SIN backend LLM: %s", exc)
        logger.error("La interfaz arrancará igualmente y reportará el problema al usuario.")

    server = AgentServer(agent, config.socket_path, error=agent_error)
    await server.start()
    if pidfile:
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(str(os.getpid()))

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
    logger.info("AgentD en ejecución", extra={"pid": os.getpid()})
    await stop_event.wait()
    logger.info("Deteniendo AgentD")
    await server.stop()
    if pidfile:
        with contextlib.suppress(FileNotFoundError):
            pidfile.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AgentD — demonio agente de AgentOS")
    parser.add_argument("--socket", help="Ruta del socket UNIX")
    parser.add_argument("--backend", choices=["auto", "llama_cpp", "openai"], help="Backend LLM")
    parser.add_argument("--model", help="Ruta al modelo GGUF")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING...")
    parser.add_argument("--pidfile", help="Escribir el PID en este fichero")
    args = parser.parse_args(argv)

    config = get_config()
    if args.socket:
        config.socket_path = Path(args.socket).expanduser()
    if args.backend:
        config.llm_backend = args.backend
    if args.model:
        config.model_path = Path(args.model).expanduser()
    setup_logging(config, args.log_level)
    try:
        asyncio.run(serve(config, Path(args.pidfile) if args.pidfile else None))
    except Exception as exc:  # noqa: BLE001
        logger.error("AgentD terminó con error: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
