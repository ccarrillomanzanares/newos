#!/usr/bin/env python3
"""Convierte los datos crudos en un dataset conversacional JSONL para fine-tuning.

Entradas (opcionales, las que existan):
  * raw/manpages.jsonl  (collect_manpages.py)
  * raw/docs.jsonl      (collect_docs.py)
  * Ejemplos sintéticos de tool calling generados aquí a partir de plantillas,
    con el MISMO formato que usa AgentD en producción (<tool>{...}</tool> +
    [OBSERVATION ...]).

Salida: formato "messages" (compatible con LLaMA-Factory en modo sharegpt/openai,
TRL y el script train.py):
  {"messages": [{"role": "system", ...}, {"role": "user", ...}, {"role": "assistant", ...}]}

Uso:
    python training/data/format_dataset.py --raw-dir training/data/raw --out training/data/agentos_sft.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path

logger = logging.getLogger("format_dataset")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

SHORT_SYSTEM = ("You are AgentD, the autonomous system administrator of AgentOS. Use tools with "
                '<tool>{"name": ..., "args": {...}}</tool>, one per message, and answer in the user\'s language.')


def system_prompt(full: bool) -> str:
    """System prompt real de AgentD (si se puede importar) o versión corta."""
    if not full:
        return SHORT_SYSTEM
    try:
        from core.agent.tools import build_default_registry
        from core.llm.prompts import build_system_prompt

        return build_system_prompt(build_default_registry(), mode="os", cwd="/root")
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo construir el system prompt completo (%s); usando el corto", exc)
        return SHORT_SYSTEM


def tool(tool_name: str, **args: object) -> str:
    # Parámetro llamado tool_name (no `name`) para que las tools que tienen a su
    # vez un argumento `name` — p. ej. process_manager(action="kill", name="ffmpeg")
    # — no provoquen "got multiple values for argument 'name'".
    return "<tool>" + json.dumps({"name": tool_name, "args": args}, ensure_ascii=False) + "</tool>"


def obs(name: str, body: str) -> str:
    return f"[OBSERVATION {name}]\nstatus: ok\n{body}"


# Trayectorias ReAct sintéticas: (usuario, [(pensamiento+tool, observación)], respuesta final)
SYNTHETIC: list[tuple[str, list[tuple[str, str]], str]] = [
    ("¿Cuánto espacio libre queda en el disco?",
     [("Consulto el uso de disco.\n" + tool("system_monitor", sections=["disk"]),
       obs("system_monitor", "Discos:\n  /  /dev/nvme0n1p2 41.2/98.3 GB (41.9%), libres 57.1 GB [ext4]"))],
     "Te quedan 57,1 GB libres en la partición raíz (41,9 % usado de 98,3 GB)."),
    ("Instala nginx y arráncalo",
     [("Instalo nginx.\n" + tool("package_manager", action="install", packages=["nginx"]),
       obs("package_manager", "exit_code: 0\nstdout:\nSetting up nginx (1.24.0-2) ...")),
      ("Arranco y habilito el servicio.\n" + tool("bash_exec", command="systemctl enable --now nginx && systemctl is-active nginx"),
       obs("bash_exec", "exit_code: 0\nstdout:\nactive"))],
     "nginx 1.24.0 está instalado, habilitado al arranque y en ejecución."),
    ("¿Qué proceso consume más CPU?",
     [("Listo los procesos ordenados por CPU.\n" + tool("process_manager", action="list", sort_by="cpu", limit=5),
       obs("process_manager", "    PID   CPU%   MEM% USER         STATUS    NAME\n   2311   87.0    3.2 user         running   ffmpeg"))],
     "El proceso que más CPU usa es ffmpeg (PID 2311) con un 87 %. ¿Quieres que lo detenga?"),
    ("Mata el proceso ffmpeg",
     [("Envío SIGTERM a ffmpeg (requiere confirmación).\n" + tool("process_manager", action="kill", name="ffmpeg"),
       obs("process_manager", "Señal TERM enviada a: [2311]"))],
     "He terminado ffmpeg (PID 2311) con SIGTERM."),
    ("Muéstrame las últimas líneas del log de nginx",
     [("Leo el log de errores de nginx.\n" + tool("bash_exec", command="tail -n 20 /var/log/nginx/error.log"),
       obs("bash_exec", "exit_code: 0\nstdout:\n2026/10/02 10:01:12 [notice] 811#811: start worker processes"))],
     "El log de nginx solo muestra el arranque normal de los workers; no hay errores recientes."),
    ("Crea un fichero ~/notas.txt con el texto 'reunión a las 10'",
     [("Escribo el fichero.\n" + tool("file_ops", action="write", path="~/notas.txt", content="reunión a las 10\n"),
       obs("file_ops", "Escritos 17 caracteres en /root/notas.txt"))],
     "He creado ~/notas.txt con el texto «reunión a las 10»."),
    ("Abre una terminal",
     [("Lanzo la terminal gráfica.\n" + tool("bash_exec", command="weston-terminal &"),
       obs("bash_exec", "exit_code: 0\nstdout: (vacío)"))],
     "La terminal se está abriendo."),
    ("What's my IP address?",
     [("Checking network interfaces.\n" + tool("system_monitor", sections=["network"]),
       obs("system_monitor", "Red:\n  lo   UP 127.0.0.1\n  eth0 UP 192.168.1.42  tx 120 MB / rx 900 MB"))],
     "Your local IP address is 192.168.1.42 on eth0."),
    ("Hola, ¿qué puedes hacer?", [],
     "Soy AgentD, el administrador de AgentOS. Puedo gestionar paquetes, procesos, ficheros, servicios y red, "
     "vigilar los recursos del sistema y abrir aplicaciones. Dime qué necesitas."),
]


def trajectory_messages(user: str, steps: list[tuple[str, str]], final: str) -> list[dict[str, str]]:
    msgs = [{"role": "user", "content": user}]
    for assistant, observation in steps:
        msgs.append({"role": "assistant", "content": assistant})
        msgs.append({"role": "user", "content": observation})
    msgs.append({"role": "assistant", "content": final})
    return msgs


def from_manpage(rec: dict) -> list[list[dict[str, str]]]:
    """Genera pares pregunta/respuesta a partir de una man page."""
    page = rec["page"]
    convs = []
    desc = rec.get("description", "").strip()
    if desc:
        first = desc.split("\n\n")[0][:1200]
        convs.append([{"role": "user", "content": f"What does the `{page}` command do?"},
                      {"role": "assistant", "content": f"{rec.get('name') or page}\n\n{first}"}])
    if rec.get("synopsis"):
        convs.append([{"role": "user", "content": f"Show the usage/synopsis of `{page}`."},
                      {"role": "assistant", "content": f"```\n{rec['synopsis'][:1200]}\n```"}])
    if rec.get("options"):
        convs.append([{"role": "user", "content": f"List the main options of `{page}`."},
                      {"role": "assistant", "content": rec["options"][:2500]}])
    return convs


def from_doc(rec: dict) -> list[list[dict[str, str]]]:
    title = rec.get("title") or rec.get("source")
    return [[{"role": "user", "content": f"Explain '{title}' from the {rec['source']} documentation."},
             {"role": "assistant", "content": rec["text"][:3000]}]]


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        logger.info("No existe %s (se omite)", path)
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw-dir", default="training/data/raw")
    parser.add_argument("--out", default="training/data/agentos_sft.jsonl")
    parser.add_argument("--eval-ratio", type=float, default=0.05)
    parser.add_argument("--synthetic-repeat", type=int, default=20,
                        help="Veces que se repiten los ejemplos de tool calling (sobremuestreo)")
    parser.add_argument("--full-system-prompt", action="store_true",
                        help="Usar el system prompt real de AgentD en los ejemplos de tool calling")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    random.seed(args.seed)

    raw = Path(args.raw_dir)
    agent_system = system_prompt(args.full_system_prompt)
    knowledge_system = "You are AgentD, an expert Linux system administrator. Answer accurately and concisely."

    examples: list[dict] = []
    for _ in range(max(1, args.synthetic_repeat)):
        for user, steps, final in SYNTHETIC:
            examples.append({"messages": [{"role": "system", "content": agent_system},
                                          *trajectory_messages(user, steps, final)]})
    for rec in read_jsonl(raw / "manpages.jsonl"):
        for conv in from_manpage(rec):
            examples.append({"messages": [{"role": "system", "content": knowledge_system}, *conv]})
    for rec in read_jsonl(raw / "docs.jsonl"):
        for conv in from_doc(rec):
            examples.append({"messages": [{"role": "system", "content": knowledge_system}, *conv]})

    random.shuffle(examples)
    n_eval = int(len(examples) * args.eval_ratio)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    eval_path = out.with_name(out.stem + "_eval.jsonl")
    for path, subset in ((eval_path, examples[:n_eval]), (out, examples[n_eval:])):
        with path.open("w", encoding="utf-8") as fh:
            for ex in subset:
                fh.write(json.dumps(ex, ensure_ascii=False) + "\n")
    logger.info("Dataset: %d ejemplos de entrenamiento en %s, %d de evaluación en %s",
                len(examples) - n_eval, out, n_eval, eval_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
