#!/usr/bin/env python3
"""Extrae las man pages instaladas en el sistema y las guarda como JSONL.

Cada línea: {"page", "section", "name", "synopsis", "description", "options", "text"}

Uso:
    python training/data/collect_manpages.py --out training/data/raw/manpages.jsonl \
        [--sections 1 8] [--limit 500] [--workers 8]

Requiere: man-db (``apt install man-db manpages``). En contenedores Docker
mínimos las man pages suelen estar eliminadas (ver /etc/dpkg/dpkg.cfg.d/excludes).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger("collect_manpages")

SECTION_HEADER = re.compile(r"^([A-Z][A-Z0-9 /_-]{1,40})$")
MAX_TEXT_CHARS = 12000


def list_pages(sections: list[str]) -> list[tuple[str, str]]:
    """Devuelve [(página, sección)] usando ``man -k .`` (apropos)."""
    pages: set[tuple[str, str]] = set()
    for section in sections:
        res = subprocess.run(["man", "-k", "-s", section, "."], capture_output=True, text=True)
        for line in res.stdout.splitlines():
            m = re.match(r"^(\S+)\s+\((\w+)\)", line)
            if m:
                pages.add((m.group(1), m.group(2)))
    return sorted(pages)


def render_page(page: str, section: str) -> str | None:
    """Renderiza una man page como texto plano."""
    env = {**os.environ, "MANWIDTH": "100", "MAN_KEEP_FORMATTING": "0", "LC_ALL": "C.UTF-8"}
    try:
        res = subprocess.run(["man", "-P", "cat", section, page], capture_output=True, text=True, env=env,
                             timeout=20)
    except subprocess.TimeoutExpired:
        return None
    if res.returncode != 0 or not res.stdout.strip():
        return None
    # Eliminar secuencias de retroceso (negrita/subrayado de nroff)
    return re.sub(r".\x08", "", res.stdout)


def split_sections(text: str) -> dict[str, str]:
    """Divide el texto en secciones NAME, SYNOPSIS, DESCRIPTION, OPTIONS..."""
    sections: dict[str, list[str]] = {}
    current = "HEADER"
    for line in text.splitlines():
        if SECTION_HEADER.match(line.rstrip()):
            current = line.strip()
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def dedent_block(text: str) -> str:
    lines = [ln.rstrip() for ln in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(ln[7:] if ln.startswith(" " * 7) else ln.strip() for ln in lines))


def process(page: str, section: str) -> dict | None:
    text = render_page(page, section)
    if not text:
        return None
    parts = split_sections(text)
    name_line = " ".join(parts.get("NAME", "").split())
    return {
        "page": page,
        "section": section,
        "name": name_line,
        "synopsis": dedent_block(parts.get("SYNOPSIS", ""))[:1500],
        "description": dedent_block(parts.get("DESCRIPTION", ""))[:4000],
        "options": dedent_block(parts.get("OPTIONS", ""))[:5000],
        "text": text[:MAX_TEXT_CHARS],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="training/data/raw/manpages.jsonl")
    parser.add_argument("--sections", nargs="+", default=["1", "8"], help="Secciones de man (1=usuario, 8=admin)")
    parser.add_argument("--limit", type=int, default=0, help="Máximo de páginas (0 = todas)")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    pages = list_pages(args.sections)
    if not pages:
        logger.error("No se encontraron man pages. ¿Está instalado man-db y el índice generado (mandb)?")
        return 1
    if args.limit:
        pages = pages[: args.limit]
    logger.info("Procesando %d man pages", len(pages))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out.open("w", encoding="utf-8") as fh, ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(process, p, s): p for p, s in pages}
        for fut in as_completed(futures):
            record = fut.result()
            if record and record["description"]:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
    logger.info("Guardadas %d man pages en %s", written, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
