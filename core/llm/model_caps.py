"""
Capacidades del modelo: se PREGUNTAN, no se tabulan.

Filosofia
---------
No hay tabla que mantener. Cuando el SO necesita saber que puede hacer el modelo
(ventana de contexto, herramientas, vision...), se lo PREGUNTA:

  * LISTA  -> GET  {base}/api/tags            (Ollama)  |  GET {base}/models (OpenAI)
  * FICHA  -> POST {base}/api/show   {"model":X}         (ctx, family, params, quant)
  * SONDA  -> POST {base}/tokenize   {"model":X,"prompt":"*500"}  (mide el tope real)

Si el backend no sabe contestar, se cae a valores PRUDENTES y se avisa.

En LOCAL ademas el limite no lo pone el modelo, lo pone EL HARDWARE del equipo:
el SO se instala en muchos equipos distintos, asi que el contexto se calcula con
la RAM real, el tamano del modelo y su arquitectura (KV cache).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
import urllib.error
from dataclasses import dataclass, asdict, field

# --------------------------------------------------------------------------- #
#  HARDWARE REAL
# --------------------------------------------------------------------------- #
@dataclass
class Hardware:
    ram_total_gb: float
    ram_libre_gb: float
    cores: int
    gpu: str | None = None
    vram_gb: float | None = None


def hw_info() -> Hardware:
    ram_total = ram_libre = 0.0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    ram_total = int(line.split()[1]) / 1e6
                elif line.startswith("MemAvailable:"):
                    ram_libre = int(line.split()[1]) / 1e6
    except OSError:
        pass
    cores = os.cpu_count() or 1
    gpu = None
    vram = None
    # GPU: intento barato, sin dependencias
    for cmd in (["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],):
        if shutil.which(cmd[0]):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
                if out:
                    partes = [p.strip() for p in out.splitlines()[0].split(",")]
                    gpu, vram = partes[0], int(partes[1]) / 1024
                    break
            except Exception:
                pass
    if gpu is None:
        for p in ("/sys/class/drm/card0/device/vendor",):
            if os.path.exists(p):
                gpu = "integrada"
    return Hardware(round(ram_total, 1), round(ram_libre, 1), cores, gpu, vram)


# --------------------------------------------------------------------------- #
#  PREGUNTAR AL MODELO
# --------------------------------------------------------------------------- #
@dataclass
class ModeloInfo:
    nombre: str
    backend: str                       # "cloud" | "local"
    encontrado: bool = False
    context_length: int | None = None  # ventana oficial del modelo
    capabilities: list[str] = field(default_factory=list)
    family: str = ""
    parameter_billions: float | None = None
    quant: str = ""
    size_gb: float | None = None       # tamano del fichero (local)
    arch: dict = field(default_factory=dict)  # block_count, heads, kv... para KV
    fuente: str = ""


def _post(url: str, payload: dict, key: str, timeout: float = 20.0) -> dict | None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _get(url: str, key: str, timeout: float = 20.0) -> dict | None:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def listar_modelos(base_url: str, key: str) -> list[str]:
    """Pregunta que modelos hay. Ollama /api/tags y OpenAI /v1/models."""
    d = _get(_base_nativa(base_url) + "/api/tags", key)
    if d and "models" in d:
        return [m.get("name", "") for m in d["models"] if m.get("name")]
    d = _get(base_url.rstrip("/") + "/models", key)
    if d and "data" in d:
        return [m.get("id", "") for m in d["data"] if m.get("id")]
    return []


def _base_nativa(base_url: str) -> str:
    """
    Los endpoints nativos de Ollama (/api/show, /api/tags) viven en la RAIZ del
    servidor, pero la config del agente apunta a la API OpenAI-compatible
    (.../v1). Aqui se quita el sufijo /v1 para poder preguntar la ficha.
    """
    b = base_url.rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3]
    return b


def _variantes_nombre(modelo: str) -> list[str]:
    """
    El servidor no siempre usa el mismo nombre que el usuario: p.ej. lista
    'deepseek-v4.1-flash' pero el usuario escribe 'deepseek-v4.1-flash:cloud'.
    Se prueban las variantes razonables hasta que una conteste.
    """
    v = []
    if modelo.endswith(":cloud"):
        v.append(modelo[:-6])
    v.append(modelo)
    if ":" in modelo:
        v.append(modelo.rsplit(":", 1)[0])
    else:
        v.append(modelo + ":latest")
    return list(dict.fromkeys(v))


def preguntar_capacidades(base_url: str, key: str, modelo: str,
                          backend: str = "auto") -> ModeloInfo:
    """Pregunta al servidor que sabe hacer ese modelo."""
    base = _base_nativa(base_url)
    info = ModeloInfo(nombre=modelo, backend="cloud" if "cloud" in modelo or base_url.startswith("https") else "local")

    d = None
    usado = modelo
    for nombre in _variantes_nombre(modelo):
        d = _post(base + "/api/show", {"model": nombre}, key)
        if not d:
            d = _get(base + f"/api/show?model={nombre}", key)   # Ollama viejo
        if d:
            usado = nombre
            break
    if not d:
        info.fuente = "sin respuesta: no se pudo consultar la ficha"
        return info

    info.encontrado = True
    info.fuente = f"{base}/api/show (nombre: {usado})"
    info.capabilities = d.get("capabilities") or []
    det = d.get("details") or {}
    info.family = det.get("family") or ""
    info.quant = det.get("quantization_level") or ""
    ps = det.get("parameter_size")
    if ps and str(ps).isdigit():
        info.parameter_billions = round(int(ps) / 1e9, 1)
    elif ps:
        info.parameter_billions = None

    mi = d.get("model_info") or {}
    # contexto: la clave varia segun familia (llama.context_length, deepseek_v41.context_length...)
    for k, v in mi.items():
        if k.endswith("context_length") and isinstance(v, int):
            info.context_length = v
        # arquitectura para el KV cache (local)
        corta = k.split(".")[-1]
        if corta in ("block_count", "head_count", "head_count_kv", "embedding_length",
                     "key_length", "value_length"):
            info.arch[corta] = v
    return info


# --------------------------------------------------------------------------- #
#  LOCAL: cuanto contexto CABE en ESTE equipo
# --------------------------------------------------------------------------- #
def contexto_local(info: ModeloInfo, hw: Hardware, bytes_elem: int = 2,
                   overhead_gb: float = 1.6) -> tuple[int, str]:
    """
    Calcula el contexto maximo que cabe en la RAM real de ESTE equipo.

    KV cache = 2 * capas * cabezas_kv * dim_cabeza * tokens * bytes
    (la 'dim_cabeza' se deduce: embedding_length / head_count)
    """
    pesos_gb = info.size_gb
    if pesos_gb is None and info.parameter_billions:
        pesos_gb = info.parameter_billions * 0.62      # estimacion Q4_K_M
    if pesos_gb is None:
        return 8192, "sin datos del modelo: valor prudente"

    arch = info.arch
    capas = arch.get("block_count")
    n_head = arch.get("head_count")
    n_kv = arch.get("head_count_kv") or n_head
    emb = arch.get("embedding_length")
    if not (capas and n_head and n_kv and emb):
        # sin arquitectura: regla gruesa ~0.10 GB por 1k tokens en un 8B
        libre = hw.ram_total_gb - pesos_gb - overhead_gb
        tokens = max(2048, int(libre / (0.012 * max(pesos_gb / 5.0, 0.3)) * 1000))
    else:
        dim = emb // n_head
        por_token = 2 * capas * n_kv * dim * bytes_elem          # bytes/token
        libre_gb = hw.ram_total_gb - pesos_gb - overhead_gb
        tokens = max(2048, int(libre_gb * 1e9 / max(por_token, 1)))

    # a potencia de 2
    ctx = 2048
    while ctx * 2 <= tokens and ctx < 262144:
        ctx *= 2
    if info.context_length:
        ctx = min(ctx, info.context_length)
    motivo = (f"pesos {pesos_gb:.1f} GB + reserva {overhead_gb} GB sobre "
              f"{hw.ram_total_gb:.1f} GB de RAM = {tokens} tokens posibles -> {ctx}")
    return ctx, motivo


# --------------------------------------------------------------------------- #
#  AJUSTE FINAL (lo que consume el agente)
# --------------------------------------------------------------------------- #
@dataclass
class Ajuste:
    modelo: str
    backend: str
    n_ctx: int
    max_tokens: int
    iteraciones: int
    tool_output: int
    keep_last: int
    capacidades: list[str]
    motivo: str
    aviso: str = ""
    encontrado: bool = True      # False = no se pudo consultar la ficha (plan B)


def _potencia_2_max(limite: int, tope: int = 32768) -> int:
    c = 2048
    while c * 2 <= limite and c < tope:
        c *= 2
    return c


def autotune(base_url: str, key: str, modelo: str, hw: Hardware | None = None) -> Ajuste:
    """Pregunta al modelo y al hardware; devuelve los parametros ya ajustados."""
    hw = hw or hw_info()
    info = preguntar_capacidades(base_url, key, modelo)

    es_cloud = info.backend == "cloud"
    avisos = []
    if not info.encontrado:
        avisos.append("no he podido consultar la ficha del modelo: uso valores prudentes")

    if es_cloud:
        # manda el modelo. 1/32 del contexto como ventana de trabajo del agente,
        # con suelo y techo sensatos. Si no hay ficha, 16384: seguro para
        # practicamente cualquier modelo moderno y 2x el valor por defecto viejo.
        cap = info.context_length or 16384
        n_ctx = _potencia_2_max(cap // 32, tope=65536)
        motivo = (f"nube: el modelo soporta {cap} tokens; uso {n_ctx} como ventana de trabajo"
                  if info.context_length else "nube: contexto oficial desconocido")
    else:
        n_ctx, motivo = contexto_local(info, hw)
        if info.size_gb and info.parameter_billions:
            avisos.append(f"local: {info.parameter_billions}B ({info.size_gb:.1f} GB) en "
                          f"{hw.ram_total_gb:.1f} GB de RAM")

    max_tokens = min(2048, max(512, n_ctx // 8))
    iteraciones = 24 if es_cloud else 16
    tool_output = min(8000, max(3000, n_ctx // 4))
    keep_last = 12 if n_ctx >= 16384 else 6

    return Ajuste(modelo, info.backend, n_ctx, max_tokens, iteraciones,
                  tool_output, keep_last, info.capabilities, motivo,
                  "; ".join(avisos), encontrado=info.encontrado)


def guardar(aj: Ajuste, ruta: str) -> None:
    try:
        os.makedirs(os.path.dirname(ruta), exist_ok=True)
        with open(ruta, "w") as f:
            json.dump(asdict(aj), f, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    base = os.environ.get("AGENTOS_OPENAI_BASE_URL", "https://ollama.com/v1")
    key = os.environ.get("AGENTOS_OPENAI_API_KEY", "")
    hw = hw_info()
    print(f"HARDWARE: {hw.ram_total_gb} GB RAM ({hw.ram_libre_gb} libres), "
          f"{hw.cores} cores, GPU={hw.gpu} {hw.vram_gb or ''}")
    print()

    modelo = sys.argv[1] if len(sys.argv) > 1 else "deepseek-v4.1-flash:cloud"
    if "listar" in sys.argv:
        print("MODELOS DISPONIBLES:", listar_modelos(base, key))
        raise SystemExit

    aj = autotune(base, key, modelo, hw)
    print(f"MODELO   : {aj.modelo}")
    print(f"BACKEND  : {aj.backend}")
    print(f"n_ctx    : {aj.n_ctx}")
    print(f"max_tok  : {aj.max_tokens}")
    print(f"pasos    : {aj.iteraciones}")
    print(f"tool_out : {aj.tool_output}")
    print(f"keep_last: {aj.keep_last}")
    print(f"caps     : {aj.capacidades}")
    print(f"motivo   : {aj.motivo}")
    if aj.aviso:
        print(f"AVISO    : {aj.aviso}")
