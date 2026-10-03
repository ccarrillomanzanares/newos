#!/bin/bash
# =============================================================================
# agentos-install — instala AgentOS en un disco interno (BORRA EL DISCO).
#
# Esquema GPT válido para UEFI y BIOS:
#   1: bios_grub  1 MiB   (EF02)  núcleo GRUB para arranque BIOS
#   2: EFI        512 MiB (EF00)  FAT32: EFI/BOOT/bootx64.efi, boot/bzImage, boot/grub
#   3: raíz       resto   (8304)  ext4 "agentos-root"
#
# Uso: agentos-install            (interactivo, como root)
# =============================================================================
set -euo pipefail

MNT=/mnt/agentos-install
GRUB_TEMPLATE=/usr/share/agentos/grub.cfg

die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }

cleanup() {
    set +e
    for m in "$MNT/esp" "$MNT/root"; do
        mountpoint -q "$m" 2>/dev/null && umount "$m"
    done
}
trap cleanup EXIT

[ "$(id -u)" -eq 0 ] || die "ejecuta este comando como root"
for cmd in lsblk sgdisk mkfs.vfat mkfs.ext4 blkid grub-install; do
    command -v "$cmd" >/dev/null || die "falta la herramienta '$cmd'"
done
[ -f "$GRUB_TEMPLATE" ] || die "falta $GRUB_TEMPLATE"
[ -f /boot/bzImage ] || die "falta el kernel /boot/bzImage"

# --- Discos que NO se pueden elegir: el medio live -----------------------------
live_disks=()
root_src="$(findmnt -n -o SOURCE / 2>/dev/null || true)"
if [ -b "$root_src" ]; then
    parent="$(lsblk -n -d -o PKNAME "$root_src" 2>/dev/null || true)"
    live_disks+=("/dev/${parent:-$(basename "$root_src")}")
fi
while read -r name fstype; do
    [ "${fstype:-}" = "iso9660" ] && live_disks+=("$name")
done < <(lsblk -d -n -p -o NAME,FSTYPE 2>/dev/null)

is_live() {
    local d
    for d in "${live_disks[@]}"; do [ "$d" = "$1" ] && return 0; done
    return 1
}

# --- Elegir disco ----------------------------------------------------------------
echo ""
echo "Discos disponibles:"
echo ""
candidates=()
while read -r name size type tran model; do
    [ "$type" = "disk" ] || continue
    case "$name" in /dev/loop*|/dev/sr*|/dev/zram*|/dev/ram*) continue ;; esac
    if is_live "$name"; then
        printf "   -  %-14s %8s  %-5s %s   (medio de instalación, no seleccionable)\n" "$name" "$size" "${tran:-}" "${model:-}"
        continue
    fi
    candidates+=("$name")
    printf "  %2d) %-14s %8s  %-5s %s\n" "${#candidates[@]}" "$name" "$size" "${tran:-}" "${model:-}"
done < <(lsblk -d -n -p -o NAME,SIZE,TYPE,TRAN,MODEL)

[ "${#candidates[@]}" -gt 0 ] || die "no hay ningún disco de destino disponible (añade un disco virtual o conecta un disco distinto del medio de instalación)"
echo ""
read -r -p "Número del disco donde instalar AgentOS (o 'q' para salir): " choice
[ "$choice" = "q" ] && { echo "Cancelado."; exit 0; }
[[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#candidates[@]}" ] \
    || die "selección no válida"
DISK="${candidates[$((choice - 1))]}"
is_live "$DISK" && die "$DISK es el medio de instalación"

size_bytes="$(lsblk -b -d -n -o SIZE "$DISK")"
[ "$size_bytes" -ge $((8 * 1024 * 1024 * 1024)) ] || die "$DISK es demasiado pequeño (mínimo 8 GB)"

echo ""
echo "  ¡ATENCIÓN! Se BORRARÁ TODO el contenido de $DISK:"
lsblk -p -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT "$DISK" | sed 's/^/    /'
echo ""
read -r -p "Escribe BORRAR para confirmar: " confirm
[ "$confirm" = "BORRAR" ] || { echo "Cancelado. No se ha modificado nada."; exit 0; }

# Desmontar cualquier partición del disco de destino
for p in $(lsblk -n -p -o NAME "$DISK" | tail -n +2); do
    umount "$p" 2>/dev/null || true
done

# --- Particionado ----------------------------------------------------------------
info "Particionando $DISK (GPT)"
wipefs -a "$DISK" >/dev/null 2>&1 || true
sgdisk --zap-all "$DISK" >/dev/null
sgdisk -n 1:2048:+1M   -t 1:EF02 -c 1:bios_grub \
       -n 2:0:+512M    -t 2:EF00 -c 2:EFI \
       -n 3:0:0        -t 3:8304 -c 3:agentos-root "$DISK" >/dev/null
partprobe "$DISK" 2>/dev/null || blockdev --rereadpt "$DISK" 2>/dev/null || true
udevadm settle 2>/dev/null || sleep 2

case "$DISK" in
    *[0-9]) PSEP="p" ;;   # nvme0n1 -> nvme0n1p1, mmcblk0 -> mmcblk0p1
    *)      PSEP=""  ;;
