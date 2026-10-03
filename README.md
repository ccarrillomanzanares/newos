# AgentOS

Sistema operativo conversacional sobre el kernel Linux (Buildroot, x86-64). Un agente (AgentD) con
LLaMA 3.1 8B (GGUF vía llama-cpp-python) interpreta lenguaje natural y ejecuta herramientas del
sistema (bash, ficheros, procesos, paquetes, apps, monitor) con niveles de riesgo y log de auditoría.

## Inicio rápido (modo dev en Ubuntu)
```bash
make install-deps      # Python 3.11+, llama-cpp-python (CUDA si hay GPU), etc.
pip install -r requirements.txt   # solo núcleo (Ollama Cloud); modelo local: pip install -r requirements-local.txt
make dev-cloud         # contra Ollama Cloud (export OLLAMA_API_KEY=...)
make download-model    # models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf
make run               # (= make dev-agent) AgentD + chat TUI con LLaMA real
python3 -m ui.gui.launch  # interfaz gráfica (http://localhost:8080)
make test
```
Sin GGUF, AgentD intenta un servidor OpenAI-compatible (Ollama en 127.0.0.1:11434).

## Imagen del SO
`make build` (o `make iso`; Buildroot 2024.11.1, BR2_EXTERNAL = este repo) genera en `build/output/images/`:
`agentos.img` (USB: `sudo dd if=agentos.img of=/dev/sdX bs=4M conv=fsync`, BIOS + UEFI) y `agentos.iso` (DVD/VM).
Desde el sistema live, `agentos-install` instala en el disco interno (GPT, BIOS + UEFI; borra el disco).
Prueba en QEMU: `make run-qemu` (imagen USB) o `make run-qemu-iso`. Ficheros de arranque en `board/agentos/`.

## Variables de entorno (prefijo AGENTOS_)
MODE (dev|os), DATA_DIR, SOCKET, LLM_BACKEND (auto|llama_cpp|openai), MODEL_PATH, N_CTX,
N_GPU_LAYERS, TEMPERATURE, MAX_TOKENS, OPENAI_BASE_URL, OPENAI_MODEL, MAX_ITERATIONS,
AUTO_CONFIRM, BASH_TIMEOUT, LOG_LEVEL, LOG_FORMAT (text|json). Ver `core/agent/config.py`.

## Estructura
- `core/agent` — loop ReAct, herramientas, memoria (SQLite episódica + de trabajo), auditoría JSONL
- `core/llm` — backends (llama.cpp, OpenAI-compat), tool calling `<tool>{...}</tool>` + GBNF
- `core/ipc` — servidor de socket UNIX (NDJSON)
- `ui/tui`, `ui/voice` — chat de terminal, STT (faster-whisper) y TTS (piper)
- `training/` — recogida de datos y fine-tuning QLoRA
- `build/` — defconfig de Buildroot, fragmento de kernel, overlay con init script
