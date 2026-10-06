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

# GRUB lee DOS rutas distintas segun el modo de arranque del firmware:
#   BIOS/Legacy -> /boot/grub/grub.cfg
#   UEFI        -> /EFI/BOOT/grub.cfg   (el EFI/ de Buildroot trae el suyo)
# La copia de $BINARIES_DIR/efi-part/EFI trae el grub.cfg POR DEFECTO de
# Buildroot, que hace "linux /boot/bzImage root=/dev/sda1": la particion 1 es la
# FAT de la EFI, NO la raiz (que esta en la particion 2). Con eso el kernel no
# encuentra init, no monta nada y se queda en PANTALLA NEGRA SIN NINGUN MENSAJE
# (callan quiet y console=tty1). Sintoma verificado: el .img arrancaba en BIOS y
# daba pantalla negra en UEFI. Hay que escribir el nuestro en LAS DOS rutas.
cp "$ESP/boot/grub/grub.cfg" "$ESP/EFI/BOOT/grub.cfg"


support/scripts/genimage.sh -c "$ROOT_DIR/build/genimage.cfg"

# --- ISO HIBRIDA (BIOS + UEFI) con grub-mkrescue ------------------------------
# Un solo artefacto: live en RAM y medio de instalacion. Arranca de USB y de CD,
# en BIOS y en UEFI. Se usa el grub-mkrescue del SISTEMA (embebe El Torito + MBR
# hibrido); el de Buildroot genera solo MBR y NO arranca como CD (VirtualBox).
ISO_TREE="$(mktemp -d)"
mkdir -p "$ISO_TREE/boot/grub"
# xorriso no acepta un symlink como destino (Buildroot deja agentos.iso -> rootfs.iso9660).
rm -f "$BINARIES_DIR/agentos.iso"
cp "$BINARIES_DIR/bzImage" "$ISO_TREE/boot/bzImage"
cp "$BINARIES_DIR/rootfs.cpio.gz" "$ISO_TREE/boot/initrd"
sed -e "s|__KERNEL_PATH__|/boot/bzImage|g" -e "s|__INITRD_PATH__|/boot/initrd|g" "$BOARD_DIR/grub-iso.cfg" > "$ISO_TREE/boot/grub/grub.cfg"
GRUB_MKRESCUE=/usr/bin/grub-mkrescue
[ -x "$GRUB_MKRESCUE" ] || GRUB_MKRESCUE=grub-mkrescue
PATH=/usr/bin:/bin:/usr/sbin:/sbin "$GRUB_MKRESCUE" -o "$BINARIES_DIR/agentos.iso" "$ISO_TREE" -- -volid AGENTOS || echo "AVISO: no se pudo generar agentos.iso"
rm -rf "$ISO_TREE"
echo ""
echo "=== AgentOS: imágenes generadas en $BINARIES_DIR ==="
echo "  agentos.img  -> USB:  sudo dd if=agentos.img of=/dev/sdX bs=4M status=progress conv=fsync"
echo "  agentos.iso  -> DVD o máquina virtual"
echo "  Para instalar en el disco interno, arranca el live y ejecuta: agentos-install"
