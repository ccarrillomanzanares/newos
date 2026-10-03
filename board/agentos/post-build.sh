#!/bin/sh
# AgentOS — post-build: se ejecuta sobre $TARGET_DIR antes de crear las imágenes.
set -e
BOARD_DIR="$(dirname "$(readlink -f "$0")")"

# Instalador disponible como comando (no se ejecuta automáticamente)
install -D -m 0755 "$BOARD_DIR/install-to-disk.sh" "$TARGET_DIR/usr/bin/agentos-install"
# Plantilla del menú GRUB que usa el instalador (@ROOT@ -> PARTUUID del disco)
install -D -m 0644 "$BOARD_DIR/grub.cfg" "$TARGET_DIR/usr/share/agentos/grub.cfg"

# El grub.cfg genérico de Buildroot en el rootfs confundiría la búsqueda de
# /boot/grub/grub.cfg que hace la configuración embebida de GRUB.
rm -f "$TARGET_DIR/boot/grub/grub.cfg"

# boot.img (primer sector BIOS) para genimage
cp -f "$TARGET_DIR/lib/grub/i386-pc/boot.img" "$BINARIES_DIR/boot.img"
