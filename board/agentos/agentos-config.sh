#!/bin/sh
# AgentOS — relanza el asistente de configuración en cualquier momento.
# Uso (como root):  agentos-config
WIZARD=/opt/agentos/board/agentos/first-boot.sh
MARKER=/etc/agentos/.configured

if [ "$(id -u)" -ne 0 ]; then
    echo "agentos-config necesita permisos de root: sudo agentos-config"
    exit 1
fi
[ -x "$WIZARD" ] || WIZARD="$(dirname "$(readlink -f "$0")")/first-boot.sh"

rm -f "$MARKER"
sh "$WIZARD"
rc=$?
# Volver a marcar como configurado al terminar (aunque se cancele, si ya había configuración)
[ -f /etc/default/agentos ] && { mkdir -p /etc/agentos; touch "$MARKER"; }

if [ $rc -eq 0 ] && [ -x /etc/init.d/S99agentos ]; then
    printf '¿Reiniciar AgentD para aplicar los cambios? [S/n]: '
    read -r R
    case "$R" in n|N) echo "Aplica los cambios con: /etc/init.d/S99agentos restart" ;;
                 *) /etc/init.d/S99agentos restart ;; esac
fi
exit $rc
