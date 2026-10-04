# ADR-001 — Kernel genérico (agnóstico al hardware)

**Estado:** propuesto · **Fecha:** 2026-10-04 · **Autor:** Hermes (a petición de Carlos)

## Contexto: el SO debe arrancar en cualquier hardware

Un SO que solo arranca en el hardware donde se compiló no es un SO. Fedora, Ubuntu y
Arch consiguen arrancar en casi cualquier x86_64 porque **compilan su kernel con la
configuración completa que mantiene el propio kernel para x86_64**, no con una lista de
hardware escrita a mano. Nosotros hicimos lo segundo.

## Evidencia medida (kernel 6.11.11, comparado con Ubuntu 6.11 y Arch)

Comparado contra el `.config` real de nuestro kernel compilado, la config de **Ubuntu
6.11 (oracular)** — misma versión mayor que la nuestra, la referencia correcta — y la
de Arch. Medido con `cmp_kernels.py`:

| Métrica | AgentOS | **Ubuntu 6.11** ⁽¹⁾ | Arch | Factor vs Ubuntu |
|---|---|---|---|---|
| Opciones `=y` (built-in) | 1.613 | **3.115** | 2.850 | 1,9× |
| **Opciones `=m` (módulos)** | **67** | **6.359** | 5.704 | **95×** |
| Total de opciones activas | 1.680 | **9.474** | 8.554 | 5,6× |
| DRM (GPU) | 29 | 89 | 81 | 3,1× |
| SND (audio) | 44 | 706 | 629 | 16× |
| USB | 23 (**0 módulos**) | 387 (309 mód.) | 382 (310 mód.) | 17× |
| SATA/ATA/SCSI/NVMe/MMC | 30 (**0 módulos**) | 173 (125 mód.) | 172 (126 mód.) | 5,8× |
| Red (net) | 95 (**0 módulos**) | 252 (142 mód.) | 253 (148 mód.) | 2,7× |
| WiFi (802.11) | 48 (18 mód.) | 149 | 143 | 3,1× |
| Ethernet | 11 (**0 módulos**) | 35 (21 mód.) | 33 (21 mód.) | 3,2× |
| Filesystems | 15 | 60 | 53 | 4× |

**La lectura clave:** tenemos **67 módulos** donde Ubuntu tiene **6.359**. En
almacenamiento, USB y Ethernet tenemos **cero módulos** — todo compilado dentro. Eso
es lo que salva el arranque (el USB y el SATA funcionan, por eso el portátil arrancó),
pero deja el sistema **rígido**: lo que no esté en esa lista compilada dentro,
simplemente no existe. La GPU es el caso donde eso duele: `i915` es `=m` y **nadie lo
autocarga**.

Fuentes y comandos:

```bash
# Nuestra config real (en la VPS)
C=~/newos/build/output/build/linux-6.11.11/.config
grep -c "^CONFIG_.*=y" $C; grep -c "^CONFIG_.*=m" $C

# Ubuntu 6.11 (oracular): toda la config vive en un solo fichero 'annotations'
git clone --depth 1 --filter=blob:none --sparse \
  https://git.launchpad.net/~ubuntu-kernel/ubuntu/+source/linux/+git/oracular
cd oracular && git show HEAD:debian.master/config/annotations > ubuntu-annotations.txt
# formato: CONFIG_X  policy<{'amd64': 'y', ...}>

# Arch
curl -sL -o arch.config \
  https://raw.githubusercontent.com/archlinux/svntogit-packages/packages/linux/trunk/config
```

⁽¹⁾ **Cifra no verificada al 100%.** Sale del fichero `annotations` de Ubuntu 6.11
(oracular), que es la *definición declarada* de la config; Ubuntu genera el `.config`
real aplicando ese fichero, que puede resolver a algo distinto. **Cifra de respaldo, sin
ese problema: Ubuntu 24.04 LTS trae 5.730 módulos** (medido). En cualquier caso el orden
de magnitud es el mismo: **~90× más módulos que nosotros**. Si se quiere el número exacto,
extraer el `.config` de un `linux-image-*-generic` de Ubuntu (con `dpkg-deb -x` y
`scripts/extract-ikconfig`), que es el fichero realmente compilado.

## Evidencia del fallo (portátil HP real, 2026-10-04)

