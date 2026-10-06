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
import subprocess
import sys
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

# --------------------------------------------------------------------------
# Red / WiFi (nmcli) y configuracion del proveedor LLM
# El servidor corre como root (lo lanza S99agentos), asi que puede ejecutar
# nmcli y escribir /etc/default/agentos.
# --------------------------------------------------------------------------
LLM_CONF = Path("/etc/default/agentos")


def _run(args, timeout=30):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except Exception as exc:                      # nmcli ausente, timeout...
        return 1, "", str(exc)


def _json(obj, status=HTTPStatus.OK):
    return _response(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                     "application/json; charset=utf-8")


def _tcp_ok(host, port=443, timeout=3.0):
    import socket as _s
    try:
        with _s.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def has_internet():
    """Comprueba INTERNET de verdad, no el estado de NetworkManager: este dice
    "connected (local only)" o cuenta una eth0 sin salida como conectada. Se
    prueba una conexion TCP al endpoint del LLM y a 1.1.1.1."""
    st = llm_status()
    host = "1.1.1.1"
    base = st.get("base_url") or ""
    if "//" in base:
        host = base.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0] or host
    return _tcp_ok(host) or _tcp_ok("1.1.1.1")


def net_connected():
    """(hay_internet, ssid). El panel de red se muestra si NO hay internet."""
    if has_internet():
        ssid = ""
        rc, out, _ = _run(["nmcli", "-t", "-f", "ACTIVE,SSID", "device", "wifi"])
        for line in out.splitlines():
            if line.startswith(("yes:", "*:")):
                ssid = line.split(":", 1)[1].strip()
        return True, ssid
    return False, ""


def net_list():
    """Redes WiFi visibles, de mayor a menor senal."""
    rc, out, _ = _run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY",
                       "device", "wifi", "list", "--rescan", "yes"], timeout=40)
    seen = {}
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 4 or not parts[1]:
            continue
        ssid = parts[1]
        if ssid in seen:
            continue
        try:
            sig = int(parts[2])
        except ValueError:
            sig = 0
        seen[ssid] = {"ssid": ssid, "signal": sig,
                      "security": ":".join(parts[3:]) or "abierta",
                      "active": parts[0] in ("yes", "*")}
    return sorted(seen.values(), key=lambda n: -n["signal"])


