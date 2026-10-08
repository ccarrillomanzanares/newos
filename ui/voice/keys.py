"""Teclas de volumen del portatil (SIN acpid ni entorno de escritorio).

En este sistema no hay ningun demonio que lea las teclas de funcion: se leen los
eventos de /dev/input/event* a pelo y se traduce a `amixer`. Asi las teclas de
subir/bajar volumen y silencio funcionan igual que en cualquier escritorio.

Formato de un evento de entrada en Linux (struct input_event, 64 bits):
    struct timeval { long tv_sec; long tv_usec; }  -> 16 bytes
    __u16 type; __u16 code; __s32 value;           -> 8 bytes
    total 24 bytes
Solo interesan type=EV_KEY(1), value=1 (pulsacion) y los codigos 113/114/115.
"""
from __future__ import annotations

import logging
import os
import select
import struct
import subprocess
import time

log = logging.getLogger("agentos.voice.keys")

EVENTO = struct.Struct("llHHi")          # tv_sec, tv_usec, type, code, value
EV_KEY = 0x01
MUTE, VOL_DOWN, VOL_UP = 113, 114, 115
PASO = 5                                 # % por pulsacion

from . import audio_ctl                  # noqa: E402  (mismo paquete)


def _dispositivos():
    """Todos los /dev/input/event* legibles."""
    try:
        nombres = [n for n in os.listdir("/dev/input") if n.startswith("event")]
    except OSError:
        return []
    return [os.path.join("/dev/input", n) for n in sorted(nombres)]


def _aplicar(delta=0, alternar_mute=False):
    if alternar_mute:
        audio_ctl.aplicar(audio_ctl.SALIDA, pct=0 if not audio_ctl.esta_silenciado() else 70,
                          quitar_mute=True)
        return
    actual = audio_ctl.volumen_actual()
    if actual is None:
        return
    audio_ctl.aplicar(audio_ctl.SALIDA, pct=max(0, min(100, actual + delta)),
                      quitar_mute=True)


def bucle():
    """Escucha los dispositivos de entrada y atiende las teclas de volumen."""
    fds = {}
    for dev in _dispositivos():
        try:
            fds[os.open(dev, os.O_RDONLY | os.O_NONBLOCK)] = dev
        except OSError:
            continue
    if not fds:
        log.warning("no hay dispositivos de entrada legibles")
        return
    log.info("escuchando teclas en %s", list(fds.values()))
    while True:
        try:
            listos, _, _ = select.select(list(fds), [], [], 2.0)
        except InterruptedError:
            continue
        for fd in listos:
            try:
                datos = os.read(fd, EVENTO.size * 32)
            except OSError:
                continue
            for i in range(0, len(datos) - EVENTO.size + 1, EVENTO.size):
                _t, _tv, tipo, codigo, valor = EVENTO.unpack_from(datos, i)
                if tipo != EV_KEY or valor != 1:
                    continue
                if codigo == VOL_UP:
                    _aplicar(delta=+PASO)
                elif codigo == VOL_DOWN:
                    _aplicar(delta=-PASO)
                elif codigo == MUTE:
                    _aplicar(alternar_mute=True)
        time.sleep(0.01)
