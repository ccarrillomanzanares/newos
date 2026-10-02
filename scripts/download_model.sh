#!/usr/bin/env bash
# Descarga LLaMA 3.1 8B Instruct Q4_K_M (GGUF) y opcionalmente la voz Piper es_ES.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p models/piper
FILE=Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf
URL="https://huggingface.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/$FILE"
if [[ ! -f "models/$FILE" ]]; then
  echo "[model] Descargando $FILE (~4.9 GB)..."
  curl -L --fail -C - ${HF_TOKEN:+-H "Authorization: Bearer $HF_TOKEN"} -o "models/$FILE.part" "$URL"
  mv "models/$FILE.part" "models/$FILE"
fi
if [[ "${WITH_VOICE:-0}" == "1" ]]; then
  V=https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_ES/davefx/medium
  for f in es_ES-davefx-medium.onnx es_ES-davefx-medium.onnx.json; do
    [[ -f models/piper/$f ]] || curl -L --fail -o "models/piper/$f" "$V/$f"
  done
fi
echo "[model] OK -> models/$FILE"