esac
ESP_PART="${DISK}${PSEP}2"
ROOT_PART="${DISK}${PSEP}3"
for p in "$ESP_PART" "$ROOT_PART"; do
    for _ in 1 2 3 4 5 6 7 8 9 10; do [ -b "$p" ] && break; sleep 1; done
    [ -b "$p" ] || die "no aparece la partición $p"
done

info "Formateando"
mkfs.vfat -F 32 -n AGENTOS_EFI "$ESP_PART" >/dev/null
mkfs.ext4 -F -q -L agentos-root "$ROOT_PART"

mkdir -p "$MNT/root" "$MNT/esp"
mount "$ROOT_PART" "$MNT/root"
mount "$ESP_PART" "$MNT/esp"

# --- Copia del sistema -----------------------------------------------------------
info "Copiando el sistema (puede tardar unos minutos)"
EXCLUDES=(--exclude=/proc/* --exclude=/sys/* --exclude=/dev/* --exclude=/run/*
          --exclude=/tmp/* --exclude=/mnt/* --exclude=/media/* --exclude=/init)
if command -v rsync >/dev/null; then
    rsync -aHx --info=progress2 "${EXCLUDES[@]}" / "$MNT/root/"
else
    cp -ax / "$MNT/root/"
fi
mkdir -p "$MNT/root/proc" "$MNT/root/sys" "$MNT/root/dev" "$MNT/root/run" \
         "$MNT/root/tmp" "$MNT/root/mnt"
chmod 1777 "$MNT/root/tmp"

# --- Arranque: kernel + GRUB (UEFI y BIOS) ---------------------------------------
info "Instalando kernel y GRUB"
mkdir -p "$MNT/esp/boot/grub"
cp /boot/bzImage "$MNT/esp/boot/bzImage"

ROOT_PARTUUID="$(blkid -s PARTUUID -o value "$ROOT_PART")"
[ -n "$ROOT_PARTUUID" ] || die "no se pudo leer el PARTUUID de $ROOT_PART"
sed "s|@ROOT@|PARTUUID=$ROOT_PARTUUID|g" "$GRUB_TEMPLATE" > "$MNT/esp/boot/grub/grub.cfg"

# Directorio de módulos GRUB instalado por Buildroot (BR2_TARGET_GRUB2_INSTALL_TOOLS)
grub_dir() {
    local d
    for d in /lib/grub/"$1" /usr/lib/grub/"$1"; do
        [ -d "$d" ] && { echo "--directory=$d"; return; }
    done
}
# UEFI: ruta "removable" (EFI/BOOT/BOOTX64.EFI), no depende de variables NVRAM
grub-install $(grub_dir x86_64-efi) --target=x86_64-efi --efi-directory="$MNT/esp" \
    --boot-directory="$MNT/esp/boot" --removable --no-nvram
# BIOS: núcleo GRUB en la partición bios_grub
grub-install $(grub_dir i386-pc) --target=i386-pc --boot-directory="$MNT/esp/boot" "$DISK"

sync
cleanup
trap - EXIT

echo ""
echo "============================================================"
echo "  AgentOS instalado en $DISK"
echo "  Retira el USB/DVD y reinicia con: reboot"
echo "  Usuario 'agent' (contraseña: agent); root sin contraseña."
echo "  Cambia ambas con 'passwd' en cuanto arranques."
echo "============================================================"
