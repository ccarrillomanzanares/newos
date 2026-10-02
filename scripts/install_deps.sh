#!/usr/bin/env bash
# Instala dependencias de AgentOS (modo dev, Ubuntu/Debian).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
"$PY" -c 'import sys; assert sys.version_info >= (3,11), "Se requiere Python 3.11+"'
"$PY" -m pip install --upgrade pip
# llama-cpp-python: CUDA si hay GPU NVIDIA, si no rueda precompilada CPU
if command -v nvidia-smi >/dev/null 2>&1; then
  echo "[install] GPU NVIDIA detectada -> compilando con CUDA"
  CMAKE_ARGS="-DGGML_CUDA=on" "$PY" -m pip install --no-cache-dir llama-cpp-python
else
  "$PY" -m pip install llama-cpp-python --prefer-binary \
    --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
fi
"$PY" -m pip install psutil colorama aiofiles httpx sqlalchemy pytest pytest-asyncio
if [[ "${WITH_VOICE:-1}" == "1" ]]; then
  "$PY" -m pip install faster-whisper piper-tts sounddevice numpy || echo "[install] AVISO: extras de voz no instalados"
fi
echo "[install] OK"
