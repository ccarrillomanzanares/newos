"""Servidor de la interfaz gráfica: HTTP estático + WebSocket /ws ↔ AgentD (socket Unix, NDJSON).

Protocolo navegador → servidor:
    {"type": "message", "text": "..."}
    {"type": "confirm", "id": "...", "approved": true|false}
Servidor → navegador:
    {"type": "ready", "llm": "..."}
    {"type": "token", "text": "..."}
    {"type": "tool", "name": "...", "status": "running|ok|error"}
    {"type": "confirm", "id": "...", "name": "...", "args": {...}, "reason": "..."}
    {"type": "done", "text": "..."}
    {"type": "error", "text": "..."}

Uso: python -m ui.gui.server [--host 127.0.0.1] [--port 8080] [--socket RUTA]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes
import os
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from websockets.exceptions import ConnectionClosed

try:   # websockets >= 13 (API nueva)
    from websockets.asyncio.server import serve
    from websockets.datastructures import Headers
    from websockets.http11 import Response
    LEGACY = False
except ImportError:   # websockets 10–12 (p. ej. Buildroot 2024.02)
    from websockets.server import serve  # type: ignore[no-redef]
    LEGACY = True

log = logging.getLogger("agentos.gui")
STATIC_DIR = Path(__file__).parent / "static"
# Tipos que el visor puede mostrar vía /file?path=... (solo se escucha en 127.0.0.1)
VIEWABLE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".pdf", ".txt", ".md", ".log",
            ".json", ".py", ".sh", ".csv", ".html"}


def default_socket() -> Path:
    """Misma lógica que AgentConfig: AGENTOS_SOCKET > modo os > DATA_DIR/run/agentos.sock."""
    if os.environ.get("AGENTOS_SOCKET"):
        return Path(os.environ["AGENTOS_SOCKET"]).expanduser()
    if os.environ.get("AGENTOS_MODE") == "os":
        return Path("/run/agentos/input.sock")
    data = Path(os.environ.get("AGENTOS_DATA_DIR") or Path.home() / ".agentos").expanduser()
    return data / "run" / "agentos.sock"


async def wait_for_socket(path: Path, timeout: float = 30.0) -> bool:
    for _ in range(int(timeout / 0.5)):
        if path.is_socket():
            return True
        await asyncio.sleep(0.5)
    return path.is_socket()


def _response(status: HTTPStatus, body: bytes, ctype: str = "text/plain; charset=utf-8"):
    headers = [("Content-Type", ctype), ("Content-Length", str(len(body))),
               ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")]
    if LEGACY:
        return status, headers, body
    return Response(status.value, status.phrase, Headers(headers), body)


def http_response(path: str):
    """Sirve HTTP normal; devuelve None solo para el handshake WebSocket en /ws."""
    url = urlsplit(path)
    if url.path == "/ws":
        return None
    if url.path == "/file":
        raw = (parse_qs(url.query).get("path") or [""])[0]
        path = Path(raw).expanduser()
        if not path.is_absolute() or path.suffix.lower() not in VIEWABLE or not path.is_file():
            return _response(HTTPStatus.NOT_FOUND, b"no disponible")
        ctype = mimetypes.guess_type(path.name)[0] or "text/plain"
        if ctype.startswith("text/") or path.suffix.lower() in {".json", ".py", ".sh", ".md", ".log"}:
            ctype = "text/plain; charset=utf-8"   # nunca ejecutar HTML/JS de ficheros locales
        return _response(HTTPStatus.OK, path.read_bytes()[:20_000_000], ctype)
    name = "index.html" if url.path in ("", "/") else url.path.lstrip("/")
    target = (STATIC_DIR / name).resolve()
    if STATIC_DIR.resolve() not in target.parents or not target.is_file():
        return _response(HTTPStatus.NOT_FOUND, b"no encontrado")
    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if ctype.startswith("text/"):
        ctype += "; charset=utf-8"
    return _response(HTTPStatus.OK, target.read_bytes(), ctype)


def process_request(connection, request):           # API nueva
    return http_response(request.path)


async def process_request_legacy(path, request_headers):  # API antigua
    return http_response(path)


class Bridge:
    """Una conexión WebSocket ↔ una conexión NDJSON con AgentD."""

    def __init__(self, ws, socket_path: Path) -> None:
        self.ws = ws
        self.socket_path = socket_path
        self.tools: dict[str, str] = {}   # id → nombre

    async def to_browser(self, msg: dict) -> None:
        await self.ws.send(json.dumps(msg, ensure_ascii=False))

    def translate(self, ev: dict) -> dict | None:
        t = ev.get("type")
        if t == "hello":
            return {"type": "ready", "llm": ev.get("llm")}
        if t == "token":
            return {"type": "token", "text": ev.get("text", "")}
        if t == "tool_call":
            self.tools[str(ev.get("id"))] = ev.get("name", "")
            return {"type": "tool", "id": ev.get("id"), "name": ev.get("name", ""), "status": "running"}
        if t == "tool_result":
            name = self.tools.pop(str(ev.get("id")), ev.get("name", ""))
            return {"type": "tool", "id": ev.get("id"), "name": name,
                    "status": "ok" if ev.get("success") else "error",
                    "output": str(ev.get("output", ""))[:4000]}
        if t == "confirm_request":
            return {"type": "confirm", "id": ev.get("id"), "name": ev.get("name"),
                    "args": ev.get("args"), "reason": ev.get("reason", "")}
        if t == "final":
            return {"type": "done", "text": ev.get("text", "")}
        if t == "error":
            return {"type": "error", "text": ev.get("message", "error")}
        return None   # pong/status/history… no se usan en la UI

    async def run(self) -> None:
        if not await wait_for_socket(self.socket_path, timeout=10.0):
            await self.to_browser({"type": "error", "text": f"AgentD no disponible ({self.socket_path})"})
            return
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.socket_path), limit=8 * 1024 * 1024)
        except OSError as exc:
            await self.to_browser({"type": "error", "text": f"No se pudo conectar con AgentD: {exc}"})
            return

        async def agent_to_ws() -> None:
            while line := await reader.readline():
                try:
                    out = self.translate(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if out:
                    await self.to_browser(out)
            await self.to_browser({"type": "error", "text": "AgentD cerró la conexión"})

        async def ws_to_agent() -> None:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if msg.get("type") == "message" and str(msg.get("text", "")).strip():
                    out = {"type": "message", "text": str(msg["text"])}
                elif msg.get("type") == "confirm":
                    out = {"type": "confirm", "id": msg.get("id"), "approved": bool(msg.get("approved"))}
                else:
                    continue
                writer.write((json.dumps(out, ensure_ascii=False) + "\n").encode())
                await writer.drain()

        tasks = [asyncio.create_task(agent_to_ws()), asyncio.create_task(ws_to_agent())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except ConnectionClosed:
            pass
        finally:
            for t in tasks:
                t.cancel()
            writer.close()


async def main(host: str, port: int, socket_path: Path) -> None:
    log.info("Esperando a AgentD en %s …", socket_path)
    if await wait_for_socket(socket_path, 30.0):
        log.info("AgentD listo")
    else:
        log.warning("AgentD no responde todavía; la UI reintentará al conectar")

    async def handler(ws, *_: object) -> None:
        try:
            await Bridge(ws, socket_path).run()
        except ConnectionClosed:
            pass

    pr = process_request_legacy if LEGACY else process_request
    async with serve(handler, host, port, process_request=pr, max_size=2**20):
        log.info("Interfaz en http://%s:%d", host, port)
        await asyncio.Future()


def cli() -> None:
    ap = argparse.ArgumentParser(description="Servidor de la interfaz gráfica de AgentOS")
    ap.add_argument("--host", default=os.environ.get("AGENTOS_GUI_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("AGENTOS_GUI_PORT", "8080")))
    ap.add_argument("--socket", type=Path, default=None)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="[gui] %(message)s")
    try:
        asyncio.run(main(a.host, a.port, a.socket or default_socket()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
