#!/bin/sh
# AgentOS — asistente de primer arranque (BusyBox sh / POSIX).
# Escribe /etc/default/agentos y marca /etc/agentos/.configured.
# Relanzar manualmente: agentos-config

CONF_DIR=/etc/agentos
MARKER=$CONF_DIR/.configured
DEFAULTS=/etc/default/agentos
MODELS_DIR=/data/models

[ -f "$MARKER" ] && exit 0

# Si no hay terminal (p. ej. arranque sin consola), usar tty1 o /dev/console
if [ ! -t 0 ]; then
    for t in /dev/tty1 /dev/console; do
        [ -c "$t" ] && { exec <"$t" >"$t" 2>&1; break; }
    done
fi

# Entrada oculta con stty -echo (equivale a read -s; funciona en BusyBox sh)
trap 'stty echo 2>/dev/null' EXIT
trap 'stty echo 2>/dev/null; echo; echo "Configuración cancelada."; exit 1' INT TERM

# Comillas simples seguras para el fichero de configuración
quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

clear 2>/dev/null || printf '\033[2J\033[H'
cat <<'EOF'
╔════════════════════════════════════════╗
║        AgentOS — Configuración        ║
╚════════════════════════════════════════╝

Selecciona el proveedor LLM:
  [1] Ollama Cloud (necesitas API key)
  [2] OpenAI (necesitas API key)
  [3] Modelo local (archivo .gguf en /data/models/)
EOF

while :; do
    printf 'Opción [1]: '
    read -r OPT
    OPT=${OPT:-1}
    case "$OPT" in 1|2|3) break ;; *) echo "Opción no válida." ;; esac
done

BACKEND=openai
BASE_URL=""; API_KEY=""; MODEL=""; MODEL_PATH=""
if [ "$OPT" = 3 ]; then
    BACKEND=llama_cpp
    echo
    echo "Modelos en $MODELS_DIR:"
    ls -1 "$MODELS_DIR"/*.gguf 2>/dev/null || echo "  (ninguno; copia un .gguf a $MODELS_DIR)"
    while :; do
        printf 'Ruta completa del modelo .gguf: '
        read -r MODEL_PATH
        [ -n "$MODEL_PATH" ] && break
    done
    [ -f "$MODEL_PATH" ] || echo "AVISO: $MODEL_PATH no existe todavía."
else
    if [ "$OPT" = 1 ]; then
        BASE_URL=https://ollama.com/v1; DEF_MODEL=deepseek-v4.1-flash; PROV="Ollama Cloud"
    else
        BASE_URL=https://api.openai.com/v1; DEF_MODEL=gpt-4o-mini; PROV="OpenAI"
    fi
    echo
    while :; do
        printf 'API key de %s: ' "$PROV"
        stty -echo 2>/dev/null
        read -r API_KEY
        stty echo 2>/dev/null
        echo
        [ -n "$API_KEY" ] && { echo "API key: ****"; break; }
        echo "La API key no puede estar vacía."
    done
    printf 'Modelo [%s]: ' "$DEF_MODEL"
    read -r MODEL
    MODEL=${MODEL:-$DEF_MODEL}
fi


# Escribir configuración
mkdir -p /etc/default "$CONF_DIR"
umask 077
{
    echo "# Generado por first-boot.sh — no editar a mano, usa agentos-config"
    echo "AGENTOS_LLM_BACKEND=$BACKEND"
    if [ "$BACKEND" = openai ]; then
        echo "AGENTOS_OPENAI_BASE_URL=$BASE_URL"
        echo "AGENTOS_OPENAI_API_KEY=$(quote "$API_KEY")"
        echo "AGENTOS_OPENAI_MODEL=$(quote "$MODEL")"
        echo "# AGENTOS_MODEL_PATH=/data/models/modelo.gguf   # solo para backend local"
    else
        echo "AGENTOS_MODEL_PATH=$(quote "$MODEL_PATH")"
    fi
} > "$DEFAULTS"
chmod 0600 "$DEFAULTS"
unset API_KEY
touch "$MARKER"

echo
echo "✔ Configuración guardada en $DEFAULTS"
echo "  Puedes cambiarla en cualquier momento con: agentos-config"
sleep 2
exit 0
