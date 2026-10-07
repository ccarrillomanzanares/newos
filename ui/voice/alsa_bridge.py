"""Puente de audio por ALSA para la voz de AgentOS.

EL PROBLEMA QUE RESUELVE
------------------------
El navegador (cog/WPE) NO puede capturar el microfono: la imagen no trae
GStreamer ni WebRTC, asi que `getUserMedia` no lee nada. Tocar el orbe se veia
bonito (se ponia dorado) pero el agente no oia.

LA SOLUCION
-----------
El micro lo lee el SERVIDOR con `arecord` (o `parec`), se lo manda a Gemini Live
por WebSocket y la respuesta se reproduce con `aplay`. La pagina solo enciende
y apaga: la captura no pasa por el navegador. Sin GStreamer y sin WebRTC.

    arecord (micro)  --PCM 16 kHz-->  Gemini Live  --PCM 24 kHz-->  aplay

Uso desde el servidor de la interfaz:

    from ui.voice.alsa_bridge import AudioBridge
    bridge = AudioBridge()
    await bridge.start()      # abre micro + sesion Live + altavoz
    ...
    await bridge.stop()

Tambien se puede probar a mano:  python3 -m ui.voice.alsa_bridge --probe
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .gemini_live import GeminiLive, IN_RATE, OUT_RATE, DEFAULT_VOICE

log = logging.getLogger("agentos.voice.alsa")

# Cuantos segundos de senal se piden al micro para decidir si ese dispositivo
# funciona de verdad (no basta con que el comando exista: hay que oir bytes).
PROBE_SECONDS = 1.0
# Tamano del trozo que se manda al modelo (100 ms). Mas pequeno = menos
# latencia de subida; mas grande = menos paquetes. 100 ms va bien.
CHUNK = IN_RATE * 2 // 10


# ---------------------------------------------------------------------------
# Deteccion del micro: se PRUEBA, no se adivina
# ---------------------------------------------------------------------------
def tarjetas():
    """Tarjetas de sonido que ve el kernel, segun /proc/asound/cards."""
    out = []
    try:
        for line in Path("/proc/asound/cards").read_text().splitlines():
            line = line.strip()
            # Formato:  " 0 [PCH            ]: HDA-Intel - HDA Intel PCH"
            if line and line[0].isdigit() and "[" in line:
                idx = line.split()[0]
                nombre = line.split("[", 1)[1].split("]", 1)[0].strip()
                out.append((idx, nombre))
    except OSError:
        pass
    return out


def candidatos_captura():
    """Comandos posibles para leer el micro, de mas a menos preferido.

    - `plughw:N,0`  : acceso crudo a la tarjeta N (no necesita mixer ni nada).
    - `default`     : lo que diga la config ALSA (o PulseAudio si esta corriendo).
    - `parec`       : si PulseAudio esta vivo, el reescala a 16 kHz el solo.
    """
    cmds = []
    for idx, _ in tarjetas():
        cmds.append(["arecord", "-D", f"plughw:{idx},0", "-f", "S16_LE",
                     "-r", str(IN_RATE), "-c", "1", "-t", "raw", "-q", "-"])
    cmds.append(["arecord", "-D", "default", "-f", "S16_LE",
                 "-r", str(IN_RATE), "-c", "1", "-t", "raw", "-q", "-"])
    if shutil.which("parec"):
        cmds.append(["parec", "--format=s16le", "--rate=%d" % IN_RATE,
                     "--channels=1"])
    return cmds


def probar_captura(cmd, segundos=PROBE_SECONDS):
    """Lanza el comando y comprueba que SALE senal de verdad.

    Devuelve los bytes leidos (puede ser silencio digital, eso vale) o None si
    el comando no arranca o no produce nada.
    """
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except (OSError, FileNotFoundError):
        return None
    if p.stdout is None:
        p.kill()
        return None
    leido = b""
    limite = time.time() + segundos
    try:
        while time.time() < limite and len(leido) < CHUNK:
            r, _, _ = select.select([p.stdout.fileno()], [], [], 0.2)
            if not r:
                continue
            trozo = os.read(p.stdout.fileno(), 4096)
            if not trozo:
                break
            leido += trozo
    except OSError:
        pass
    finally:
        try:
            p.terminate()
            p.wait(timeout=2)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    return leido or None


def elegir_captura(verbose=False):
    """Primer comando de micro que produce audio. None si no hay ninguno."""
    for cmd in candidatos_captura():
        if not shutil.which(cmd[0]):
            continue
        datos = probar_captura(cmd)
        if verbose:
            print("  %-60s -> %s" % (" ".join(cmd), "OK (%d bytes)" % len(datos) if datos else "sin senal"))
        if datos:
            return cmd
    return None


# ---------------------------------------------------------------------------
# El puente
# ---------------------------------------------------------------------------
class AudioBridge:
    """Sesion de voz de punta a punta: micro -> Gemini Live -> altavoz.

    Un solo objeto por vez (no hay dos micros a la vez).
    """

    def __init__(self, voice: str | None = None, lang: str | None = None,
                 on_text=None, on_state=None):
        self.voice = voice
        self.lang = lang
        self.on_text = on_text          # callback(texto, es_final)
        self.on_state = on_state        # callback("listening"|"idle"|"error")
        self.g: GeminiLive | None = None
        self._mic = None                # subproceso arecord
        self._salida = None             # subproceso aplay
        self._tareas: list[asyncio.Task] = []
        self._vivo = False
        self.comando_micro = None
        self.error = ""

    # -- estado ------------------------------------------------------------
    @property
    def activo(self) -> bool:
        return self._vivo

    def _estado(self, cual):
        if self.on_state:
            try:
                self.on_state(cual)
            except Exception:
                pass

    def _texto(self, txt, fin=False):
        if self.on_text:
            try:
                self.on_text(txt, fin)
            except Exception:
                pass

    # -- altavoz -----------------------------------------------------------
    def _abrir_salida(self):
        """Un `aplay` persistente: se le escriben los trozos segun llegan."""
        if not shutil.which("aplay"):
            return None
        for dev in ("default", "plughw:0,0"):
            cmd = ["aplay", "-D", dev, "-f", "S16_LE", "-r", str(OUT_RATE),
                   "-c", "1", "-t", "raw", "-q", "-"]
            try:
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                time.sleep(0.05)                      # si el dispositivo no existe, muere ya
                if p.poll() is None:
                    log.info("altavoz: %s", " ".join(cmd))
                    return p
            except OSError:
                continue
        return None

    def _suena(self, pcm: bytes):
        if not pcm:
            return
        if self._salida is None or self._salida.poll() is not None:
            self._salida = self._abrir_salida()
        if self._salida is None:
            return
        try:
            if self._salida.stdin:
                self._salida.stdin.write(pcm)
                self._salida.stdin.flush()
        except (BrokenPipeError, ValueError, AttributeError):
            self._salida = None
        # (si no hay altavoz, el texto de la transcripcion sigue saliendo en el chat)

    # -- micro -------------------------------------------------------------
    async def _bombea_micro(self):
        cmd = self.comando_micro
        try:
            self._mic = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
        except OSError as exc:
            self.error = "no se pudo abrir el micro: %s" % exc
            log.error(self.error)
            return
        log.info("micro: %s", " ".join(cmd))
        while self._vivo:
            try:
                trozo = await asyncio.wait_for(self._mic.stdout.read(CHUNK), timeout=5)
            except asyncio.TimeoutError:
                continue
            if not trozo:
                break
            try:
                await self.g.audio(trozo)
            except Exception as exc:
                log.warning("se corto el envio al modelo: %s", exc)
                break

    # -- respuestas --------------------------------------------------------
    async def _bombea_modelo(self):
        while self._vivo:
            try:
                async for t, a in self.g.frases():
                    if a:
                        self._suena(a)
                    if t == "[fin]":
                        self._texto("", True)
                    elif t and t != "[interrumpido]":
                        self._texto(t)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("se corto la lectura del modelo: %s", exc)
                break

    # -- ciclo de vida -----------------------------------------------------
    async def start(self) -> bool:
        """Abre micro + modelo + altavoz. True si la voz queda funcionando."""
        if self._vivo:
            return True
        self.error = ""
        if self.comando_micro is None:
            loop = asyncio.get_running_loop()
            self.comando_micro = await loop.run_in_executor(None, elegir_captura)
        if not self.comando_micro:
            self.error = "no hay ningun dispositivo de microfono con senal"
            log.error(self.error)
            self._estado("error")
            return False

        self.g = GeminiLive(voice=self.voice or DEFAULT_VOICE,
                            lang=self.lang if self.lang is not None else "")
        try:
            await self.g.conectar()
        except Exception as exc:
            self.error = "no se pudo abrir la sesion de voz: %s" % exc
            log.error(self.error)
            self._estado("error")
            return False

        self._vivo = True
        self._salida = self._abrir_salida()
        self._estado("listening")
        self._tareas = [asyncio.create_task(self._bombea_micro()),
                        asyncio.create_task(self._bombea_modelo())]
        log.info("voz lista (micro %s, voz %s)", " ".join(self.comando_micro), self.g.voice)
        return True

    async def stop(self):
        self._vivo = False
        for t in self._tareas:
            t.cancel()
        self._tareas = []
        if self._mic and self._mic.returncode is None:
            try:
                self._mic.terminate()
            except ProcessLookupError:
                pass
        if self._salida:
            try:
                self._salida.stdin.close()
            except Exception:
                pass
            try:
                self._salida.terminate()
            except Exception:
                pass
        self._salida = None
        self._mic = None
        if self.g:
            try:
                await self.g.cerrar()
            except Exception:
                pass
        self.g = None
        self._estado("idle")


# ---------------------------------------------------------------------------
# Prueba manual
# ---------------------------------------------------------------------------
def _main() -> int:
    ap = argparse.ArgumentParser(description="Puente de audio por ALSA para AgentOS")
    ap.add_argument("--probe", action="store_true",
                    help="solo mirar que dispositivos de micro tienen senal")
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--lang", default=os.environ.get("AGENTOS_LIVE_LANG", ""))
    ap.add_argument("--seconds", type=float, default=30, help="duracion de la prueba de voz")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    print("tarjetas de sonido:")
    for idx, nombre in tarjetas() or [("-", "(ninguna)")]:
        print("  %s  %s" % (idx, nombre))
    print("candidatos de micro:")
    for cmd in candidatos_captura():
        print("  " + " ".join(cmd))
    print("probando cual tiene senal...")
    elegido = elegir_captura(verbose=True)
    if a.probe:
        print("\nELEGIDO:", " ".join(elegido) if elegido else "NINGUNO")
        return 0 if elegido else 1
    if not elegido:
        print("\nNo hay micro con senal. Nada que probar.")
        return 1

    async def correr():
        b = AudioBridge(voice=a.voice, lang=a.lang,
                        on_text=lambda t, fin=False: print(("  [fin]" if fin else "  > " + t)))
        if not await b.start():
            print("ERROR:", b.error)
            return 1
        print("\nhabla por el micro (%0.f s)... guarda silencio si no quieres que conteste." % a.seconds)
        await asyncio.sleep(a.seconds)
        await b.stop()
        print("fin de la prueba")
        return 0

    return asyncio.run(correr())


if __name__ == "__main__":
    raise SystemExit(_main())
