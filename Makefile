# AgentOS — Makefile principal
PY ?= python3
BR_VERSION ?= 2024.02.6
BR_DIR := build/buildroot
OUT := $(BR_DIR)/output/images

.PHONY: install-deps download-model dev-agent agentd chat test build run-qemu clean

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

# Imagen completa con Buildroot (BR2_EXTERNAL = raíz del proyecto)
build:
	[ -d $(BR_DIR) ] || git clone --depth 1 -b $(BR_VERSION) https://gitlab.com/buildroot.org/buildroot.git $(BR_DIR)
	mkdir -p build/overlays/opt/agentos
	rsync -a --delete --exclude build --exclude models --exclude .git --exclude training ./ build/overlays/opt/agentos/
	$(MAKE) -C $(BR_DIR) BR2_EXTERNAL=$(CURDIR) BR2_DEFCONFIG=$(CURDIR)/build/configs/agentos_defconfig defconfig
	$(MAKE) -C $(BR_DIR)

run-qemu:
	qemu-system-x86_64 -enable-kvm -cpu host -m 12G -smp 4 \
	  -kernel $(OUT)/bzImage -drive file=$(OUT)/rootfs.ext4,if=virtio,format=raw \
	  -append "root=/dev/vda console=ttyS0" -nographic -nic user,hostfwd=tcp::2222-:22

clean:
	rm -rf build/overlays/opt/agentos
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