El arranque desde USB llega a la interfaz pero **Weston NO arranca**:

```
Starting weston: OK        ← S50weston (script DUPLICADO)
Starting weston: FAIL      ← S71weston
Iniciando agentd: AVISO (AgentD no responde)
Iniciando interfaz gráfica: OK
```

Y la pantalla se queda congelada en ese texto → el orbe nunca aparece.

**Causa raíz CONFIRMADA:**

1. La GPU del portátil es Intel → necesita el módulo `i915`.
2. El módulo **está dentro de la imagen** (`.../drm/i915/i915.ko`, 7,4 MB) — pero
   **nadie lo carga**: no existe `/etc/modules` ni ningún script `S1xmodules`.
3. Sin `i915` no se crea `/dev/dri` (`ls /dev/dri` → *No such file or directory*,
   confirmado en el portátil).
4. Sin `/dev/dri` Weston no puede inicializar su backend DRM → muere.

**En la VM nunca se vio** porque VirtualBox usa `vmwgfx`, que va **compilado dentro**
(`CONFIG_DRM_VMWGFX=y`), no como módulo. Es un fallo que solo aparece en hardware real.

### Bugs adicionales encontrados

| # | Bug | Evidencia |
|---|---|---|
| A | **Dos scripts de Weston** (`S50weston` y `S71weston`) | `ls /etc/init.d/` en el target; explica el doble «Starting weston» |
| B | ~~`udev` incompleto~~ **DESCARTADO** | `udevd` SÍ existe, en `/sbin/udevd` (es donde eudev lo pone; `/usr/libexec/udev/` es de systemd-udevd). El `udevadm settle failed` de la pantalla es un **timeout cosmético** de 30 s en `S10udev`: el script sigue igual (`echo done`). **No hay bug.** Comprobado contra la arquitectura original: está así por diseño |
| C | `weston.log` no existe: S71weston no redirige su salida | El fallo de Weston es **invisible** |
| D | **El `kmod load` por modalias no dispara para la GPU** — CAUSA NO CONFIRMADA | Verificado que el mecanismo está completo: `modules.alias` tiene 365 entradas de `i915`, `/etc/udev/udev.conf` existe, `libkmod.so.2` está, y `udevd` lleva el builtin `kmod` dentro. Aun así **en el portátil `/dev/dri` no aparece**. Por qué no dispara es lo que hay que averiguar (hipótesis a probar: el `udevadm trigger` de `S10udev` no ve el dispositivo PCI, o el `settle` que da timeout indica que los eventos no se procesan). **No afirmar causa sin probarla.** |
| E | Al kernel le faltan drivers Ethernet con cable (`=y` por el fragmento, 0 módulos) | Config real, medido |

## Qué dice la documentación original (comprobado)

`docs/informe_actualizado.md` (sección 3.1, extraída del PDF) describe el diseño actual
**como intencionado**:

> «El kernel se compila con una configuración fragmentaria (`build/configs/kernel_config`)
> que **añade subsistemas específicos de AgentOS al defconfig x86-64 estándar**.»

Y el fragmento lo dice de sí mismo, en su cabecera:

> «Se aplica SOBRE x86_64_defconfig. **Objetivo: kernel < 8 MB con solo lo necesario.**»

**Conclusión:** el fragmento manual **no es un descuido, es el diseño documentado**. Su
objetivo declarado era un kernel mínimo. Por tanto esta decisión **modifica un diseño
deliberado**, no arregla un olvido — y el informe de arquitectura habrá que actualizarlo
cuando se ejecute.

Y la sección 3.1 también revela **de dónde viene el fallo real**: lista `CONFIG_DRM_I915
(Intel iGPU)` como si bastara con pedirla. Cierto a medias: pedirla la compila como
módulo, pero **nada la carga**, y el documento no menciona ese paso.

## Decisión

**Adoptar el kernel `x86_64_defconfig` del propio kernel como fuente única de verdad**
en lugar del fragmento manual actual, siguiendo exactamente el método que usan las
distros. Concretamente:

