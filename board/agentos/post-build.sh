#!/bin/sh
# AgentOS — post-build: se ejecuta sobre $TARGET_DIR antes de crear las imágenes.
set -e
BOARD_DIR="$(dirname "$(readlink -f "$0")")"

# Instalador disponible como comando (no se ejecuta automáticamente)
install -D -m 0755 "$BOARD_DIR/install-to-disk.sh" "$TARGET_DIR/usr/bin/agentos-install"
# Plantilla del menú GRUB que usa el instalador (@ROOT@ -> PARTUUID del disco)
install -D -m 0644 "$BOARD_DIR/grub.cfg" "$TARGET_DIR/usr/share/agentos/grub.cfg"

# Asistente de primer arranque y agentos-config (board/ no se copia a /opt/agentos
# con el rsync del Makefile; /usr/bin/agentos-config es un enlace a esta ruta)
install -D -m 0755 "$BOARD_DIR/first-boot.sh" "$TARGET_DIR/opt/agentos/board/agentos/first-boot.sh"
install -D -m 0755 "$BOARD_DIR/agentos-config.sh" "$TARGET_DIR/opt/agentos/board/agentos/agentos-config.sh"

# Los scripts de arranque del overlay tienen que ser ejecutables: rcS los lanza
# con "$i start", y un fichero sin bit x falla con "Permission denied" y el
# servicio (Weston incluido) NO arranca — sin ningún error visible en pantalla.
chmod 0755 "$TARGET_DIR"/etc/init.d/S[0-9][0-9]* 2>/dev/null || true

# Los servicios escriben en /var/log/, pero en esta imagen /var/log es un
# tmpfs vacío (no hay syslogd al que apuntar): la carpeta debe existir antes
# de que arranque nada, o las redirecciones ">> /var/log/xxx.log" fallan.
mkdir -p "$TARGET_DIR/var/log"

# sudo: permisos estrictos (git no conserva 0440) e inclusión de /etc/sudoers.d
if [ -f "$TARGET_DIR/etc/sudoers.d/agent" ]; then
    chmod 0440 "$TARGET_DIR/etc/sudoers.d/agent"
    grep -qE '^[@#]includedir /etc/sudoers.d' "$TARGET_DIR/etc/sudoers" 2>/dev/null || \
        echo '@includedir /etc/sudoers.d' >> "$TARGET_DIR/etc/sudoers"
fi

# Consola de depuración por puerto serie (ttyS0). Con el kernel arrancado con
# console=ttyS0 el arranque entero se puede leer en texto desde el anfitrión
# (VirtualBox: --uart1 0x3F8 4 --uartmode1 file arranque.log). Sin esto, un
# fallo de arranque solo se puede diagnosticar a ciegas con capturas de pantalla.
ttyS0::respawn:/sbin/getty -L 115200 ttyS0 vt100

# Autologin en tty2 (consola de respaldo). BusyBox getty NO soporta -a,
# así que se usa /bin/login -f (entra sin contraseña).
if [ -f "$TARGET_DIR/etc/inittab" ]; then
    if grep -q '^tty2::respawn:' "$TARGET_DIR/etc/inittab"; then
        sed -i 's|^tty2::respawn:.*|tty2::respawn:/bin/login -f agent|' "$TARGET_DIR/etc/inittab"
    else
        echo 'tty2::respawn:/bin/login -f agent' >> "$TARGET_DIR/etc/inittab"
    fi
fi

# El grub.cfg genérico de Buildroot en el rootfs confundiría la búsqueda de
# /boot/grub/grub.cfg que hace la configuración embebida de GRUB.
rm -f "$TARGET_DIR/boot/grub/grub.cfg"

# boot.img (primer sector BIOS) para genimage
cp -f "$TARGET_DIR/lib/grub/i386-pc/boot.img" "$BINARIES_DIR/boot.img"
