# AgentOS — Makefile principal
PY      ?= python3
J       ?= $(shell nproc)
BR_VERSION ?= 2024.11.1
BR_DIR  := build/buildroot
BR_OUT  := $(CURDIR)/build/output
OUT     := $(BR_OUT)/images
TARGET_DIR := $(BR_OUT)/target

.PHONY: install-deps download-model run gui dev-cloud dev-agent agentd chat test build iso run-qemu run-qemu-iso clean

# Con el modelo GGUF real (ejecutar antes: make download-model)
run: dev-agent

# Interfaz gráfica + AgentD (leer: export OLLAMA_API_KEY=...)
gui:
	AGENTOS_UI=gui ./scripts/run_dev.sh

# Ollama Cloud (necesita: export OLLAMA_API_KEY=...; modelo opcional: OLLAMA_MODEL)
dev-cloud:
	@test -n "$$OLLAMA_API_KEY" || { echo "Falta OLLAMA_API_KEY (export OLLAMA_API_KEY=tu_clave)"; exit 1; }
	AGENTOS_LLM_BACKEND=openai AGENTOS_OPENAI_BASE_URL=https://ollama.com/v1 \
	AGENTOS_OPENAI_API_KEY="$$OLLAMA_API_KEY" AGENTOS_OPENAI_MODEL="$${OLLAMA_MODEL:-deepseek-v4.1-flash}" \
	./scripts/run_dev.sh

install-deps:
	./scripts/install_deps.sh

download-model:
	./scripts/download_model.sh

dev-agent:
	./scripts/run_dev.sh

agentd:
	$(PY) -m core.ipc.socket_server

chat:
	$(PY) -m ui.tui.chat

test:
	$(PY) -m pytest -q tests

# Imagen completa con Buildroot (BR2_EXTERNAL = raíz del proyecto).
# Resultado: build/output/images/agentos.iso (DVD/VM) y agentos.img (USB, dd).
# Uso: make build                        → auto-detecta CPUs (nproc)
#      make build J=16                   → fuerza 16 jobs
#      HOSTCC=gcc-12 HOSTCXX=g++-12 make build J=16   → gcc-12 + 16 jobs (Ubuntu 24.04)
build:
	@echo ">>> Buildroot $(BR_VERSION) con -j$(J) HOSTCC=$(HOSTCC) HOSTCXX=$(HOSTCXX)"
	[ -d $(BR_DIR) ] || git clone --depth 1 -b $(BR_VERSION) https://gitlab.com/buildroot.org/buildroot.git $(BR_DIR)
	mkdir -p build/overlays/opt/agentos
	rsync -a --delete \
	  --exclude build --exclude board --exclude models \
	  --exclude .git  --exclude training \
	  ./ build/overlays/opt/agentos/
	# Los overlays se COPIAN encima del target de Buildroot, de forma aditiva: si
	# se borra un fichero del repo, se queda en la imagen para siempre. Se borra
	# antes la copia del target para que refleje exactamente el repositorio.
	rm -rf $(TARGET_DIR)/opt/agentos
	$(MAKE) -C $(BR_DIR) O=$(BR_OUT) \
	  BR2_EXTERNAL=$(CURDIR) \
	  BR2_DEFCONFIG=$(CURDIR)/build/configs/agentos_defconfig \
	  defconfig
	$(MAKE) -C $(BR_DIR) O=$(BR_OUT) -j$(J) \
	  HOSTCC="$(HOSTCC)" HOSTCXX="$(HOSTCXX)"
	@ls -lh $(OUT)/agentos.iso $(OUT)/agentos.img 2>/dev/null || ls -lh $(OUT)/

iso: build

# Probar la imagen en QEMU (BIOS). Para UEFI: -bios /usr/share/ovmf/OVMF.fd
run-qemu:
	qemu-system-x86_64 -enable-kvm -cpu host -m 4G -smp 4 \
	  -drive file=$(OUT)/agentos.img,format=raw,if=virtio \
	  -device virtio-vga -nic user,hostfwd=tcp::2222-:22

run-qemu-iso:
	qemu-system-x86_64 -enable-kvm -cpu host -m 4G -smp 4 \
	  -cdrom $(OUT)/agentos.iso -device virtio-vga -nic user,hostfwd=tcp::2222-:22

clean:
	rm -rf build/overlays/opt/agentos
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
