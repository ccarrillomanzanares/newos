#!/usr/bin/env python3
"""Descarga documentación de proyectos populares de administración de sistemas.

Descarga ficheros Markdown/texto plano (README, guías) desde repositorios
públicos y los trocea en fragmentos por encabezado, guardando JSONL:
{"source", "url", "title", "text"}

Uso:
    python training/data/collect_docs.py --out training/data/raw/docs.jsonl [--sources sources.json]

``--sources`` permite pasar un JSON propio: [{"source": "...", "url": "..."}].
Solo usa la librería estándar (urllib).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger("collect_docs")

# Documentación en formato Markdown con licencias permisivas / libres.
DEFAULT_SOURCES: list[dict[str, str]] = [
    {"source": "systemd", "url": "https://raw.githubusercontent.com/systemd/systemd/main/README"},
    {"source": "nginx-admin-guide",
     "url": "https://raw.githubusercontent.com/nginx/nginx/master/README.md"},
    {"source": "docker-cli", "url": "https://raw.githubusercontent.com/docker/cli/master/README.md"},
    {"source": "tldr-tar", "url": "https://raw.githubusercontent.com/tldr-pages/tldr/main/pages/common/tar.md"},
    {"source": "tldr-find", "url": "https://raw.githubusercontent.com/tldr-pages/tldr/main/pages/common/find.md"},
    {"source": "tldr-systemctl",
     "url": "https://raw.githubusercontent.com/tldr-pages/tldr/main/pages/linux/systemctl.md"},
    {"source": "tldr-journalctl",
     "url": "https://raw.githubusercontent.com/tldr-pages/tldr/main/pages/linux/journalctl.md"},
    {"source": "tldr-ip", "url": "https://raw.githubusercontent.com/tldr-pages/tldr/main/pages/linux/ip.md"},
    {"source": "tldr-apt", "url": "https://raw.githubusercontent.com/tldr-pages/tldr/main/pages/linux/apt.md"},
    {"source": "tldr-ufw", "url": "https://raw.githubusercontent.com/tldr-pages/tldr/main/pages/linux/ufw.md"},
    {"source": "tldr-rsync", "url": "https://raw.githubusercontent.com/tldr-pages/tldr/main/pages/common/rsync.md"},
    {"source": "tldr-ss", "url": "https://raw.githubusercontent.com/tldr-pages/tldr/main/pages/linux/ss.md"},
    {"source": "tldr-df", "url": "https://raw.githubusercontent.com/tldr-pages/tldr/main/pages/common/df.md"},
    {"source": "tldr-du", "url": "https://raw.githubusercontent.com/tldr-pages/tldr/main/pages/common/du.md"},
    {"source": "tldr-crontab",
     "url": "https://raw.githubusercontent.com/tldr-pages/tldr/main/pages/common/crontab.md"},
]

HEADING = re.compile(r"^(#{1,3})\s+(.*)$", re.MULTILINE)


def fetch(url: str, retries: int = 2, timeout: int = 20) -> str | None:
    request = urllib.request.Request(url, headers={"User-Agent": "AgentOS-dataset-builder/0.1"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                logger.warning("404: %s", url)
                return None
            logger.warning("HTTP %s en %s (intento %d)", exc.code, url, attempt + 1)
        except (urllib.error.URLError, TimeoutError) as exc:
            logger.warning("Error de red en %s: %s (intento %d)", url, exc, attempt + 1)
        time.sleep(2 * (attempt + 1))
    return None


def chunk_markdown(text: str, max_chars: int = 3000) -> list[tuple[str, str]]:
    """Trocea un Markdown por encabezados; los trozos largos se parten por párrafos."""
    matches = list(HEADING.finditer(text))
    if not matches:
        sections = [("", text)]
    else:
        sections = []
        if matches[0].start() > 0:
            sections.append(("", text[: matches[0].start()]))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            sections.append((m.group(2).strip(), text[m.end():end]))
    chunks: list[tuple[str, str]] = []
    for title, body in sections:
        body = body.strip()
        if len(body) < 80:
            continue
        while len(body) > max_chars:
            cut = body.rfind("\n\n", 0, max_chars)
            cut = cut if cut > max_chars // 3 else max_chars
            chunks.append((title, body[:cut].strip()))
            body = body[cut:].strip()
        chunks.append((title, body))
    return chunks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="training/data/raw/docs.jsonl")
    parser.add_argument("--sources", help="JSON con la lista de fuentes")
    parser.add_argument("--delay", type=float, default=0.5, help="Pausa entre descargas (s)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    sources = json.loads(Path(args.sources).read_text()) if args.sources else DEFAULT_SOURCES
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with out.open("w", encoding="utf-8") as fh:
        for src in sources:
            text = fetch(src["url"])
            if not text:
                continue
            for title, body in chunk_markdown(text):
                fh.write(json.dumps({"source": src["source"], "url": src["url"], "title": title, "text": body},
                                    ensure_ascii=False) + "\n")
                total += 1
            logger.info("%s: ok", src["source"])
            time.sleep(args.delay)
    logger.info("Guardados %d fragmentos en %s", total, out)
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
