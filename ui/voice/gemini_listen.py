"""Modo micro: hablar por voz con AgentOS usando la Live API de Gemini.

Flujo:
  micro -> sounddevice (PCM 16k) -> Gemini Live -> audio 24k -> altavoces
El modelo oye, piensa y habla por si mismo (speech-to-speech). No usa
faster-whisper ni piper.

Dos modos:
  * push-to-talk (por defecto): habla mientras pulses ENTER y suelta.
  * --always: escucha continua (se corta con Ctrl-C).

El modelo responde en el idioma en que le hables (multilingual switching).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import subprocess
import sys
import threading
import time

from ui.voice.gemini_live import OUT_RATE, IN_RATE, GeminiLive

logger = logging.getLogger("agentos.voice.listen")

BLOCK = IN_RATE // 10          # 100 ms de audio por envio
CANAL_SALIDA = "/tmp/agentos_voz_out.raw"


class Salida:
    """Reproduce PCM crudo con el primer reproductor que exista."""

    def __init__(self):
        self.cmd = None
        for c in (["pw-play", "--rate", str(OUT_RATE), "--channels", "1", "--format", "s16", "-"],
                  ["paplay", "--raw", "--rate=" + str(OUT_RATE), "--channels=1", "--format=s16le"],
                  ["aplay", "-q", "-r", str(OUT_RATE), "-c", "1", "-f", "S16_LE", "-t", "raw", "-"]):
            if shutil.which(c[0]):
                self.cmd = c
                break
        self.proc = None

    def abrir(self):
        if self.cmd and not self.proc:
            self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def escribir(self, pcm: bytes):
        if not self.cmd:
            return
        self.abrir()
        try:
            self.proc.stdin.write(pcm)
            self.proc.stdin.flush()
        except (BrokenPipeError, AttributeError):
            self.proc = None

    def cortar(self):
        """Barge-in: corta la reproduccion en curso."""
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            self.proc.terminate()
            self.proc = None

    def cerrar(self):
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            self.proc = None


async def _enviar_micro(g: GeminiLive, parar: asyncio.Event, always: bool):
    import sounddevice as sd
    loop = asyncio.get_running_loop()
    cola: asyncio.Queue = asyncio.Queue()

    def cb(indata, frames, t, status):
        loop.call_soon_threadsafe(cola.put_nowait, bytes(indata))

    with sd.RawInputStream(samplerate=IN_RATE, channels=1, dtype="int16", blocksize=BLOCK, callback=cb):
        while not parar.is_set():
            try:
                datos = await asyncio.wait_for(cola.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if always or _hablando.is_set():
                await g.audio(datos)


async def _recibir(g: GeminiLive, salida: Salida):
    async for texto, audio in g.frases():
        if audio:
            if _cancelar.is_set():
                _cancelar.clear()
                salida.cortar()
            salida.escribir(audio)
        if texto and texto not in ("[fin]", "[interrumpido]"):
            print(texto, end="", flush=True)
        if texto == "[interrumpido]":
            salida.cortar()
        if texto == "[fin]":
            print()


_hablando = threading.Event()
_cancelar = threading.Event()


async def _ptt(parar: asyncio.Event):
    """Push-to-talk: ENTER para hablar."""
    print("\n[push-to-talk] Pulsa ENTER para hablar; Enter de nuevo para parar; Ctrl-C para salir.")
    loop = asyncio.get_running_loop()
    while not parar.is_set():
        await loop.run_in_executor(None, sys.stdin.readline)
        _hablando.set()
        print("... grabando (ENTER para terminar)")
        await loop.run_in_executor(None, sys.stdin.readline)
        _hablando.clear()
        print("... enviando")


async def escuchar(always: bool):
    g = GeminiLive()
    salida = Salida()
    parar = asyncio.Event()
    print(f"conectando con {g.model} (voz {g.voice})...")
    await g.conectar()
    print("listo.")

    tareas = [
        asyncio.create_task(_recibir(g, salida)),
        asyncio.create_task(_enviar_micro(g, parar, always)),
    ]
    if not always:
        tareas.append(asyncio.create_task(_ptt(parar)))
    try:
        await asyncio.gather(*tareas)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        parar.set()
        salida.cerrar()
        await g.cerrar()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Hablar con AgentOS (Gemini Live)")
    ap.add_argument("--listen", action="store_true", help="micro + altavoces")
    ap.add_argument("--always", action="store_true", help="escucha continua")
    ap.add_argument("--model", default=None)
    ap.add_argument("--voice", default=None)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not a.listen:
        ap.print_help()
        return 2
    import ui.voice.gemini_live as gl
    if a.model:
        gl.DEFAULT_MODEL = a.model
    if a.voice:
        gl.DEFAULT_VOICE = a.voice
    try:
        return asyncio.run(escuchar(a.always))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
