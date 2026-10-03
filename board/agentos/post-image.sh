#!/bin/sh
# AgentOS — post-image: genera agentos.img (USB, BIOS+UEFI) con genimage y
# deja el ISO de Buildroot como agentos.iso. Se ejecuta desde el árbol Buildroot.
set -e
BOARD_DIR="$(dirname "$(readlink -f "$0")")"
ROOT_DIR="$(readlink -f "$BOARD_DIR/../..")"
LIVE_PARTUUID="a6e705a1-02"   # firma de disco de build/genimage.cfg + partición 2

# Contenido de la partición EFI del USB (directorio aparte: efi-part/ lo usa
# también el ISO, cuya imagen FAT EFI es de solo 1 MB)
ESP="$BINARIES_DIR/esp-usb"
rm -rf "$ESP"
mkdir -p "$ESP/boot/grub"
cp -r "$BINARIES_DIR/efi-part/EFI" "$ESP/"
cp "$BINARIES_DIR/bzImage" "$ESP/boot/bzImage"
sed "s|@ROOT@|PARTUUID=$LIVE_PARTUUID|g" "$BOARD_DIR/grub.cfg" > "$ESP/boot/grub/grub.cfg"

support/scripts/genimage.sh -c "$ROOT_DIR/build/genimage.cfg"

if [ -f "$BINARIES_DIR/rootfs.iso9660" ]; then
    ln -sf rootfs.iso9660 "$BINARIES_DIR/agentos.iso"
fi

echo ""
echo "=== AgentOS: imágenes generadas en $BINARIES_DIR ==="
echo "  agentos.img  -> USB:  sudo dd if=agentos.img of=/dev/sdX bs=4M status=progress conv=fsync"
echo "  agentos.iso  -> DVD o máquina virtual"
echo "  Para instalar en el disco interno, arranca el live y ejecuta: agentos-install"
