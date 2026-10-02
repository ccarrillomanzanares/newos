"""Interfaz conversacional de texto para AgentOS.

Modos:
* socket (por defecto): se conecta a AgentD por el socket UNIX.
* directo (``--direct``): ejecuta el agente en el mismo proceso (modo dev).

Uso:  python -m ui.tui.chat [--direct] [--socket RUTA] [--backend mock]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

from colorama import Fore, Style
from colorama import init as colorama_init

from core.agent.config import get_config, setup_logging

PROMPT = "AgentOS > "
HELP = """Comandos disponibles:
  /help              Muestra esta ayuda
  /clear             Empieza una conversación nueva
  /history [N]       Muestra los últimos N mensajes de la sesión (por defecto 20)
  /sessions          Lista las sesiones guardadas
  /resume <id>       Reanuda una sesión anterior
  /status            Estado de AgentD (modelo, tools)
  /exit              Salir (también Ctrl+D)"""


class Backend(Protocol):
    """Interfaz común de los modos socket y directo."""

    session_id: str | None
    llm_name: str | None

    async def start(self) -> None: ...
    def ask(self, text: str) -> AsyncIterator[dict[str, Any]]: ...
    async def confirm(self, call_id: str, approved: bool) -> None: ...
    async def history(self, limit: int) -> list[dict[str, Any]]: ...
    async def sessions(self) -> list[dict[str, Any]]: ...
    async def clear(self) -> None: ...
    async def resume(self, session_id: str) -> None: ...
    async def status(self) -> dict[str, Any]: ...
    async def close(self) -> None: ...


class SocketBackend:
    """Habla con AgentD a través del socket UNIX."""

    def __init__(self, socket_path: Path) -> None:
        from core.ipc.socket_server import AgentClient

        self.client = AgentClient(socket_path)
        self.session_id: str | None = None
        self.llm_name: str | None = None

    async def start(self) -> None:
        hello = await self.client.connect()
        self.session_id, self.llm_name = hello.get("session_id"), hello.get("llm")

    async def ask(self, text: str) -> AsyncIterator[dict[str, Any]]:
        async for event in self.client.ask(text):
            yield event

    async def confirm(self, call_id: str, approved: bool) -> None:
        await self.client.confirm(call_id, approved)

    async def history(self, limit: int) -> list[dict[str, Any]]:
        return (await self.client.command("history", limit=limit)).get("messages", [])

    async def sessions(self) -> list[dict[str, Any]]:
        return (await self.client.command("sessions")).get("sessions", [])

    async def clear(self) -> None:
        self.session_id = (await self.client.command("clear")).get("session_id")

    async def resume(self, session_id: str) -> None:
        self.session_id = (await self.client.command("session", session_id=session_id)).get("session_id")

    async def status(self) -> dict[str, Any]:
        return await self.client.command("status")

    async def close(self) -> None:
        await self.client.close()


class DirectBackend:
    """Ejecuta el agente en proceso. La confirmación se hace con una cola de respuestas."""

    def __init__(self) -> None:
        from core.agent.agent import Agent

        self.agent = Agent(get_config())
        self.session_id: str | None = None
        self.llm_name: str | None = None
        self._answers: dict[str, asyncio.Future[bool]] = {}

    async def start(self) -> None:
        await self.agent.start()
        self.session_id = self.agent.new_session()
        self.llm_name = self.agent.llm.name if self.agent.llm else None

    async def _confirm_cb(self, call: Any, reason: str) -> bool:
        future = self._answers.setdefault(call.id, asyncio.get_running_loop().create_future())
        try:
            return await future
        finally:
            self._answers.pop(call.id, None)

    async def ask(self, text: str) -> AsyncIterator[dict[str, Any]]:
        assert self.session_id
        async for event in self.agent.stream(text, self.session_id, confirm=self._confirm_cb):
            if event.type == "confirm_request":
                self._answers.setdefault(event.data["id"], asyncio.get_running_loop().create_future())
            yield event.to_dict()

    async def confirm(self, call_id: str, approved: bool) -> None:
        future = self._answers.get(call_id)
        if future and not future.done():
            future.set_result(approved)

    async def history(self, limit: int) -> list[dict[str, Any]]:
        return await self.agent.episodic.get_session_history(self.session_id or "", limit=limit)

    async def sessions(self) -> list[dict[str, Any]]:
        return await self.agent.episodic.list_sessions()

    async def clear(self) -> None:
        if self.session_id:
            self.agent.forget_session(self.session_id)
        self.session_id = self.agent.new_session()

    async def resume(self, session_id: str) -> None:
        await self.agent.get_working_memory(session_id)
        self.session_id = session_id

    async def status(self) -> dict[str, Any]:
        return {"llm": self.llm_name, "tools": self.agent.registry.names(), "mode": self.agent.config.mode,
                "session_id": self.session_id}

    async def close(self) -> None:
        await self.agent.close()


class ChatUI:
    """Bucle de lectura/escritura en terminal."""

    def __init__(self, backend: Backend, typewriter_delay: float = 0.0, show_tools: bool = True) -> None:
        self.backend = backend
        self.delay = typewriter_delay
        self.show_tools = show_tools
        self.loop = asyncio.new_event_loop()

    # ------------------------------------------------------------- salida
    def write(self, text: str, color: str = "") -> None:
        """Escribe carácter a carácter (efecto máquina de escribir opcional)."""
        if color:
            sys.stdout.write(color)
        if self.delay > 0:
            for ch in text:
                sys.stdout.write(ch)
                sys.stdout.flush()
                time.sleep(self.delay)
        else:
            sys.stdout.write(text)
        if color:
            sys.stdout.write(Style.RESET_ALL)
        sys.stdout.flush()

    @staticmethod
    def info(text: str, color: str = Fore.CYAN) -> None:
        print(f"{color}{text}{Style.RESET_ALL}")

    def run_async(self, coro: Any) -> Any:
        return self.loop.run_until_complete(coro)

    # ------------------------------------------------------------- turno
    async def _turn(self, text: str) -> None:
        streamed = False
        async for event in self.backend.ask(text):
            etype = event.get("type")
            if etype == "token":
                if not streamed:
                    self.write("● ", Fore.GREEN + Style.BRIGHT)
                    streamed = True
                self.write(event["text"])
            elif etype == "tool_call" and self.show_tools:
                if streamed:
                    print()
                    streamed = False
                args = json.dumps(event.get("args", {}), ensure_ascii=False)
                self.info(f"  ⚙ {event.get('name')} {args[:200]}", Fore.YELLOW)
            elif etype == "confirm_request":
                approved = self._ask_confirmation(event)
                await self.backend.confirm(event["id"], approved)
            elif etype == "tool_result" and self.show_tools:
                status = "✔" if event.get("success") else "✘"
                color = Fore.LIGHTBLACK_EX if event.get("success") else Fore.RED
                lines = str(event.get("output", "")).splitlines()
                preview = "\n".join("    " + line for line in lines[:8])
                more = f"\n    … ({len(lines) - 8} líneas más)" if len(lines) > 8 else ""
                self.info(f"  {status} {event.get('name')} ({event.get('duration_ms', 0)} ms)\n{preview}{more}", color)
            elif etype == "error":
                if streamed:
                    print()
                    streamed = False
                self.info(f"  ! {event.get('message')}", Fore.RED)
            elif etype == "final":
                if not streamed:
                    # La respuesta final no se emitió en streaming (p. ej. límite de iteraciones)
                    self.write("● ", Fore.GREEN + Style.BRIGHT)
                    self.write(event.get("text", ""))
                print("\n")

    def _ask_confirmation(self, event: dict[str, Any]) -> bool:
        print()
        self.info("  ⚠ Acción que requiere confirmación", Fore.RED + Style.BRIGHT)
        self.info(f"    Tool: {event.get('name')}  Args: {json.dumps(event.get('args', {}), ensure_ascii=False)}",
                  Fore.RED)
        self.info(f"    Motivo: {event.get('reason')}", Fore.RED)
        try:
            answer = input(f"{Fore.RED}    ¿Ejecutar? [s/N]: {Style.RESET_ALL}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        return answer in {"s", "si", "sí", "y", "yes"}

    # ------------------------------------------------------------- comandos
    def _command(self, line: str) -> bool:
        """Ejecuta un comando /... Devuelve False si hay que salir."""
        cmd, _, arg = line.partition(" ")
        cmd = cmd.lower()
        if cmd in ("/exit", "/quit"):
            return False
        if cmd == "/help":
            self.info(HELP)
        elif cmd == "/clear":
            self.run_async(self.backend.clear())
            print("\033[2J\033[H", end="")
            self.info(f"Nueva conversación (sesión {self.backend.session_id})")
        elif cmd == "/history":
            limit = int(arg) if arg.strip().isdigit() else 20
            rows = self.run_async(self.backend.history(limit))
            if not rows:
                self.info("(sin mensajes en esta sesión)")
            for row in rows:
                role = row.get("role")
                color = {"user": Fore.CYAN, "assistant": Fore.GREEN, "tool": Fore.LIGHTBLACK_EX}.get(role, "")
                content = " ".join(str(row.get("content", "")).split())
                label = f"{role}:{row['tool_name']}" if role == "tool" and row.get("tool_name") else role
                print(f"{color}[{row.get('timestamp', '')}] {label}: {content[:300]}{Style.RESET_ALL}")
        elif cmd == "/sessions":
            for s in self.run_async(self.backend.sessions()):
                title = " ".join(str(s.get("title") or "").split())[:60]
                marker = "*" if s.get("session_id") == self.backend.session_id else " "
                print(f"{marker} {s['session_id']}  {s.get('last_activity')}  ({s.get('message_count')} msgs)  {title}")
        elif cmd == "/resume":
            if not arg.strip():
                self.info("Uso: /resume <session_id>", Fore.RED)
            else:
                self.run_async(self.backend.resume(arg.strip()))
                self.info(f"Sesión reanudada: {self.backend.session_id}")
        elif cmd == "/status":
            status = self.run_async(self.backend.status())
            for key in ("llm", "mode", "session_id", "tools"):
                if key in status:
                    print(f"  {key}: {status[key]}")
        else:
            self.info(f"Comando desconocido: {cmd}. Escribe /help", Fore.RED)
        return True

    # ------------------------------------------------------------- bucle principal
    def run(self) -> int:
        self.run_async(self.backend.start())
        self.info(f"AgentOS v0.1 — LLM: {self.backend.llm_name} — sesión {self.backend.session_id}", Fore.MAGENTA)
        self.info("Escribe tu petición en lenguaje natural. /help para ver los comandos.\n", Fore.MAGENTA)
        try:
            while True:
                try:
                    line = input(f"{Fore.CYAN}{Style.BRIGHT}{PROMPT}{Style.RESET_ALL}").strip()
                except EOFError:
                    print()
                    break
                except KeyboardInterrupt:
                    print()
                    continue
                if not line:
                    continue
                if line.startswith("/"):
                    if not self._command(line):
                        break
                    continue
                task = self.loop.create_task(self._turn(line))
                try:
                    self.loop.run_until_complete(task)
                except KeyboardInterrupt:
                    task.cancel()
                    try:
                        self.loop.run_until_complete(task)
                    except (asyncio.CancelledError, Exception):
                        pass
                    self.info("\n[interrumpido]", Fore.RED)
                except ConnectionError as exc:
                    self.info(f"Conexión con AgentD perdida: {exc}", Fore.RED)
                    return 1
        finally:
            try:
                self.run_async(self.backend.close())
            finally:
                self.loop.close()
        self.info("Hasta luego.", Fore.MAGENTA)
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chat de texto de AgentOS")
    parser.add_argument("--direct", action="store_true", help="Ejecutar el agente en proceso (sin AgentD)")
    parser.add_argument("--socket", help="Ruta del socket de AgentD")
    parser.add_argument("--backend", choices=["auto", "llama_cpp", "openai", "mock"],
                        help="Backend LLM (solo modo directo)")
    parser.add_argument("--typewriter", type=float, default=0.0, help="Retardo por carácter en segundos")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--quiet-tools", action="store_true", help="No mostrar tool calls ni resultados")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)

    colorama_init(strip=args.no_color or None)
    config = get_config()
    setup_logging(config, args.log_level)
    if args.backend:
        config.llm_backend = args.backend
    socket_path = Path(args.socket).expanduser() if args.socket else config.socket_path

    backend: Backend
    if args.direct:
        backend = DirectBackend()
    elif socket_path.exists():
        backend = SocketBackend(socket_path)
    else:
        print(f"{Fore.YELLOW}AgentD no está escuchando en {socket_path}; usando modo directo.{Style.RESET_ALL}")
        backend = DirectBackend()

    try:
        return ChatUI(backend, args.typewriter, not args.quiet_tools).run()
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        print(f"{Fore.RED}No se pudo conectar con AgentD: {exc}{Style.RESET_ALL}")
        return 1
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("agentos.tui").debug("fallo", exc_info=True)
        print(f"{Fore.RED}Error: {exc}{Style.RESET_ALL}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
