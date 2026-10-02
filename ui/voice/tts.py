"""Text-to-Speech local con Piper.

Usa la API Python de ``piper-tts`` si está instalada (compatible con las
versiones 1.2 y 1.3+) y, si no, el binario ``piper``. El audio se reproduce
con pw-play, paplay o aplay (el primero disponible).

Modelos de voz: https://huggingface.co/rhasspy/piper-voices
(p. ej. es_ES-davefx-medium.onnx + .onnx.json)

Uso:  python -m ui.voice.tts "Hola, soy AgentOS" [--out salida.wav]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

logger = logging.getLogger("agentos.voice.tts")

DEFAULT_VOICE = Path(os.environ.get(
    "AGENTOS_PIPER_VOICE",
    str(Path(__file__).resolve().parents[2] / "models" / "piper" / "es_ES-davefx-medium.onnx"),
))
PLAYERS = (["pw-play"], ["paplay"], ["aplay", "-q"])


def clean_for_speech(text: str) -> str:
    """Elimina markdown y bloques de código que no tiene sentido leer en voz alta."""
    text = re.sub(r"```.*?```", " (bloque de código omitido) ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"[*_#>|]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class TextToSpeech:
    """Sintetiza texto a WAV y lo reproduce."""

    def __init__(self, voice_path: str | Path = DEFAULT_VOICE) -> None:
        self.voice_path = Path(voice_path)
        self._voice = None
        try:
            from piper import PiperVoice  # type: ignore[import-not-found]

            if self.voice_path.is_file():
                self._voice = PiperVoice.load(str(self.voice_path))
        except ImportError:
            logger.debug("piper-tts (Python) no disponible; se intentará el binario 'piper'")
        if self._voice is None and not shutil.which("piper"):
            raise RuntimeError(
                f"Piper no disponible. Instala 'pip install piper-tts' y descarga una voz en {self.voice_path}")
        if not self.voice_path.is_file():
            raise RuntimeError(f"No existe el modelo de voz: {self.voice_path} (ver scripts/download_model.sh)")

    def synthesize(self, text: str, out_path: str | Path) -> Path:
        """Genera un fichero WAV con la voz sintetizada."""
        out = Path(out_path)
        text = clean_for_speech(text)
        if self._voice is not None:
            with wave.open(str(out), "wb") as wav:
                if hasattr(self._voice, "synthesize_wav"):  # piper-tts >= 1.3
                    self._voice.synthesize_wav(text, wav)
                else:  # piper-tts 1.2
                    self._voice.synthesize(text, wav)
        else:
            subprocess.run(["piper", "--model", str(self.voice_path), "--output_file", str(out)],
                           input=text.encode("utf-8"), check=True, capture_output=True, timeout=120)
        return out

    @staticmethod
    def play(wav_path: str | Path) -> bool:
        for player in PLAYERS:
            if shutil.which(player[0]):
                result = subprocess.run([*player, str(wav_path)], capture_output=True, timeout=300)
                return result.returncode == 0
        logger.warning("No hay reproductor de audio (pw-play/paplay/aplay)")
        return False

    def speak(self, text: str) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            path = Path(tmp.name)
        try:
            self.play(self.synthesize(text, path))
        finally:
            path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TTS de AgentOS (Piper)")
    parser.add_argument("text")
    parser.add_argument("--voice", default=str(DEFAULT_VOICE))
    parser.add_argument("--out", help="Guardar en WAV en lugar de reproducir")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    tts = TextToSpeech(args.voice)
    if args.out:
        print(tts.synthesize(args.text, args.out))
    else:
        tts.speak(args.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
