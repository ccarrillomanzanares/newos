# AgentOS

Sistema operativo conversacional sobre el kernel Linux (Buildroot, x86-64). Un agente (AgentD) con
LLaMA 3.1 8B (GGUF vía llama-cpp-python) interpreta lenguaje natural y ejecuta herramientas del
sistema (bash, ficheros, procesos, paquetes, apps, monitor) con niveles de riesgo y log de auditoría.

## Inicio rápido (modo dev en Ubuntu)
```bash
make install-deps      # Python 3.11+, llama-cpp-python (CUDA si hay GPU), etc.
make dev               # prueba inmediata con LLM simulado (sin modelo)
make dev-cloud         # contra Ollama Cloud (export OLLAMA_API_KEY=...)
make download-model    # models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf
make run               # (= make dev-agent) AgentD + chat TUI con LLaMA real
AGENTOS_LLM_BACKEND=mock python3 -m ui.tui.chat --direct   # prueba sin modelo
make test
```
Sin GGUF, AgentD intenta un servidor OpenAI-compatible (Ollama en 127.0.0.1:11434).

## Imagen del SO
`make build` (Buildroot 2024.02, BR2_EXTERNAL = este repo) y `make run-qemu`.

## Variables de entorno (prefijo AGENTOS_)
MODE (dev|os), DATA_DIR, SOCKET, LLM_BACKEND (auto|llama_cpp|openai|mock), MODEL_PATH, N_CTX,
N_GPU_LAYERS, TEMPERATURE, MAX_TOKENS, OPENAI_BASE_URL, OPENAI_MODEL, MAX_ITERATIONS,
AUTO_CONFIRM, BASH_TIMEOUT, LOG_LEVEL, LOG_FORMAT (text|json). Ver `core/agent/config.py`.

## Estructura
- `core/agent` — loop ReAct, herramientas, memoria (SQLite episódica + de trabajo), auditoría JSONL
- `core/llm` — backends (llama.cpp, OpenAI-compat, mock), tool calling `<tool>{...}</tool>` + GBNF
- `core/ipc` — servidor de socket UNIX (NDJSON)
- `ui/tui`, `ui/voice` — chat de terminal, STT (faster-whisper) y TTS (piper)
- `training/` — recogida de datos y fine-tuning QLoRA
- `build/` — defconfig de Buildroot, fragmento de kernel, overlay con init script
