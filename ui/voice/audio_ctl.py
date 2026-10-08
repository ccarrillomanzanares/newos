"""Control del volumen REAL del sistema (mezclador ALSA).

POR QUE EXISTE ESTE FICHERO
---------------------------
El control de sonido de la interfaz solo cambiaba el volumen de la VOZ del
agente (escalando el PCM en memoria). Eso no sirve para dos cosas que el usuario
espera:
  * que el control suba/baje el sonido de TODO el sistema;
  * que las teclas de volumen del portatil lo cambien.
Aqui se actua sobre el MEZCLADOR de la tarjeta con `amixer`, que es lo que oye
el hardware: por eso afecta a todo lo que suena y se puede leer/ajustar tambien
desde fuera.

Los nombres de los controles CAMBIAN de una tarjeta a otra (Realtek suele traer
Master/PCM/Speaker/Headphone; otras solo PCM). Por eso NO se da por hecho
ninguno: se enumeran los que existen y se actua sobre todos los de salida.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess

log = logging.getLogger("agentos.voice.audio")

# Controles de SALIDA habituales, de mas general a mas concreto. Se aplican
# todos los que existan en la tarjeta.
SALIDA = ["Master", "PCM", "Speaker", "Headphone", "Front", "Surround", "Bass Speaker"]
# Controles de ENTRADA (micro): hay que subirlos y quitarles el mute o el agente
# no oye nada (es lo que pasaba: "silenciado o al minimo").
ENTRADA = ["Capture", "Mic", "Internal Mic", "Mic Boost", "Internal Mic Boost"]

_SIMPLE = re.compile(r"Simple mixer control '([^']+)'")


def _amixer(args, timeout=5):
    if not shutil.which("amixer"):
        return None
    try:
        return subprocess.run(["amixer", *args], capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def tarjetas():
    """Indices de tarjeta de sonido presentes (0, 1, ...)."""
    idx = []
    for i in range(4):
        p = _amixer(["-c", str(i), "scontrols"])
        if p is not None and p.returncode == 0:
            idx.append(i)
    return idx or [0]


def controles(card=0):
    """Nombres de los controles del mezclador de esa tarjeta."""
    p = _amixer(["-c", str(card), "scontrols"])
    if p is None or p.returncode != 0:
        return []
    return _SIMPLE.findall(p.stdout or "")


def aplicar(grupo=SALIDA, pct=70, card=0, quitar_mute=True):
    """Pone `pct` (0-100) y quita el mute de los controles del grupo que existan.

    Devuelve (aplicados, fallidos). No hace nada si la tarjeta no expone esos
    controles: en ese caso el sistema simplemente no tiene control por software
    de esa funcion.
    """
    pct = max(0, min(100, int(pct)))
    existentes = [c for c in controles(card)]
    hechos, fallidos = [], []
    for nombre in grupo:
        # Amixer acepta el nombre exacto; si no esta, falla y se ignora.
        if existentes and nombre not in existentes:
            continue
        args = ["-c", str(card), "sset", nombre, "%d%%" % pct]
        if quitar_mute:
            args.append("unmute")
        p = _amixer(args)
        if p is not None and p.returncode == 0:
            hechos.append(nombre)
        else:
            fallidos.append(nombre)
    return hechos, fallidos


def desilenciar_todo(card=0, pct=None):
    """Quita el mute de salida Y entrada. Se llama al arrancar el sistema.

    Es la causa del sintoma "los controles se ven pero no se oye": la tarjeta
    arranca con el altavoz silenciado y nadie lo quita.
    """
    hechos = []
    for grupo in (SALIDA, ENTRADA):
        for nombre in grupo:
            args = ["-c", str(card), "sset", nombre, "unmute"]
            if pct is not None:
                args.insert(3, "%d%%" % pct)
            p = _amixer(args)
            if p is not None and p.returncode == 0:
                hechos.append(nombre)
    return hechos


def volumen_actual(card=0):
    """Volumen actual (0-100) del primer control de salida que responda."""
    for nombre in SALIDA:
        p = _amixer(["-c", str(card), "sget", nombre])
        if p is None or p.returncode != 0:
            continue
        m = re.search(r"\[(\d+)%\]", p.stdout or "")
        if m:
            return int(m.group(1))
    return None


def esta_silenciado(card=0):
    """True si TODOS los controles de salida estan en [off]."""
    visto = False
    for nombre in SALIDA:
        p = _amixer(["-c", str(card), "sget", nombre])
        if p is None or p.returncode != 0:
            continue
        if "[on]" in (p.stdout or ""):
            return False
        visto = True
    return visto


def resumen(card=0):
    """Texto con el estado del mezclador, para el diagnostico."""
    lineas = ["tarjetas: %s" % tarjetas(), "controles: %s" % controles(card)]
    salida = [c for c in controles(card) if c in SALIDA]
    entrada = [c for c in controles(card) if c in ENTRADA]
    lineas.append("salida: %s -> %s%%" % (salida, volumen_actual(card)))
    lineas.append("entrada: %s" % entrada)
    lineas.append("silenciado: %s" % esta_silenciado(card))
    return "\n".join(lineas)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    print(resumen())
    if len(sys.argv) > 1:
        print("aplicando %s%%..." % sys.argv[1])
        print(aplicar(pct=int(sys.argv[1])))
        print("volumen ahora:", volumen_actual())
