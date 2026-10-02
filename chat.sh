#!/usr/bin/env bash
# AgentOS – lanzador de chat interactivo.
# Uso:
#   ./chat.sh                          → modelo local (busca .gguf automáticamente)
#   OLLAMA_API_KEY=xxx ./chat.sh       → Ollama Cloud (no necesita modelo local)
#   OLLAMA_MODEL=llama3.1:8b ./chat.sh → elige el modelo cloud
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}

# ── 1. Elegir backend ─────────────────────────────────────────────────────────
if [[ -n "${OLLAMA_API_KEY:-}" ]]; then
    MODEL_CLOUD="${OLLAMA_MODEL:-llama3.1:8b}"
    echo "[AgentOS] Usando Ollama Cloud → modelo: $MODEL_CLOUD"
    export AGENTOS_LLM_BACKEND=openai
    export AGENTOS_OPENAI_BASE_URL=https://ollama.com/v1
    export AGENTOS_OPENAI_API_KEY="$OLLAMA_API_KEY"
    export AGENTOS_OPENAI_MODEL="$MODEL_CLOUD"
else
    # Buscar un .gguf: primero el que indica AGENTOS_MODEL_PATH, luego models/, luego ruta conocida del sandbox
    MODEL_PATH="${AGENTOS_MODEL_PATH:-}"
    if [[ -z "$MODEL_PATH" ]]; then
        # Buscar en models/ del proyecto
        FOUND=$(find models -maxdepth 1 -name '*.gguf' 2>/dev/null | head -1)
        if [[ -n "$FOUND" ]]; then
            MODEL_PATH="$FOUND"
        # Ruta del modelo descargado en este sandbox
        elif [[ -f "/home/ubuntu/test_models/qwen0.5b.gguf" ]]; then
            MODEL_PATH="/home/ubuntu/test_models/qwen0.5b.gguf"
        fi
    fi

    if [[ -z "$MODEL_PATH" ]]; then
        echo "ERROR: no se encontró ningún modelo .gguf."
        echo "  Descarga uno con:  make download-model"
        echo "  O usa Ollama Cloud: OLLAMA_API_KEY=tu_clave ./chat.sh"
        exit 1
    fi

    echo "[AgentOS] Usando modelo local → $MODEL_PATH"
    export AGENTOS_LLM_BACKEND=llama_cpp
    export AGENTOS_MODEL_PATH="$MODEL_PATH"
    export AGENTOS_N_CTX="${AGENTOS_N_CTX:-4096}"
fi

# ── 2. Directorio de datos ────────────────────────────────────────────────────
export AGENTOS_DATA_DIR="${AGENTOS_DATA_DIR:-$HOME/.agentos}"
mkdir -p "$AGENTOS_DATA_DIR"

# ── 3. Arrancar el chat ───────────────────────────────────────────────────────
exec "$PY" -m ui.tui.chat --direct
