"""Arranca el servidor de la interfaz y abre el navegador en modo kiosco (ventana completa).

* Wayland: cog → chromium → firefox
* X11:     chromium → firefox
* Sin display: solo el servidor (sandbox / headless)

Uso: python -m ui.gui.launch [--port 8080] [--socket RUTA] [--no-browser]
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time


def browser_command(url: str) -> list[str] | None:
    wayland, x11 = os.environ.get("WAYLAND_DISPLAY"), os.environ.get("DISPLAY")
    chromium = shutil.which("chromium-browser") or shutil.which("chromium") or shutil.which("google-chrome")
    chrome_args = ["--kiosk", f"--app={url}", "--no-sandbox", "--noerrdialogs",
                   "--disable-infobars", "--no-first-run", "--disable-translate"]
    candidates: list[list[str]] = []
    if wayland:
        if shutil.which("cog"):
            # El módulo de plataforma Wayland de cog se llama "wl": el fichero es
            # /usr/lib/cog/modules/libcogplatform-wl.so. Con "wayland" aborta con
            # "cannot find module 'wayland'". Tampoco existe "gl".
            candidates.append(["cog", "--platform=wl", url])
        if chromium:
            candidates.append([chromium, "--ozone-platform=wayland", *chrome_args])
    if wayland or x11:
        if chromium:
            candidates.append([chromium, *chrome_args])
        if shutil.which("firefox"):
            candidates.append(["firefox", "--kiosk", url])
    return candidates[0] if candidates else None


def wait_port(port: int, timeout: float = 60.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Lanzador de la interfaz gráfica de AgentOS")
    ap.add_argument("--port", type=int, default=int(os.environ.get("AGENTOS_GUI_PORT", "8080")))
    ap.add_argument("--no-browser", action="store_true", help="Solo arrancar el servidor")
    ap.add_argument("--socket", default=None, help="Ruta del socket de AgentD")
    args = ap.parse_args()
    url = f"http://localhost:{args.port}"

    server_cmd = [sys.executable, "-m", "ui.gui.server", "--port", str(args.port)]
    if args.socket:
        server_cmd += ["--socket", args.socket]
    server = subprocess.Popen(server_cmd)

    def stop(*_: object) -> None:
        server.terminate()
        try:
            server.wait(5)
        except subprocess.TimeoutExpired:
            server.kill()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    cmd = None if args.no_browser else browser_command(url)
    if cmd is None:
        print(f"[gui] Sin display o sin navegador: solo servidor en {url}", flush=True)
        return server.wait()

    # El servidor espera hasta 30 s a AgentD antes de escuchar
    if not wait_port(args.port):
        print("[gui] El servidor no arrancó a tiempo", file=sys.stderr)
        stop()
    print(f"[gui] Abriendo {url} con {os.path.basename(cmd[0])}", flush=True)
    # cog abre su superficie a 1024x768 por defecto (COG_PLATFORM_WL_VIEW_WIDTH/
    # HEIGHT), asi que la interfaz NO llenaba la pantalla: se veia un recuadro
    # sobre el fondo del compositor. Con FULLSCREEN=1 la superficie cubre TODA la
    # salida, sea cual sea la resolucion (dice la doc de cog, platform-wl.md).
    if os.path.basename(cmd[0]) == "cog":
        os.environ["COG_PLATFORM_WL_VIEW_FULLSCREEN"] = "1"
    try:
        code = subprocess.call(cmd)      # el navegador es el proceso principal
    finally:
        server.terminate()
    return code


if __name__ == "__main__":
    sys.exit(main())
