"""Voz de AgentOS con la Live API de Gemini (speech-to-speech).

Este backend es DISTINTO de stt.py/tts.py: ``gemini-*-live`` es un LLM con
audio nativo, es decir OYE y HABLA él mismo. No hace falta faster-whisper
(STT) ni piper (TTS): una sola pieza.

Protocolo: WebSocket persistente (BidiGenerateContent).
  wss://generativelanguage.googleapis.com/ws/...BidiGenerateContent?key=API_KEY

- Entrada de audio: PCM 16 bit, 16 kHz, little-endian (crudo).
- Salida de audio : PCM 16 bit, 24 kHz, little-endian (crudo).
- Voces predefinidas: Kore (femenina), Puck, Charon, Fenrir... (masculinas).
- El modelo cambia de idioma solo (multilingual switching): se le pide que
  responda en el idioma del usuario.

Uso:
    python3 -m ui.voice.gemini_live --test "Di hola"     # prueba sin micro
    python3 -m ui.voice.gemini_live --listen             # micro + altavoces
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import wave
from pathlib import Path

logger = logging.getLogger("agentos.voice.gemini")

WS_URL = ("wss://generativelanguage.googleapis.com/ws/"
          "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")
DEFAULT_MODEL = os.environ.get("AGENTOS_LIVE_MODEL", "gemini-3.1-flash-live-preview")
DEFAULT_VOICE = os.environ.get("AGENTOS_LIVE_VOICE", "Leda")   # Leda = femenina, juvenil
# Idioma de SALIDA (BCP-47). Es lo que fija el ACENTO. VACIO = automatico: el
# modelo sigue el idioma del usuario (asi el SO sirve en cualquier pais). Solo
# se rellena si algun dia se quiere FORZAR un acento concreto (--lang es-ES).
DEFAULT_LANG = os.environ.get("AGENTOS_LIVE_LANG", "")
IN_RATE = 16000     # micro
OUT_RATE = 24000    # respuesta

SYSTEM_INSTRUCTION = (
    "Eres AgentD, el administrador autonomo de AgentOS, un sistema operativo "
    "conversacional. Hablas con el usuario por voz.\n"
    "IDIOMA: responde SIEMPRE en el idioma en el que te hable el usuario. Si te "
    "habla en espanol, contesta en ESPANOL DE ESPANA (castellano): acento, "
    "pronunciacion y expresiones de Espana, nunca latinoamericanas. Si te habla "
    "en ingles, en ingles; y asi con cualquier idioma.\n"
    "ESTILO: breve, natural, calido y directo; son respuestas habladas para el "
    "oido, no un documento. No uses markdown, listas con asteriscos ni codigo.\n"
    "ACCIONES: TIENES MANOS. Cuando el usuario te pida hacer algo en el sistema "
    "(abrir una aplicacion o el navegador, mirar o cambiar algo, instalar, "
    "comprobar la red, el disco, los procesos, hacer un diagnostico...), llama a "
    "la funcion run_in_system con la peticion tal cual. NO digas que no puedes: "
    "puedes. Cuando la funcion te devuelva el resultado, cuentaselo hablando."
)

# Una SOLA funcion: el resto de herramientas (bash_exec, file_ops, diag...) las
# tiene AgentD en el sistema. Asi la voz no duplica cada tool en el formato de
# Google: pide "haz esto" y AgentD lo ejecuta con su registro completo.
TOOLS = [{
    "functionDeclarations": [{
        "name": "run_in_system",
        "description": (
            "Execute an action on the AgentOS system: open an application or the browser, "
            "inspect or change the network, disks, processes, files or services, install "
            "packages, run commands, or collect a diagnostic. Use it whenever the user asks "
            "you to DO something on the machine."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "request": {
                    "type": "STRING",
                    "description": "The action to perform, in natural language and in the user's language.",
                },
            },
            "required": ["request"],
        },
    }],
}]


def _clave() -> str:
    k = os.environ.get("AGENTOS_GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if k:
        return k
    for p in ("/etc/default/agentos", str(Path.home() / ".agentos" / "gemini.key")):
        try:
            for line in open(p):
                if "GEMINI_API_KEY" in line or "GOOGLE_API_KEY" in line:
                    return line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            continue
    raise SystemExit("falta la clave de Gemini (AGENTOS_GEMINI_API_KEY)")


class GeminiLive:
    """Sesion de voz contra la Live API."""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 voice: str = DEFAULT_VOICE, instrucciones: str = SYSTEM_INSTRUCTION,
                 lang: str = DEFAULT_LANG, on_tool_call=None):
        self.key = api_key or _clave()
        self.model = model
        self.voice = voice
        self.lang = lang
        self.instrucciones = instrucciones
        self.ws = None
        # Se llama cuando el modelo pide ejecutar algo en el sistema. Recibe la
        # lista de functionCalls; el que la atiende responde con responder_tool().
        self.on_tool_call = on_tool_call

    async def conectar(self):
        import websockets
        self.ws = await websockets.connect(f"{WS_URL}?key={self.key}", max_size=None)
        speech = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}}
        if self.lang:
            speech["languageCode"] = self.lang      # fija el ACENTO (p.ej. es-ES)
        await self.ws.send(json.dumps({"setup": {
            "model": f"models/{self.model}",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": speech,
            },
            "systemInstruction": {"parts": [{"text": self.instrucciones}]},
            "tools": TOOLS,
        }}))
        # el servidor confirma con setupComplete
        for _ in range(10):
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=30))
            if "setupComplete" in msg:
                logger.info("sesion Gemini Live lista", extra={"model": self.model, "voice": self.voice})
                return True
        raise RuntimeError("el servidor no confirmo el setup")

    async def texto(self, texto: str) -> None:
        await self.ws.send(json.dumps({"clientContent": {
            "turns": [{"role": "user", "parts": [{"text": texto}]}],
            "turnComplete": True}}))

    async def responder_tool(self, llamadas: list, resultado: str) -> None:
        """Devuelve al modelo el resultado de ejecutar run_in_system.

        `llamadas` es la lista de functionCalls recibida en el toolCall; se
        responde a cada una con su id (obligatorio) y el texto del resultado.
        El resultado se recorta: al oido no le hace falta un log de 4000 lineas.
        """
        respuestas = []
        for fc in llamadas:
            respuestas.append({
                "id": fc.get("id"),
                "name": fc.get("name", "run_in_system"),
                "response": {"result": (resultado or "")[:4000]},
            })
        await self.ws.send(json.dumps({"toolResponse": {"functionResponses": respuestas}}))

    async def audio(self, pcm: bytes) -> None:
        await self.ws.send(json.dumps({"realtimeInput": {"audio": {
            "data": base64.b64encode(pcm).decode(),
            "mimeType": f"audio/pcm;rate={IN_RATE}"}}}))

    async def frases(self):
        """Generador: cede (texto_transcrito, audio_pcm)."""
        async for raw in self.ws:
            msg = json.loads(raw)
            # El modelo pide EJECUTAR algo en el sistema (function calling).
            tc = msg.get("toolCall")
            if tc and self.on_tool_call:
                try:
                    await self.on_tool_call(tc.get("functionCalls") or [])
                except Exception as exc:
                    logger.warning("fallo atendiendo la tool call: %s", exc)
                continue
            sc = msg.get("serverContent") or {}
            if sc.get("interrupted"):
                yield ("[interrumpido]", b"")
            for part in ((sc.get("modelTurn") or {}).get("parts") or []):
                inline = part.get("inlineData") or {}
                if inline.get("data"):
                    yield ("", base64.b64decode(inline["data"]))
                elif part.get("text"):
                    yield (part["text"], b"")
            tr = (sc.get("outputTranscription") or {}).get("text")
            if tr:
                yield (tr, b"")
            if sc.get("turnComplete"):
                yield ("[fin]", b"")
                return

    async def cerrar(self):
        if self.ws:
            await self.ws.close()


def _guardar_wav(pcm: bytes, ruta: str, rate: int = OUT_RATE) -> str:
    with wave.open(ruta, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return ruta


async def _test(texto: str, salida: str, voice: str | None = None,
                  lang: str | None = None) -> int:
    loop = asyncio.get_running_loop()
    g = GeminiLive(voice=voice or DEFAULT_VOICE, lang=lang if lang is not None else DEFAULT_LANG)
    print(f"conectando al modelo {g.model} (voz {g.voice})...")
    await g.conectar()
    print("setup OK -> enviando:", texto)
    await g.texto(texto)
    trozos, audio = [], bytearray()
    async for t, a in g.frases():
        if a:
            audio += a
        if t and t not in ("[fin]", "[interrumpido]"):
            trozos.append(t)
        if t:
            print("   transcript:", t)
    await g.cerrar()
    transcripcion = "".join(trozos).strip()
    print("RESPUESTA:", transcripcion or "(sin transcripcion)")
    print(f"AUDIO: {len(audio)} bytes PCM @ {OUT_RATE}Hz")
    if audio:
        # wav + reproduccion con el primero de los reproductores disponibles
        import shutil, subprocess
        wav = _guardar_wav(bytes(audio), salida)
        print("WAV:", wav)
        for reproductor in (["pw-play"], ["paplay"], ["aplay", "-q"]):
            if shutil.which(reproductor[0]):
                await loop.run_in_executor(None, lambda: subprocess.run(
                    reproductor + [wav], capture_output=True))
                print("reproducido con", reproductor[0])
                break
    return 0 if transcripcion else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Voz de AgentOS con Gemini Live")
    ap.add_argument("--test", metavar="TEXTO", help="enviar texto y leer la respuesta")
    ap.add_argument("--out", default="/tmp/agentos_voz.wav")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--lang", default=DEFAULT_LANG, help="idioma de salida (es-ES, en-US...)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if a.test:
        return asyncio.run(_test(a.test, a.out, a.voice, a.lang))
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
