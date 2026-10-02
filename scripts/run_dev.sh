#!/usr/bin/env bash
# Modo desarrollo: AgentD en segundo plano + TUI en primer plano.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
export AGENTOS_MODE=${AGENTOS_MODE:-dev}
RUN_DIR="${AGENTOS_DATA_DIR:-$HOME/.agentos}/run"
SOCK="${AGENTOS_SOCKET:-$RUN_DIR/agentos.sock}"
PIDFILE="$RUN_DIR/agentd.pid"
MODEL="${AGENTOS_MODEL_PATH:-models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf}"
mkdir -p "$RUN_DIR"
if [[ ! -f "$MODEL" ]]; then
  echo "[dev] AVISO: no existe $MODEL -> se usará Ollama (OpenAI-compat) o fallará; usa AGENTOS_LLM_BACKEND=mock para probar"
fi
cleanup() {
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill -TERM "$(cat "$PIDFILE")" 2>/dev/null || true; sleep 1
    kill -KILL "$(cat "$PIDFILE")" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
}
trap cleanup EXIT INT TERM
"$PY" -m core.ipc.socket_server --socket "$SOCK" --pidfile "$PIDFILE" > "$RUN_DIR/agentd.log" 2>&1 &
for _ in $(seq 1 600); do
  [[ -S "$SOCK" ]] && break
  kill -0 $! 2>/dev/null || { echo "[dev] AgentD terminó; ver $RUN_DIR/agentd.log"; tail -20 "$RUN_DIR/agentd.log"; exit 1; }
  sleep 0.5
done
"$PY" -m ui.tui.chat --socket "$SOCK"