def net_connect(ssid, password):
    args = ["nmcli", "--wait", "45", "device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    rc, out, err = _run(args, timeout=60)
    return rc == 0, (out + err).strip()


def llm_status():
    cfg = {}
    if LLM_CONF.is_file():
        for line in LLM_CONF.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip("'\"")
    return {"backend": cfg.get("AGENTOS_LLM_BACKEND", ""),
            "model": cfg.get("AGENTOS_OPENAI_MODEL", ""),
            "base_url": cfg.get("AGENTOS_OPENAI_BASE_URL", ""),
            "lang": cfg.get("AGENTOS_LANG", ""),
            "voice": cfg.get("AGENTOS_LIVE_VOICE", ""),
            "live_model": cfg.get("AGENTOS_LIVE_MODEL", ""),
            "voice_chosen": bool(cfg.get("AGENTOS_VOICE_CHOSEN")),
            "configured": bool(cfg.get("AGENTOS_OPENAI_API_KEY"))}


WESTON_INI = Path("/etc/xdg/weston/weston.ini")


def set_lang(code):
    """Guarda el idioma y lo aplica: (a) al teclado de pantalla lo hace la UI,
    (b) al teclado FISICO hay que reescribir weston.ini y reiniciar Weston
    (Weston lee keymap_layout solo al arrancar)."""
    if not code or len(code) > 8:
        return False
    # /etc/default/agentos: AGENTOS_LANG=xx
    if LLM_CONF.is_file():
        keep = [l for l in LLM_CONF.read_text(encoding="utf-8", errors="replace").splitlines()
                if not l.startswith("AGENTOS_LANG=")]
    else:
        keep = []
    keep.append("AGENTOS_LANG=%s" % code)
    LLM_CONF.write_text("\n".join(keep) + "\n", encoding="utf-8")
    # weston.ini: [keyboard] keymap_layout
    try:
        txt = WESTON_INI.read_text(encoding="utf-8")
        if "keymap_layout=" in txt:
            import re as _re
            txt = _re.sub(r"keymap_layout=\S*", "keymap_layout=" + code, txt)
        else:
            txt += "\n[keyboard]\nkeymap_layout=%s\n" % code
        WESTON_INI.write_text(txt, encoding="utf-8")
        # NO se reinicia Weston aqui: reiniciarlo MATA la sesion grafica (se cae al
        # tty con las frases de arranque), que es justo lo que pasaba al elegir
        # idioma. El cambio de layout del teclado FISICO se aplica solo en el
        # siguiente arranque (Weston lee keymap_layout al iniciarse). El teclado
        # de PANTALLA cambia al momento (lo hace la UI, no Weston).
    except Exception:
        pass
    return True


# --- Voces de Gemini Live (30 predefinidas; genero y caracter oficiales) ----
VOICES = [
    ("Kore", "Female", "Firm"), ("Aoede", "Female", "Breezy"),
    ("Autonoe", "Female", "Bright"), ("Callirrhoe", "Female", "Easy-going"),
    ("Despina", "Female", "Smooth"), ("Erinome", "Female", "Clear"),
    ("Gacrux", "Female", "Mature"), ("Laomedeia", "Female", "Upbeat"),
    ("Leda", "Female", "Youthful"), ("Pulcherrima", "Female", "Forward"),
    ("Achernar", "Female", "Soft"),
    ("Puck", "Male", "Upbeat"), ("Charon", "Male", "Informative"),
    ("Fenrir", "Male", "Excitable"), ("Orus", "Male", "Firm"),
    ("Iapetus", "Male", "Clear"), ("Algenib", "Male", "Gravelly"),
    ("Algieba", "Male", "Smooth"), ("Alnilam", "Male", "Firm"),
    ("Achird", "Male", "Friendly"), ("Enceladus", "Male", "Breathy"),
    ("Rasalgethi", "Male", "Informative"), ("Sadachbia", "Male", "Lively"),
    ("Sadaltager", "Male", "Knowledgeable"), ("Schedar", "Male", "Even"),
    ("Umbriel", "Male", "Relaxed"), ("Zubenelgenubi", "Male", "Casual"),
    ("Sulafat", "Male", "Warm"), ("Vindemiatrix", "Male", "Gentle"),
    ("Zephyr", "Neutral", "Bright"),
]


def set_voice(voz):
    """Guarda la voz elegida (y marca que ya se eligio, para no preguntar mas)."""
    if not voz or len(voz) > 24 or not voz.isalnum():
        return False
    keep = []
    if LLM_CONF.is_file():
        keep = [l for l in LLM_CONF.read_text(encoding="utf-8", errors="replace").splitlines()
                if not l.startswith(("AGENTOS_LIVE_VOICE=", "AGENTOS_VOICE_CHOSEN="))]
    keep += ["AGENTOS_LIVE_VOICE='%s'" % voz, "AGENTOS_VOICE_CHOSEN=1"]
    LLM_CONF.write_text("\n".join(keep) + "\n", encoding="utf-8")
    return True


def voice_preview(voz):
    """Genera una muestra de esa voz y la reproduce (en segundo plano, sin bloquear)."""
    if not voz or len(voz) > 24:
        return False
    frase = "Hola, soy AgentD. Asi suena mi voz."
    try:
        subprocess.Popen(
            [sys.executable, "-m", "ui.voice.gemini_live", "--test", frase,
             "--voice", voz, "--out", "/tmp/voice_preview.wav"],
            cwd="/opt/agentos",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def llm_set(backend, key, model, base_url, lang=""):
    """Escribe la config del proveedor y reinicia AgentD para que la use."""
    lines = []
    if LLM_CONF.is_file():
        keep = [l for l in LLM_CONF.read_text(encoding="utf-8", errors="replace").splitlines()
                if not l.startswith(("AGENTOS_LLM_BACKEND=", "AGENTOS_OPENAI_API_KEY=",
                                     "AGENTOS_OPENAI_MODEL=", "AGENTOS_OPENAI_BASE_URL="))]
        lines = keep
    if lang:
        lines += ["AGENTOS_LANG=%s" % lang]
    lines += ["AGENTOS_LLM_BACKEND=%s" % (backend or "openai"),
              "AGENTOS_OPENAI_BASE_URL=%s" % (base_url or ""),
              "AGENTOS_OPENAI_API_KEY='%s'" % key,
              "AGENTOS_OPENAI_MODEL='%s'" % (model or "")]
    LLM_CONF.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        subprocess.Popen(["/etc/init.d/S99agentos", "restart"])
    except Exception:
        pass
    return True





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
    if url.path == "/net/status":
        ok, ssid = net_connected()
        return _json({"connected": ok, "ssid": ssid})
    if url.path == "/net/list":
        return _json({"networks": net_list()})
    if url.path == "/net/connect":
        ssid = (parse_qs(url.query).get("ssid") or [""])[0]
        pw = (parse_qs(url.query).get("password") or [""])[0]
        if not ssid:
            return _json({"ok": False, "msg": "falta la red"}, HTTPStatus.BAD_REQUEST)
        ok, msg = net_connect(ssid, pw)
        if ok:      # ya hay red: relanzar AgentD
            try:
                subprocess.Popen(["/etc/init.d/S99agentos", "restart"])
            except Exception:
                pass
        return _json({"ok": ok, "msg": msg})
    if url.path == "/voice/list":
        return _json({"voices": [{"name": n, "gender": g, "style": e} for n, g, e in VOICES],
                      "current": llm_status().get("voice", "")})
    if url.path == "/voice/set":
        voz = (parse_qs(url.query).get("voice") or [""])[0]
        return _json({"ok": set_voice(voz), "voice": voz})
    if url.path == "/voice/preview":
        voz = (parse_qs(url.query).get("voice") or [""])[0]
        return _json({"ok": voice_preview(voz), "voice": voz})
    if url.path == "/llm/status":
        return _json(llm_status())
    if url.path == "/llm/set":
        q = parse_qs(url.query)
        lang = (q.get("lang") or [""])[0]
        if lang:
            set_lang(lang)
        if (q.get("key") or [""])[0] or (q.get("model") or [""])[0]:
            llm_set((q.get("backend") or [""])[0], (q.get("key") or [""])[0],
                    (q.get("model") or [""])[0], (q.get("base_url") or [""])[0], lang)
        return _json({"ok": True})
    if url.path == "/sys/plain":
        # Info del sistema SIN HTML/markdown, lista para copiar y pegar.
        rc, out, _ = _run(["sh", "-c", "uname -a; echo; free -h; echo; df -h; echo; ip -br addr"], timeout=20)
        return _response(HTTPStatus.OK, out.encode("utf-8"), "text/plain; charset=utf-8")
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