1. **Base:** `BR2_LINUX_KERNEL_USE_DEFCONFIG=y` con `DEFCONFIG="x86_64"` (ya lo tenemos).
2. **Sustituir** `build/configs/kernel_config` (fragmento de ~3.500 líneas con lista de
   hardware escrita a mano) por un **fragmento mínimo** que solo contenga diferencias
   *deliberadas* respecto al defconfig de x86_64: `LOCALVERSION`, arranque silencioso,
   `ZSWAP`, `IKCONFIG`, `PREEMPT_VOLUNTARY`, y **todo lo demás por defecto del kernel**.
3. **Módulos `=m` por defecto del defconfig** → se cargan solos por modalias udev
   (`80-drivers.rules`), igual que en Fedora/Arch.
4. Si se necesita hardware muy concreto, **extender** con otro fragmento, nunca recortar.

### Verificación de la vía en Buildroot

Ya confirmado en el árbol local (`build/buildroot/linux/Config.in`):

```
BR2_LINUX_KERNEL_USE_CUSTOM_CONFIG      → disponible
BR2_LINUX_KERNEL_CUSTOM_CONFIG_FILE     → disponible (para poner una config completa)
BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES  → disponible (lo que usamos hoy)
```

Y las configs de referencia de las distros son descargables (medido: la de Arch,
11.438 líneas desde `archlinux/svntogit-packages`). La de Fedora bloquea con
protección anti-bot en `src.fedoraproject.org` — usar un espejo si se necesita.

## Consecuencias

**Buenas:**
- Arranca en cualquier x86_64 moderno (Intel/AMD/NVIDIA, cualquier chipset de red/disco).
- Deja de ser una lista de hardware a mantener a mano.
- Los fallos aparecen solos y se arreglan solos vía autoload por modalias.

**Costes:**
- **El ISO crece.** Hoy 285 MB; con miles de módulos + `linux-firmware` completo, esperar
  varios cientos de MB más. **Decisión de producto:** ISO grande de instalación vs.
  sistema instalado ligero. Recomendado: ISO completo para instalar, y en el sistema
  instalado podar después de detectar hardware (como hacen las distros).
- **Arranque algo más lento** (initramfs más grande, más firmware que cargar).
- Hay que **recompilar y re-verificar todo** lo que ya dimos por bueno (VM incluida).

## Plan de trabajo

| Fase | Acción | Criterio de aceptación |
|---|---|---|
| **K0** | Arreglo inmediato para el portátil que ya tienes: `/etc/modules` con `i915` (+ script `S15modules`) y **borrar el `S50weston` duplicado** | El orbe aparece en el portátil |
| **K1** | Sustituir el fragmento manual por fragmento mínimo sobre `x86_64_defconfig` | `lib/modules` > 1.000 módulos; `/dev/dri` aparece **sin `modprobe` manual** |
| **K2** | Arreglar `udev` (que `udevd` exista y arranque; `udevadm settle` deja de fallar) | `udevadm info` responde; la autocarga funciona para cualquier dispositivo nuevo |
| **K3** | Añadir log a Weston + quitar el resto de scripts duplicados | Fallos visibles sin `Ctrl+Alt+F2` |
| **K4** | Re-verificar en VM (VirtualBox) y en el portátil real | Arranca en los dos, sin tocarlos a mano |
| **K5** | Medir tamaño del ISO y decidir política de poda | ISO que cabe en USB de 2 GB, o decisión explícita de subir a 4 GB |

**Orden:** K0 desbloquea tu portátil **hoy** (arreglo pequeño y reversible). K1–K2 son el
arreglo de fondo. K1 sin K2 no sirve: con miles de módulos pero sin autocarga por udev,
habría que cargarlos a mano — peor que ahora.

## Riesgos

- **K1 toca el kernel entero** → recompilar en la VPS (16 vCPU) y re-verificar todo.
  Es el cambio de mayor alcance del proyecto hasta ahora.
- **NVIDIA propietario** no está en el kernel libre (usa `nouveau` o el driver de NVIDIA).
  Decidir si AgentOS soporta GPUs NVIDIA solo con `nouveau`.
- **El ISO puede no caber en 2 GB.** Hay que decidirlo, no descubrirlo.

## Lo que NO hay que hacer

- No copiar a ciegas la config de Fedora/Arch: son **más nuevas** que nuestro kernel
  (`6.11.11`) y meterían opciones inexistentes que rompen el build. Usar el
  `x86_64_defconfig` **de la versión exacta** que compilamos, y solo tomar ideas de las
  distros (qué categorías tienen, no qué líneas).
