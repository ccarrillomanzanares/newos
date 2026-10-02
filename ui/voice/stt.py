"""Speech-to-Text con faster-whisper y VAD por energía en streaming.

Flujo: micrófono (sounddevice, 16 kHz mono) -> VAD por energía que segmenta
las frases -> faster-whisper (con ``vad_filter`` Silero) -> texto -> AgentD.

Uso:
    python -m ui.voice.stt --file audio.wav           # transcribir un fichero
    python -m ui.voice.stt --listen                   # escuchar y mostrar texto
    python -m ui.voice.stt --listen --send [--speak]  # enviar a AgentD (y leer la respuesta con TTS)

Dependencias opcionales: faster-whisper, sounddevice, numpy.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import logging
import os
import queue
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("agentos.voice.stt")

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


@dataclass
class VADConfig:
    """Parámetros del detector de actividad de voz por energía."""

    threshold_multiplier: float = 3.0  # energía > ruido_de_fondo * multiplicador => voz
    min_threshold: float = 0.006  # RMS mínimo (audio float32 normalizado)
    silence_ms: int = 800  # silencio que cierra una frase
    min_speech_ms: int = 300  # frases más cortas se descartan
    max_speech_s: float = 20.0  # corte duro de una frase
    preroll_ms: int = 300  # audio previo al disparo que se conserva


class EnergyVAD:
    """VAD sencillo y sin dependencias extra: umbral adaptativo sobre RMS."""

    def __init__(self, cfg: VADConfig | None = None) -> None:
        self.cfg = cfg or VADConfig()
        self.noise_floor = 0.002

    def is_speech(self, frame: Any) -> bool:
        import numpy as np

        rms = float(np.sqrt(np.mean(np.square(frame)))) if len(frame) else 0.0
        threshold = max(self.noise_floor * self.cfg.threshold_multiplier, self.cfg.min_threshold)
        speech = rms > threshold
        if not speech:  # adaptar el ruido de fondo solo en silencio
            self.noise_floor = 0.95 * self.noise_floor + 0.05 * rms
        return speech

    def segment(self, frames: Iterator[Any]) -> Iterator[Any]:
        """Agrupa frames de audio en frases completas (np.ndarray float32)."""
        import numpy as np

        cfg = self.cfg
        preroll: collections.deque[Any] = collections.deque(maxlen=max(1, cfg.preroll_ms // FRAME_MS))
        speech: list[Any] = []
        silence_frames = 0
        in_speech = False
        max_frames = int(cfg.max_speech_s * 1000 / FRAME_MS)
        for frame in frames:
            voiced = self.is_speech(frame)
            if not in_speech:
                preroll.append(frame)
                if voiced:
                    in_speech, speech, silence_frames = True, list(preroll), 0
                continue
            speech.append(frame)
            silence_frames = 0 if voiced else silence_frames + 1
            if silence_frames * FRAME_MS >= cfg.silence_ms or len(speech) >= max_frames:
                voiced_ms = (len(speech) - silence_frames) * FRAME_MS
                if voiced_ms >= cfg.min_speech_ms:
                    yield np.concatenate(speech)
                in_speech, speech = False, []
                preroll.clear()


class SpeechToText:
    """Envoltorio de faster-whisper."""

    def __init__(self, model: str = "small", language: str | None = "es", device: str = "auto",
                 compute_type: str = "int8") -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("faster-whisper no está instalado: pip install faster-whisper") from exc
        logger.info("Cargando Whisper", extra={"model": model, "device": device})
        self.model = WhisperModel(model, device=device, compute_type=compute_type)
        self.language = language

    def transcribe(self, audio: Any) -> str:
        """Transcribe un np.ndarray float32 a 16 kHz o una ruta de fichero."""
        segments, _info = self.model.transcribe(
            audio, language=self.language, vad_filter=True, beam_size=1,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    def transcribe_file(self, path: str | Path) -> str:
        return self.transcribe(str(path))

    def listen(self, vad: EnergyVAD | None = None, device: int | str | None = None) -> Iterator[str]:
        """Escucha el micrófono indefinidamente y produce frases transcritas."""
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("sounddevice no está instalado: pip install sounddevice (y libportaudio2)") from exc

        vad = vad or EnergyVAD()
        frames: queue.Queue[Any] = queue.Queue()

        def callback(indata: Any, _frames: int, _time: Any, status: Any) -> None:
            if status:
                logger.debug("Estado de audio: %s", status)
            frames.put(indata[:, 0].copy())

        def frame_iter() -> Iterator[Any]:
            while True:
                yield frames.get()

        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=FRAME_SAMPLES,
                            device=device, callback=callback):
            logger.info("Escuchando micrófono…")
            for utterance in vad.segment(frame_iter()):
                text = self.transcribe(utterance)
                if text:
                    yield text


async def send_to_agent(text: str, socket_path: Path, speak: bool = False) -> str:
    """Envía el texto transcrito a AgentD y devuelve la respuesta final."""
    from core.ipc.socket_server import AgentClient

    client = AgentClient(socket_path)
    await client.connect()
    final = ""
    try:
        async for event in client.ask(text):
            if event.get("type") == "confirm_request":
                # La confirmación por voz se implementará en la fase 2; por seguridad se rechaza.
                logger.warning("Acción destructiva rechazada (confirmación por voz no implementada)")
                await client.confirm(event["id"], False)
            elif event.get("type") == "final":
                final = event.get("text", "")
    finally:
        await client.close()
    if speak and final:
        from .tts import TextToSpeech

        TextToSpeech().speak(final)
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="STT de AgentOS (faster-whisper)")
    parser.add_argument("--file", help="Transcribir un fichero de audio")
    parser.add_argument("--listen", action="store_true", help="Escuchar el micrófono")
    parser.add_argument("--send", action="store_true", help="Enviar las frases a AgentD")
    parser.add_argument("--speak", action="store_true", help="Leer la respuesta con TTS")
    parser.add_argument("--model", default=os.environ.get("AGENTOS_WHISPER_MODEL", "small"))
    parser.add_argument("--language", default=os.environ.get("AGENTOS_WHISPER_LANG", "es"))
    parser.add_argument("--device", default="auto", help="cpu | cuda | auto")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    stt = SpeechToText(args.model, args.language or None, args.device)
    if args.file:
        print(stt.transcribe_file(args.file))
        return 0
    if not args.listen:
        parser.print_help()
        return 1

    from core.agent.config import get_config

    socket_path = get_config().socket_path
    try:
        for text in stt.listen():
            print(f"🎤 {text}")
            if args.send:
                reply = asyncio.run(send_to_agent(text, socket_path, args.speak))
                print(f"🤖 {reply}")
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
